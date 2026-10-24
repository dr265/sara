"""
SARA Desktop — LLM client.

Supports three backends — selected via  "llm_provider"  in config/api_keys.json:

  "llm_provider": "xkiro"   (DEFAULT — cloud, recommended)
        Routes through the Xkiro API (https://api.xkiro.com/v1).
        OpenAI-compatible gateway with access to Gemini, Claude, GPT, Mistral.
        Set "llm_api_key" to your Xkiro key in api_keys.json.
        Fast model : "google/gemini-2.0-flash"
        Best model : "anthropic/claude-sonnet-5"

  "llm_provider": "openai"
        Any local OpenAI-compatible server: LM Studio, Jan, LocalAI, vLLM.
        Set "llm_url": "http://localhost:1234"  in api_keys.json.

  "llm_provider": "ollama"
        Local Ollama server (offline, no API key needed).
        Download: https://ollama.com — default port 11434.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Generator

import requests

# Sentence boundary — splits on [.!?] + whitespace or blank line
_SENT_END = re.compile(r'(?<=[.!?])\s+|(?<=\n)\s*\n')


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR    = get_base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# Per-provider defaults
_PROVIDER_DEFAULTS: dict[str, dict] = {
    "xkiro": {
        "url":   "https://api.xkiro.com/v1",
        "model": "mistralai/mistral-large-2512",
    },
    "anymodel": {
        "url":   "https://anymodel.org/v1",
        "model": "ag/gemini-2.5-flash",
    },
    "gemini": {
        "url":   "https://anymodel.org/v1",
        "model": "ag/gemini-2.5-flash",
    },
    "openai": {
        "url":   "http://localhost:1234",
        "model": "qwen2.5-7b-instruct",
    },
    "ollama": {
        "url":   "http://localhost:11434",
        "model": "llama3.2",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_llm_provider() -> str:
    """Returns 'xkiro', 'anymodel', 'gemini', 'openai', or 'ollama'."""
    raw = _load_config().get("llm_provider", "xkiro").strip().lower()
    if raw in ("openai", "lmstudio", "localai", "jan", "llamacpp"):
        return "openai"
    if raw in ("anymodel", "gemini"):
        return "anymodel"
    if raw == "ollama":
        return "ollama"
    return "xkiro"   # default


def get_llm_settings() -> tuple[str, str]:
    """Returns (base_url, model_name)."""
    cfg      = _load_config()
    provider = get_llm_provider()
    defs     = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["xkiro"])

    url   = cfg.get("llm_url",   defs["url"]).rstrip("/")
    model = cfg.get("llm_model", defs["model"])

    # If model is explicitly gemini or provider is anymodel/gemini, route to AnyModel
    if "gemini" in model.lower() or provider in ("anymodel", "gemini"):
        url = cfg.get("anymodel_base_url", "https://anymodel.org/v1").rstrip("/")
        if not model.startswith("ag/"):
            model = "ag/gemini-2.5-flash"
    elif provider == "xkiro":
        url = "https://api.xkiro.com/v1"

    return url, model


def _auth_headers(target_url: str | None = None) -> dict[str, str]:
    """Build request headers — includes Authorization for cloud providers."""
    import os
    cfg = _load_config()
    url = (target_url or "").lower()
    
    if "anymodel" in url:
        key = cfg.get("anymodel_api_key") or os.environ.get("ANYMODEL_API_KEY") or ""
    elif "xkiro" in url:
        key = cfg.get("llm_api_key") or os.environ.get("XKIRO_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    else:
        key = cfg.get("llm_api_key") or os.environ.get("LLM_API_KEY") or ""

    hdrs: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent":   "SARA-Desktop/1.0",
    }
    if key:
        hdrs["Authorization"] = f"Bearer {key}"
    return hdrs


# ─────────────────────────────────────────────────────────────────────────────
# Server lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def ensure_ollama_running(timeout: int = 15) -> bool:
    url, _   = get_llm_settings()
    provider = get_llm_provider()

    if provider in ("xkiro", "openai"):
        health = f"{url}/models"
        try:
            ok = requests.get(health, headers=_auth_headers(), timeout=8).status_code == 200
            if ok:
                print(f"[LLM] SARA LLM endpoint reachable at {url}")
            else:
                print(f"[LLM] Endpoint {url} returned non-200 — check your key / connection.")
            return ok
        except Exception:
            print(f"[LLM] Cannot reach endpoint at {url}.")
            return False

    health = f"{url}/api/tags"

    def _is_up() -> bool:
        try:
            return requests.get(health, timeout=3).status_code == 200
        except Exception:
            return False

    if _is_up():
        return True

    print("[LLM] Ollama not running — launching 'ollama serve'…")
    try:
        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(["ollama", "serve"], **kwargs)
    except FileNotFoundError:
        print("[LLM] 'ollama' command not found. Install from https://ollama.com")
        return False
    except Exception as e:
        print(f"[LLM] Could not launch Ollama: {e}")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        if _is_up():
            print("[LLM] Ollama started successfully.")
            return True

    print("[LLM] Ollama did not respond within the timeout.")
    return False


def warmup_model(system_prompt: str | None = None) -> bool:
    url, model = get_llm_settings()
    provider   = get_llm_provider()
    print(f"[LLM] Warming up '{model}' via {provider}…")

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": "hi"})

    if provider in ("xkiro", "openai"):
        payload = {
            "model":      model,
            "messages":   messages,
            "stream":     False,
            "max_tokens": 1,
        }
        try:
            resp = requests.post(
                f"{url}/chat/completions",
                json=payload,
                headers=_auth_headers(),
                timeout=180,
            )
            resp.raise_for_status()
            print(f"[LLM] '{model}' ready.")
            return True
        except Exception as e:
            print(f"[LLM] Warmup failed (non-fatal): {e}")
            return False

    # Ollama
    payload = {
        "model":      model,
        "messages":   messages,
        "stream":     False,
        "keep_alive": -1,
        "options":    {"num_predict": 1, "num_gpu": 99},
    }
    try:
        resp = requests.post(f"{url}/api/chat", json=payload, timeout=180)
        resp.raise_for_status()
        print(f"[LLM] '{model}' loaded and KV cache primed.")
        return True
    except Exception as e:
        print(f"[LLM] Warmup failed (non-fatal): {e}")
        return False


def check_model_available(log: Callable | None = None) -> bool:
    if get_llm_provider() != "ollama":
        return True
    url, model = get_llm_settings()
    try:
        resp = requests.get(f"{url}/api/tags", timeout=5)
        resp.raise_for_status()
        pulled     = [m.get("name", "") for m in resp.json().get("models", [])]
        model_base = model.split(":")[0]
        found      = any(
            m == model or m == model_base or m.startswith(model_base + ":")
            for m in pulled
        )
        if not found:
            available = ", ".join(pulled) if pulled else "none"
            warn = (
                f"WRN: Model '{model}' is not pulled in Ollama.\n"
                f"     Available: {available}\n"
                f"     Fix: ollama pull {model}"
            )
            print(warn)
            if log:
                log(f"WRN: '{model}' not found — run: ollama pull {model}")
        return found
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Shared OpenAI-compatible non-streaming call (xkiro + openai providers)
# ─────────────────────────────────────────────────────────────────────────────

def _openai_call(
    messages: list,
    tools:    list | None,
    timeout:  int,
    url:      str,
    model:    str,
) -> dict:
    endpoint = f"{url}/chat/completions"
    payload: dict = {
        "model":      model,
        "messages":   messages,
        "stream":     False,
        "max_tokens": 4096,
    }
    if tools:
        payload["tools"]       = tools
        payload["tool_choice"] = "auto"
    try:
        resp = requests.post(
            endpoint, json=payload,
            headers=_auth_headers(url), timeout=timeout,
        )
        if resp.status_code in (403, 404) and "xkiro" in url:
            # Fallback to AnyModel Gemini
            fb_url = "https://anymodel.org/v1"
            fb_model = "ag/gemini-2.5-flash"
            resp = requests.post(
                f"{fb_url}/chat/completions",
                json={**payload, "model": fb_model},
                headers=_auth_headers(fb_url),
                timeout=timeout,
            )
        resp.raise_for_status()
        choice  = resp.json().get("choices", [{}])[0]
        msg     = choice.get("message", {})
        raw_tc  = msg.get("tool_calls") or []
        tc_list = [
            {
                "id":       t.get("id", ""),
                "function": {
                    "name":      t["function"]["name"],
                    "arguments": (
                        json.loads(t["function"]["arguments"])
                        if isinstance(t["function"].get("arguments"), str)
                        else t["function"].get("arguments", {})
                    ),
                },
            }
            for t in raw_tc
        ]
        return {
            "content":    (msg.get("content") or "").strip(),
            "tool_calls": tc_list,
        }
    except Exception as e:
        if "anymodel" not in url:
            try:
                fb_url = "https://anymodel.org/v1"
                fb_model = "ag/gemini-2.5-flash"
                fb_resp = requests.post(
                    f"{fb_url}/chat/completions",
                    json={**payload, "model": fb_model},
                    headers=_auth_headers(fb_url),
                    timeout=timeout,
                )
                if fb_resp.status_code == 200:
                    choice = fb_resp.json().get("choices", [{}])[0]
                    msg = choice.get("message", {})
                    return {
                        "content": (msg.get("content") or "").strip(),
                        "tool_calls": [],
                    }
            except Exception:
                pass
        raise RuntimeError(f"SARA LLM call failed [{model}]: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def call_llm(
    messages: list,
    tools:    list | None = None,
    timeout:  int = 120,
) -> dict:
    """
    Non-streaming chat.  Returns {"content": str, "tool_calls": list}.
    """
    url, model = get_llm_settings()
    provider   = get_llm_provider()

    if provider in ("xkiro", "openai"):
        return _openai_call(messages, tools, timeout, url, model)

    # ── Ollama ──────────────────────────────────────────────────────────────
    endpoint = f"{url}/api/chat"
    payload  = {
        "model":      model,
        "messages":   messages,
        "stream":     False,
        "keep_alive": -1,
        "options":    {"num_predict": 150, "num_gpu": 99},
    }
    if tools:
        payload["tools"] = tools

    try:
        resp = requests.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
        msg = resp.json().get("message", {})
        return {
            "content":    (msg.get("content") or "").strip(),
            "tool_calls": msg.get("tool_calls") or [],
        }
    except requests.exceptions.ConnectionError as e:
        print(f"[LLM] ConnectionError — trying to restart Ollama… ({e})")
        if ensure_ollama_running():
            resp = requests.post(endpoint, json=payload, timeout=timeout)
            resp.raise_for_status()
            msg = resp.json().get("message", {})
            return {
                "content":    (msg.get("content") or "").strip(),
                "tool_calls": msg.get("tool_calls") or [],
            }
        raise RuntimeError(f"Cannot connect to Ollama at {url}.")
    except requests.exceptions.Timeout:
        raise RuntimeError("Ollama request timed out after 120 s.")
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}")


def call_llm_text(
    prompt:  str,
    system:  str | None = None,
    model:   str | None = None,
    timeout: int = 120,
) -> str:
    """
    Simple text-only generation (no tools).
    Used by planner, executor, error_handler, code_helper, dev_agent.
    """
    url, default_model = get_llm_settings()
    provider           = get_llm_provider()
    m                  = model or default_model

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    if provider in ("xkiro", "openai"):
        result = _openai_call(messages, None, timeout, url, m)
        return result["content"]

    # Ollama
    endpoint = f"{url}/api/chat"
    payload  = {
        "model":    m,
        "messages": messages,
        "stream":   False,
        "keep_alive": -1,
        "options":  {"num_predict": 600},
    }
    try:
        resp = requests.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
        return (resp.json().get("message", {}).get("content") or "").strip()
    except requests.exceptions.ConnectionError:
        if ensure_ollama_running():
            resp = requests.post(endpoint, json=payload, timeout=timeout)
            resp.raise_for_status()
            return (resp.json().get("message", {}).get("content") or "").strip()
        raise RuntimeError(f"Cannot connect to Ollama at {url}.")
    except Exception as e:
        raise RuntimeError(f"LLM text call failed: {e}")


def _stream_openai(
    messages: list,
    tools:    list | None,
    timeout:  int,
) -> Generator[dict, None, None]:
    """Streaming for xkiro / OpenAI-compatible servers (SSE)."""
    url, model = get_llm_settings()
    endpoint   = f"{url}/chat/completions"

    payload: dict = {
        "model":      model,
        "messages":   messages,
        "stream":     True,
        "max_tokens": 150,
    }
    if tools:
        payload["tools"]       = tools
        payload["tool_choice"] = "auto"

    try:
        with requests.post(
            endpoint, json=payload, headers=_auth_headers(),
            timeout=timeout, stream=True,
        ) as resp:
            resp.raise_for_status()
            full_content = ""
            buf          = ""
            tc_fragments: dict[int, dict] = {}

            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choice = chunk.get("choices", [{}])[0]
                delta  = choice.get("delta", {})
                text   = delta.get("content") or ""

                full_content += text
                buf          += text

                while True:
                    m = _SENT_END.search(buf)
                    if not m:
                        break
                    sentence = buf[: m.start() + 1].strip()
                    buf      = buf[m.end():]
                    if sentence:
                        yield {"type": "sentence", "text": sentence}

                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    if idx not in tc_fragments:
                        tc_fragments[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                    frag = tc_fragments[idx]
                    frag["id"] = frag["id"] or tc.get("id", "")
                    fn = tc.get("function", {})
                    frag["function"]["name"]      += fn.get("name") or ""
                    frag["function"]["arguments"] += fn.get("arguments") or ""

                finish = choice.get("finish_reason")
                if finish in ("stop", "tool_calls", "length"):
                    break

            if buf.strip():
                yield {"type": "sentence", "text": buf.strip()}

            tool_calls: list = []
            for idx in sorted(tc_fragments):
                frag = tc_fragments[idx]
                args = frag["function"]["arguments"]
                try:
                    args = json.loads(args)
                except Exception:
                    pass
                tool_calls.append({
                    "id":       frag["id"],
                    "function": {"name": frag["function"]["name"], "arguments": args},
                })

            yield {
                "type":       "done",
                "content":    full_content.strip(),
                "tool_calls": tool_calls,
            }

    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"Cannot reach SARA LLM endpoint at {url}.\n"
            "Check your internet connection and api_keys.json."
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("SARA LLM stream timed out.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"SARA LLM HTTP error {e.response.status_code}: {e.response.text[:200]}"
        )
    except Exception as e:
        raise RuntimeError(f"SARA LLM stream failed: {e}")


def call_llm_stream(
    messages: list,
    tools:    list | None = None,
    timeout:  int = 120,
) -> Generator[dict, None, None]:
    """
    Streaming chat.  Routes to Xkiro, OpenAI-compatible, or Ollama.

    Yields:
        {"type": "sentence", "text": str}
        {"type": "done", "content": str, "tool_calls": list}
    """
    provider = get_llm_provider()

    if provider in ("xkiro", "openai"):
        yield from _stream_openai(messages, tools, timeout)
        return

    # ── Ollama ──────────────────────────────────────────────────────────────
    url, model = get_llm_settings()
    endpoint   = f"{url}/api/chat"

    payload: dict = {
        "model":      model,
        "messages":   messages,
        "stream":     True,
        "keep_alive": -1,
        "options":    {"num_predict": 150, "num_gpu": 99},
    }
    if tools:
        payload["tools"] = tools

    def _do_stream() -> Generator[dict, None, None]:
        with requests.post(endpoint, json=payload, timeout=timeout, stream=True) as resp:
            resp.raise_for_status()
            full_content = ""
            tool_calls:  list = []
            buf          = ""

            for raw in resp.iter_lines():
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg   = chunk.get("message", {})
                delta = msg.get("content") or ""

                full_content += delta
                buf          += delta

                while True:
                    m = _SENT_END.search(buf)
                    if not m:
                        break
                    sentence = buf[: m.start() + 1].strip()
                    buf      = buf[m.end():]
                    if sentence:
                        yield {"type": "sentence", "text": sentence}

                tc = msg.get("tool_calls")
                if tc:
                    tool_calls.extend(tc)

                if chunk.get("done"):
                    if buf.strip():
                        yield {"type": "sentence", "text": buf.strip()}
                    yield {
                        "type":       "done",
                        "content":    full_content.strip(),
                        "tool_calls": tool_calls,
                    }
                    return

    try:
        yield from _do_stream()
    except requests.exceptions.ConnectionError as e:
        print(f"[LLM] Stream ConnectionError — trying to restart Ollama… ({e})")
        if ensure_ollama_running():
            yield from _do_stream()
            return
        raise RuntimeError(f"Cannot connect to Ollama at {url}.")
    except requests.exceptions.Timeout:
        raise RuntimeError("Ollama stream timed out.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Ollama HTTP error: {e.response.status_code}")
    except Exception as e:
        raise RuntimeError(f"LLM stream failed: {e}")
