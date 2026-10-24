"""
core/genai_bridge.py — a google.genai-compatible shim over the configured LLM.

The bundled actions were written against the official ``google-genai`` SDK.
This module gives them the same surface (``genai.Client(...)``,
``client.models.generate_content(...)``, ``response.text``,
``response.candidates[...].content.parts[...]``) while routing every call to
the OpenAI-compatible provider configured in config/api_keys.json (xkiro by
default) — so the actions work with no Google API key at all.

Import it wherever an action would import the real SDK::

    from core import genai_bridge as genai   # instead of: from google import genai
    client = genai.Client(api_key=...)       # api_key ignored — provider config wins

Real ``google.genai.types`` objects (Part.from_bytes, GenerateContentConfig)
are accepted and read via duck-typing, so actions can keep importing them.
``{"tools": [{"google_search": {}}]}`` configs are honoured by fetching live
DuckDuckGo results and injecting them as grounding context.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

import requests

try:
    from .llm_client import get_llm_settings, _auth_headers, _load_config
except ImportError:  # direct execution fallback
    from llm_client import get_llm_settings, _auth_headers, _load_config

_TIMEOUT = 180
_MAX_TOOL_CHARS = 12000


# ─────────────────────────────────────────────────────────────────────────────
# Response objects (mirror the real SDK's attribute surface)
# ─────────────────────────────────────────────────────────────────────────────
class _Part:
    def __init__(self, text: str):
        self.text = text


class _Content:
    def __init__(self, parts: list[_Part]):
        self.parts = parts


class _Candidate:
    def __init__(self, parts: list[_Part]):
        self.content = _Content(parts)
        self.finish_reason = "STOP"


class Response:
    def __init__(self, text: str):
        self._parts = [_Part(text)] if text else []
        self.candidates = [_Candidate(self._parts)]
        self.model_version = "provider"

    @property
    def text(self) -> str:
        if not self._parts:
            raise ValueError("The model returned an empty response.")
        return self._parts[0].text


# ─────────────────────────────────────────────────────────────────────────────
# Duck-typed helpers for real google.genai objects
# ─────────────────────────────────────────────────────────────────────────────
def _part_image(part: Any) -> tuple[bytes, str] | None:
    """Extract (bytes, mime) from a real types.Part / inline blob, else None."""
    blob = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
    if blob is None:
        return None
    data = getattr(blob, "data", None)
    mime = getattr(blob, "mime_type", None) or getattr(blob, "mimeType", None) or "image/png"
    if data is None:
        return None
    if isinstance(data, str):            # base64-encoded string form
        try:
            data = base64.b64decode(data)
        except Exception:
            return None
    return data, mime


def _cfg_get(config: Any, name: str) -> Any:
    """Read a field from a dict config or a real GenerateContentConfig."""
    if config is None:
        return None
    if isinstance(config, dict):
        return config.get(name)
    return getattr(config, name, None)


def _wants_grounding(config: Any) -> bool:
    tools = _cfg_get(config, "tools")
    if not tools:
        return False
    for t in tools:
        if isinstance(t, dict) and ("google_search" in t or "googleSearch" in t):
            return True
        if hasattr(t, "google_search") or hasattr(t, "googleSearch"):
            return True
    return False


def _ddg_context(query: str) -> str:
    """Live DuckDuckGo results as grounding text — '' on any failure."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return ""
    try:
        results = []
        with DDGS() as dd:
            for r in dd.text(query, max_results=8):
                results.append(
                    f"- {r.get('title', '')}: {r.get('body', '')} (source: {r.get('href', '')})"
                )
        if not results:
            return ""
        return ("Live web search results for the query above (fetched just now):\n"
                + "\n".join(results) + "\n\nUse these results to answer. "
                  "Cite source names naturally in your answer.")
    except Exception as e:
        print(f"[genai_bridge] DDG grounding failed (non-fatal): {e}")
        return ""


def _system_from_config(config: Any) -> str:
    si = _cfg_get(config, "system_instruction") or _cfg_get(config, "systemInstruction")
    if si is None:
        return ""
    if hasattr(si, "parts"):                       # real types.Content
        return " ".join(getattr(p, "text", "") for p in si.parts)
    if isinstance(si, (list, tuple)):
        return " ".join(str(s) for s in si)
    return str(si)


def _contents_to_messages(contents: Any, config: Any) -> list[dict]:
    """Convert genai-style contents into OpenAI chat messages."""
    messages: list[dict] = []

    sys_txt = _system_from_config(config)
    if sys_txt:
        messages.append({"role": "system", "content": sys_txt})

    user_parts: list[dict] = []
    if isinstance(contents, str):
        user_parts.append({"type": "text", "text": contents})
    elif isinstance(contents, (list, tuple)):
        for item in contents:
            if isinstance(item, str):
                user_parts.append({"type": "text", "text": item})
                continue
            img = _part_image(item)
            if img:
                data, mime = img
                b64 = base64.b64encode(data).decode("ascii")
                user_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
                continue
            txt = getattr(item, "text", None)
            if txt:
                user_parts.append({"type": "text", "text": txt})
                continue
            # dict-shaped part
            if isinstance(item, dict):
                if "text" in item:
                    user_parts.append({"type": "text", "text": str(item["text"])})
                img = _part_image(item)
                if img:
                    data, mime = img
                    b64 = base64.b64encode(data).decode("ascii")
                    user_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
    if not user_parts:
        user_parts.append({"type": "text", "text": "(empty request)"})
    messages.append({"role": "user", "content": user_parts})
    return messages


def _resolve_model(model: str | None) -> str:
    """Map requested Gemini model names onto the configured provider model."""
    _, configured = get_llm_settings()
    if not model:
        return configured
    m = (model or "").lower()
    if "gemini" in m or m in ("flash", "flash-lite", "pro"):
        return configured
    return model


# ─────────────────────────────────────────────────────────────────────────────
# The shim
# ─────────────────────────────────────────────────────────────────────────────
class _Models:
    def generate_content(self, model: str | None = None, contents: Any = None,
                         config: Any = None, **kw) -> Response:
        url, _ = get_llm_settings()
        resolved = _resolve_model(model)
        messages = _contents_to_messages(contents, config)

        # Grounding: inject live DDG results when a google_search tool is requested.
        if _wants_grounding(config):
            q = ""
            for m in messages:
                if m["role"] == "user":
                    c = m["content"]
                    q = c if isinstance(c, str) else " ".join(
                        p.get("text", "") for p in c if p.get("type") == "text")
                    break
            if q.strip():
                ctx = _ddg_context(q.strip()[:300])
                if ctx:
                    messages.insert(0, {"role": "system",
                                        "content": ctx[:_MAX_TOOL_CHARS]})

        payload = {
            "model": resolved,
            "messages": messages,
            "stream": False,
            "max_tokens": 4096,
        }
        try:
            resp = requests.post(f"{url}/chat/completions", json=payload,
                                 headers=_auth_headers(), timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        except Exception as e:
            raise RuntimeError(f"genai_bridge request failed: {e}") from e
        return Response(text.strip())


class Client:
    """Drop-in for genai.Client — provider settings come from config/api_keys.json."""

    def __init__(self, api_key: str | None = None, **kw):
        # api_key accepted for signature compatibility; the configured provider
        # credentials always win so stale keys in action code can't redirect calls.
        self.models = _Models()

    def __repr__(self) -> str:  # helpful in logs
        try:
            _, model = get_llm_settings()
        except Exception:
            model = "?"
        return f"<genai_bridge.Client provider-model={model!r}>"
