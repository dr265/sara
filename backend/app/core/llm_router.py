"""Centralized LLM provider abstraction with automatic failover.

Every SARA agent (chat, voice, WhatsApp, GameForge, email, scraping,
frontend/backend dev agents, edulearner, ...) routes LLM traffic through this
module instead of talking to a single provider directly.

Providers are discovered from backend/.env (key NAMES only — values are never
logged). Order = priority:

    1. LLM_API_KEY / LLM_BASE_URL / LLM_MODEL           (xKiro by default)
    2. ANYMODEL_API_KEY / ANYMODEL_BASE_URL / ANYMODEL_MODEL
    3. DEEPSEEK_API_KEY                                  (api.deepseek.com)
    4. OPENAI_API_KEY                                    (api.openai.com)

Per request the router walks the chain:
  * transient errors (429/500/502/503/504, timeouts, connection errors)
    -> retry the same provider with linear backoff;
  * hard errors (401/403/402, unknown model, billing) -> mark the provider
    unhealthy for UNHEALTHY_TTL seconds and move to the next one;
  * model not found (HTTP 400/404 mentioning "model") -> the router auto-falls
    back to the provider's cheapest working free model when the configured
    model is premium/blocked.

Every provider is validated by a real /models probe (cached) — the router
never assumes credits remain; availability is inferred from API responses.

Usage:
    from app.core.llm_router import llm_chat, get_llm_client, health_snapshot
    reply = await llm_chat(messages, max_tokens=500)          # async
    reply = llm_chat_sync(messages)                            # sync
    client = get_llm_client()                                  # OpenAI SDK shim
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("sara.llm.router")

# ---------------------------------------------------------------------------
# Provider chain discovery
# ---------------------------------------------------------------------------

PROVIDER_PRESETS: Dict[str, Dict[str, str]] = {
    # Native Google Gemini API (generateContent), not an OpenAI-compatible
    # gateway. `kind="gemini"` switches the request/response translation on.
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta", "default_model": "gemini-flash-latest"},
    "xkiro": {"base_url": "https://api.xkiro.com/v1", "default_model": ""},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "default_model": "openai/gpt-4o-mini"},
    "hashneuron": {"base_url": "https://hashneuron.space/v1", "default_model": "glm-5.3-flash"},
    "anymodel": {"base_url": "https://anymodel.org/v1", "default_model": ""},
    "selora": {"base_url": "https://api.selora.lol/v1", "default_model": "glm-5.3-flash"},
    "codecraft": {"base_url": "https://codecraftapi.com/v1", "default_model": "gemini-3.7-flash"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "default_model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "default_model": "gpt-4o-mini"},
    "axqua": {"base_url": "https://axqua.com/v1", "default_model": ""},
    "apiqik": {"base_url": "https://api.apiqik.com/v1", "default_model": ""},
    "cleanapis": {"base_url": "https://cleanapis.com/v1", "default_model": ""},
    "apinex": {"base_url": "https://api.apinex.bond/v1", "default_model": ""},
    "manifest": {"base_url": "https://app.manifest.build/v1", "default_model": ""},
}

# Free/cheap fallback models per provider — used when the configured model is
# premium-blocked (403) or exhausted (402). Discovered live from /models.
FREE_MODEL_FALLBACKS: Dict[str, List[str]] = {
    # Probed live 2026-10-22 on the project key: gemini-flash-latest answers;
    # gemini-2.5-flash is closed to new accounts (HTTP 404).
    "gemini": [
        "gemini-flash-latest",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
    ],
    # Probed live 2026-09-30: xkiro 403s on every premium model
    # ("pay-as-you-go premium"), but qwen3.7-flash:free genuinely answers.
    "xkiro": [
        "qwen/qwen3.7-flash:free",
        "qwen/qwen3.5-397b-a17b:free",
        "qwen/qwen3.6-35b-a3b:free",
        "cohere/command-r7b-12-2024",
        "cohere/tiny-aya-global",
    ],
    # HashNeuron: verified live 2026-09-26 — these answer with zero balance;
    # every other model on the account returns 402 credit_balance_exhausted.
    "hashneuron": [
        "glm-5.3-flash",
        "qwen3-30b-a3b",
        "space-bunny-alpha",
    ],
    "anymodel": [],
    # OpenRouter: benchmarked live 2026-09-30 on a 4-turn booking call —
    # gpt-4.1-mini 1.48s, gpt-4.1 1.57s, gpt-4o-mini 1.99s, all 8/8 spoken
    # quality. Kept as the failover ladder because a slower model still beats
    # silence on a live call.
    "openrouter": [
        "openai/gpt-4.1-mini",
        "openai/gpt-4o-mini",
        "google/gemini-2.5-flash",
        "meta-llama/llama-3.3-70b-instruct",
        "meta-llama/llama-3.1-8b-instruct",
    ],
    # Selora: speed-tested 2026-09-30 — glm-5.3-flash answered booking prompts
    # in 2.7-3.3 s; haiku as backup.
    "selora": [
        "glm-5.3-flash",
        "claude-haiku-4-5",
        "kimi-k3",
    ],
    # CodeCraft: verified working 2026-09-30 (~4-8 s); fallback chain picked
    # from its live /models listing.
    "codecraft": [
        "gemini-3.7-flash",
        "gemma-2-2b",
        "deepseek-v4-flash-0731",
    ],
    "deepseek": [],
    "openai": [],
    # Newer gateways: model lists are discovered live from /models, so the
    # router falls back to the provider's own catalogue when a configured
    # model is blocked or exhausted.
    "axqua": [],
    "apiqik": [],
    "cleanapis": [],
    "apinex": [],
    "manifest": [],
}


@dataclass
class ProviderState:
    name: str
    base_url: str
    api_key: str
    configured_model: str
    fallback_models: List[str] = field(default_factory=list)
    #: "openai" for OpenAI-compatible /chat/completions gateways, "gemini" for
    #: Google's native generateContent API.
    kind: str = "openai"
    healthy: bool = True
    last_error: str = ""
    last_checked: float = 0.0
    disabled_until: float = 0.0
    #: When this provider was last quarantined for being out of credits. Used to
    #: decide whether the long credit quarantine has genuinely expired.
    _quarantined_at: float = 0.0

    def is_available(self) -> bool:
        return self.healthy and time.time() >= self.disabled_until


def _build_chain() -> List[ProviderState]:
    chain: List[ProviderState] = []
    seen_urls: set[str] = set()

    def add(name: str, key: str, base_url: str, model: str, fallbacks: List[str],
            kind: str = "openai") -> None:
        if not key or not base_url:
            return
        base_url = base_url.rstrip("/")
        if base_url in seen_urls:  # avoid duplicate entries for the same host
            return
        seen_urls.add(base_url)
        chain.append(
            ProviderState(
                name=name,
                base_url=base_url,
                api_key=key,
                configured_model=model,
                fallback_models=fallbacks,
                kind=kind,
            )
        )

    # Gemini goes first when its key is present: the project owner configured it
    # explicitly, and the failover loop below still protects the call if Google
    # rate-limits or errors.
    add(
        "gemini",
        os.getenv("GEMINI_API_KEY", ""),
        os.getenv("GEMINI_BASE_URL", PROVIDER_PRESETS["gemini"]["base_url"]),
        os.getenv("GEMINI_MODEL", PROVIDER_PRESETS["gemini"]["default_model"]),
        FREE_MODEL_FALLBACKS.get("gemini", []),
        kind="gemini",
    )

    key = os.getenv("LLM_API_KEY", "")
    base = os.getenv("LLM_BASE_URL", "")
    model = os.getenv("LLM_MODEL", "")
    # Identify which preset the configured base_url actually is so the right
    # fallback model list rides along (e.g. LLM_BASE_URL pointing at Selora
    # must get Selora's fallbacks, not xkiro's).
    name = "xkiro"
    host = base.split("//")[1].split("/")[0] if base and "//" in base else ""
    for preset in PROVIDER_PRESETS:
        purl = PROVIDER_PRESETS[preset]["base_url"]
        phost = purl.split("//")[1].split("/")[0]
        if host and (host == phost or host.endswith("." + phost) or phost.endswith("." + host)):
            name = preset
            break
    add(name, key, base or PROVIDER_PRESETS["xkiro"]["base_url"], model,
        FREE_MODEL_FALLBACKS.get(name, []))

    add(
        "xkiro",
        os.getenv("XKIRO_API_KEY", ""),
        os.getenv("XKIRO_BASE_URL", PROVIDER_PRESETS["xkiro"]["base_url"]),
        os.getenv("XKIRO_MODEL", ""),
        FREE_MODEL_FALLBACKS.get("xkiro", []),
    )
    add(
        "hashneuron",
        os.getenv("HASHNEURON_API_KEY", ""),
        os.getenv("HASHNEURON_BASE_URL", PROVIDER_PRESETS["hashneuron"]["base_url"]),
        os.getenv("HASHNEURON_MODEL", ""),
        FREE_MODEL_FALLBACKS.get("hashneuron", []),
    )
    # --- Verified-live providers (probed 2026-09-30) --------------------------
    # The chain below is ordered by MEASURED latency on a real completion, so
    # a live call fails over to something fast instead of stalling on a dead
    # host. Measured: selora/claude-haiku 1.36s, selora/glm 1.45s,
    # apinex/deepseek-v4-flash 1.50s, xkiro qwen3.7-flash:free 3.06s,
    # cleanapis/gemini-3.7-flash 3.73s, codecraft/gemini-3.7-flash 4.47s.
    add(
        "apinex",
        os.getenv("APINEX_API_KEY", ""),
        os.getenv("APINEX_BASE_URL", PROVIDER_PRESETS["apinex"]["base_url"]),
        "deepseek-v4-flash",
        ["deepseek-v4.1-flash", "deepseek-v4-flash"],
    )
    add(
        "cleanapis",
        os.getenv("CLEANAPIS_API_KEY", ""),
        os.getenv("CLEANAPIS_BASE_URL", PROVIDER_PRESETS["cleanapis"]["base_url"]),
        "gemini-3.7-flash",
        ["gemini-3.6-flash", "gemini-3.7-flash"],
    )
    add(
        "anymodel",
        os.getenv("ANYMODEL_API_KEY", ""),
        os.getenv("ANYMODEL_BASE_URL", PROVIDER_PRESETS["anymodel"]["base_url"]),
        os.getenv("ANYMODEL_MODEL", ""),
        FREE_MODEL_FALLBACKS.get("anymodel", []),
    )
    add(
        "selora",
        os.getenv("SELORA_API_KEY", "") or os.getenv("LLM_API_KEY", ""),
        os.getenv("SELORA_BASE_URL", PROVIDER_PRESETS["selora"]["base_url"]),
        os.getenv("SELORA_MODEL", ""),
        FREE_MODEL_FALLBACKS.get("selora", []),
    )
    add(
        "codecraft",
        os.getenv("CODECRAFT_API_KEY", ""),
        os.getenv("CODECRAFT_BASE_URL", PROVIDER_PRESETS["codecraft"]["base_url"]),
        os.getenv("CODECRAFT_MODEL", ""),
        FREE_MODEL_FALLBACKS.get("codecraft", []),
    )
    add(
        "deepseek",
        os.getenv("DEEPSEEK_API_KEY", ""),
        PROVIDER_PRESETS["deepseek"]["base_url"],
        os.getenv("DEEPSEEK_MODEL", PROVIDER_PRESETS["deepseek"]["default_model"]),
        [],
    )
    add(
        "openai",
        os.getenv("OPENAI_API_KEY", ""),
        PROVIDER_PRESETS["openai"]["base_url"],
        os.getenv("OPENAI_MODEL", PROVIDER_PRESETS["openai"]["default_model"]),
        [],
    )
    # Additional OpenAI-compatible gateways. Each is skipped automatically when
    # its key is absent, so adding one is purely additive — it lengthens the
    # failover chain without changing behaviour for existing deployments.
    for extra in ("axqua", "apiqik", "cleanapis", "apinex"):
        add(
            extra,
            os.getenv(f"{extra.upper()}_API_KEY", ""),
            os.getenv(f"{extra.upper()}_BASE_URL", PROVIDER_PRESETS[extra]["base_url"]),
            os.getenv(f"{extra.upper()}_MODEL", PROVIDER_PRESETS[extra]["default_model"]),
            FREE_MODEL_FALLBACKS.get(extra, []),
        )
    add(
        "manifest",
        os.getenv("MANIFEST_API_KEY", ""),
        os.getenv("MANIFEST_BASE_URL", PROVIDER_PRESETS["manifest"]["base_url"]),
        os.getenv("MANIFEST_MODEL", PROVIDER_PRESETS["manifest"]["default_model"]),
        FREE_MODEL_FALLBACKS.get("manifest", []),
    )
    return chain


_chain_lock = threading.Lock()
_chain: Optional[List[ProviderState]] = None
_model_cache: Dict[str, tuple[float, List[str]]] = {}
_MODELS_TTL = 600          # /models listing cached 10 minutes
_UNHEALTHY_TTL = 120       # a transiently failing provider is retried after 2 min
#: A provider that answered "out of credits" will still be out of credits in
#: two minutes. Quarantining it for the TTL above meant every single request
#: paid a doomed call to it first — exactly the "agent ran out of credits"
#: failure mode this router exists to prevent. Balance errors get their own,
#: much longer TTL; override with LLM_QUOTA_TTL_S (0 disables the quarantine).
_QUOTA_TTL = float(os.getenv("LLM_QUOTA_TTL_S", "3600"))
_MAX_ATTEMPTS = 2          # per provider for transient errors


def _backoff_sleep(attempt: int) -> None:
    """Exponential backoff with jitter: ~1s, ~2s, ~4s, capped at 8s.

    The previous fixed ``2 * (attempt + 1)`` sleep gave every provider the same
    rhythm, so a rate-limited host was retried in lockstep by concurrent
    requests and stayed rate-limited. Jitter de-synchronises the retries.
    """
    import random

    ceiling = min(8.0, 0.75 * (2 ** attempt))
    time.sleep(random.uniform(ceiling * 0.5, ceiling))


def _get_chain() -> List[ProviderState]:
    global _chain
    with _chain_lock:
        if _chain is None:
            _chain = _build_chain()
            names = [f"{p.name}({p.configured_model or 'default'})" for p in _chain]
            log.info("LLM failover chain: %s", " -> ".join(names) if names else "EMPTY")
        return _chain


def reload_chain() -> None:
    """Re-read env (after .env changes) — exposed for admin endpoints/tests."""
    global _chain
    with _chain_lock:
        _chain = _build_chain()


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

TRANSIENT_STATUS = {429, 500, 502, 503, 504}
HARD_STATUS = {401, 402, 403}

#: Phrases that mean "your account has no money", not "this call was wrong".
#: 402 is the obvious one, but several gateways answer 400/403/429 for the same
#: condition, and each one was previously treated as a plain hard error and
#: retried two minutes later -- so an empty wallet was re-dialled on every single
#: request. Credit exhaustion is now its own class: quarantine it for an hour.
QUOTA_PHRASES = (
    "insufficient credit", "insufficient balance", "credit_balance_exhausted",
    "balance has run out", "out of credits", "exceeded your current quota",
    "quota exceeded", "billing", "insufficient funds", "payment required",
    "add credits", "top it up", "top up", "no credits", "credit limit",
)


def _is_quota_exhausted(status: int, body: str) -> bool:
    """True when the provider is refusing us for money, not for the request."""
    if status == 402:
        return True
    low = (body or "").lower()
    return any(phrase in low for phrase in QUOTA_PHRASES)


def _classify(status: int, body: str) -> str:
    """Return 'transient' | 'hard' | 'model' | 'quota' for an HTTP error."""
    low = (body or "").lower()
    # Checked first: a 403 that says "insufficient credit" is a wallet problem,
    # not a permissions problem, and must not be retried on the short TTL.
    if _is_quota_exhausted(status, body):
        return "quota"
    if status == 400 and ("model" in low and ("not found" in low or "does not exist" in low or "invalid" in low)):
        return "model"
    if status == 404 and "model" in low:
        return "model"
    if status in TRANSIENT_STATUS:
        return "transient"
    if status in HARD_STATUS:
        return "hard"
    return "hard"


def _short_error(status: int, body: str) -> str:
    """A short, secret-free error string for logs."""
    return f"HTTP {status}: {(body or '')[:160].replace(chr(10), ' ')}"


# ---------------------------------------------------------------------------
# Model discovery (availability inferred from the provider itself)
# ---------------------------------------------------------------------------

def _auth_headers(provider: ProviderState) -> Dict[str, str]:
    """Auth headers for a provider.

    Nearly every gateway here is OpenAI-compatible and expects a bearer token;
    Manifest instead uses an `x-api-key` header, so it is special-cased rather
    than being given a token it would reject.
    """
    if provider.name == "manifest":
        return {"x-api-key": provider.api_key, "Content-Type": "application/json"}
    if provider.kind == "gemini":
        return {"x-goog-api-key": provider.api_key}
    return {"Authorization": f"Bearer {provider.api_key}"}


def _list_models(provider: ProviderState) -> List[str]:
    cached = _model_cache.get(provider.name)
    now = time.time()
    if cached and now - cached[0] < _MODELS_TTL:
        return cached[1]
    try:
        resp = httpx.get(
            f"{provider.base_url}/models",
            headers=_auth_headers(provider),
            params={"pageSize": 200} if provider.kind == "gemini" else None,
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            if provider.kind == "gemini":
                ids = [
                    str(m.get("name", "")).replace("models/", "")
                    for m in (data.get("models") or []) if m.get("name")
                ]
            else:
                ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            _model_cache[provider.name] = (now, ids)
            return ids
    except Exception as exc:  # noqa: BLE001
        log.warning("Model discovery failed for %s: %s", provider.name, type(exc).__name__)
    _model_cache[provider.name] = (now, [])
    return []


def _candidate_models(provider: ProviderState, requested_model: str) -> List[str]:
    """Models to try, in order: requested -> configured -> known free fallbacks.

    A caller may pass a model belonging to a different provider (the voice
    engine passes its global LLM_MODEL). Gemini must not burn a doomed request
    on an OpenAI-style id, so only Gemini-family names are accepted there.
    """
    candidates: List[str] = []
    if provider.kind == "gemini":
        if requested_model and requested_model.startswith(("gemini", "gemma")):
            candidates.append(requested_model)
        if provider.configured_model and provider.configured_model not in candidates:
            candidates.append(provider.configured_model)
    else:
        if requested_model:
            candidates.append(requested_model)
        elif provider.configured_model:
            candidates.append(provider.configured_model)
    for fb in provider.fallback_models:
        if fb not in candidates:
            candidates.append(fb)
    return candidates


# ---------------------------------------------------------------------------
# Core call
# ---------------------------------------------------------------------------

class _GeminiResponse:
    """httpx.Response shim that normalises Gemini into the OpenAI shape.

    The failover loop only understands `choices[0].message.content` and raw
    status/content on errors, so the translation lives here and the rest of the
    router stays provider-agnostic.
    """

    def __init__(self, response: Any) -> None:
        self._response = response
        self.status_code = response.status_code
        self.content = response.content

    def json(self) -> Dict[str, Any]:
        data = self._response.json()
        candidates = data.get("candidates") or []
        text = ""
        if candidates:
            parts = ((candidates[0].get("content") or {}).get("parts") or [])
            text = "".join(
                str(p.get("text") or "")
                for p in parts
                if isinstance(p, dict) and not p.get("thought")
            )
        return {"choices": [{"message": {"content": text}}]}


def _post_gemini(provider: ProviderState, model: str, messages: List[Dict[str, Any]],
                 *, max_tokens: int, temperature: Optional[float], timeout: float):
    """One native Gemini generateContent request.

    `maxOutputTokens` gets a floor: on Gemini 3 flash the model's reasoning
    spends part of the budget, and a voice turn asking for 160 tokens came back
    with an empty candidate. The floor leaves room for reasoning + the spoken
    sentence without changing the reply length.
    """
    system_chunks: List[str] = []
    contents: List[Dict[str, Any]] = []
    for message in messages or []:
        role = str(message.get("role") or "user")
        raw = message.get("content")
        if isinstance(raw, list):
            text = " ".join(
                str(p.get("text") or "") if isinstance(p, dict) else str(p)
                for p in raw
            )
        else:
            text = str(raw or "")
        if role == "system":
            system_chunks.append(text)
            continue
        contents.append({
            "role": "model" if role in ("assistant", "model") else "user",
            "parts": [{"text": text}],
        })
    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]

    generation: Dict[str, Any] = {"maxOutputTokens": max(int(max_tokens), 512)}
    if temperature is not None:
        generation["temperature"] = temperature
    payload: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation,
    }
    if system_chunks:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_chunks)}]}

    response = httpx.post(
        f"{provider.base_url}/models/{model}:generateContent",
        headers={
            "x-goog-api-key": provider.api_key,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    return _GeminiResponse(response)


def _post_chat(provider: ProviderState, model: str, messages: List[Dict[str, Any]],
               *, max_tokens: int, temperature: Optional[float], timeout: float,
               stream: bool = False):
    if provider.kind == "gemini":
        if stream:
            raise ValueError("streaming is not supported for the native Gemini provider")
        return _post_gemini(
            provider, model, messages,
            max_tokens=max_tokens, temperature=temperature, timeout=timeout,
        )
    headers = _auth_headers(provider)
    headers["Content-Type"] = "application/json"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if stream:
        payload["stream"] = True
        return httpx.stream(
            "POST", f"{provider.base_url}/chat/completions",
            headers=headers, json=payload, timeout=timeout,
        )
    return httpx.post(
        f"{provider.base_url}/chat/completions",
        headers=headers, json=payload, timeout=timeout,
    )


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def _local_fallback(messages: List[Dict[str, Any]]) -> str:
    """Last-resort, zero-dependency reply so a run NEVER dies of API failure.

    Every provider in the chain can be down, over quota or DNS-dead at the same
    time. Previously that propagated a ``RuntimeError`` into whichever agent
    called the router, and an agent whose only brain had run out of credits
    simply terminated mid-task. This returns an honest, usable string instead:
    the agent finishes its run, reports the degradation to the user, and keeps
    its non-LLM tools (files, browser, desktop, memory) fully operational.

    It is deliberately transparent -- it never fabricates an answer.
    """
    user = _last_user_text(messages).strip()
    tail = user[-200:] if user else ""
    return (
        "[offline] Every configured AI provider is currently unavailable "
        "(out of credits, rate-limited or offline), so this response was "
        "produced without a language model. My non-AI tools are still "
        "operational."
        + (f" Your last request was: {tail!r}" if tail else "")
    )


def llm_chat_sync(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: Optional[float] = 0.4,
    timeout: float = 120.0,
    strict: bool = False,
) -> str:
    """Blocking chat completion with full failover.

    Returns reply text. If every provider fails, a local fallback reply is
    returned so no agent is ever killed by an upstream outage. Pass
    ``strict=True`` (used by tests and health checks) to get the RuntimeError
    instead of the graceful reply.
    """
    chain = [p for p in _get_chain() if p.is_available()]
    if not chain:
        # Everything is in quarantine. Ignore the SHORT TTLs once so a blip
        # recovers immediately -- but never the credit quarantine, since
        # retrying an empty wallet on every request is exactly what we prevent.
        now = time.time()
        for p in _get_chain():
            if p.healthy or _QUOTA_TTL <= 0 or (now - getattr(p, "_quarantined_at", 0.0)) > _QUOTA_TTL:
                p.disabled_until = 0.0
        chain = [p for p in _get_chain() if p.is_available()]
    if not chain:
        msg = ("No LLM provider available. Configure LLM_API_KEY/LLM_BASE_URL/"
               "LLM_MODEL (or XKIRO_API_KEY / SELORA_API_KEY / HASHNEURON_API_KEY "
               "/ APINEX_API_KEY / MANIFEST_API_KEY) in backend/.env.")
        if strict:
            raise RuntimeError(msg)
        log.error(msg)
        return _local_fallback(messages)

    errors: List[str] = []
    for provider in chain:
        candidates = _candidate_models(provider, model or "")
        last_err = "no attempt"
        worst_kind = "hard"
        for cand in candidates:
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    resp = _post_chat(provider, cand, messages,
                                      max_tokens=max_tokens, temperature=temperature,
                                      timeout=timeout)
                    if resp.status_code == 200:
                        data = resp.json()
                        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
                        if content:
                            provider.healthy = True
                            provider.last_error = ""
                            provider.last_checked = time.time()
                            provider.disabled_until = 0.0
                            return str(content).strip()
                        last_err = "empty content"
                        worst_kind = "transient"
                        continue
                    body = resp.content.decode("utf-8", errors="replace")
                    last_err = _short_error(resp.status_code, body)
                    kind = _classify(resp.status_code, body)
                    if kind == "quota":
                        # Money problem: stop dialling this provider immediately,
                        # do not burn the remaining candidate models on it.
                        worst_kind = "quota"
                        break
                    if kind == "transient":
                        worst_kind = "transient"
                        if attempt < _MAX_ATTEMPTS - 1:
                            _backoff_sleep(attempt)
                            continue
                        break
                    if kind == "model":
                        break  # try next candidate model on this provider
                    worst_kind = "hard"
                    break
                except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
                    last_err = f"{type(exc).__name__}: {str(exc)[:120]}"
                    worst_kind = "transient"
                    if attempt < _MAX_ATTEMPTS - 1:
                        _backoff_sleep(attempt)
                        continue
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{type(exc).__name__}: {str(exc)[:120]}"
                    break
            # If we reach here for this candidate, move to the next candidate
        # Provider exhausted for all candidates
        provider.healthy = False
        provider.last_error = last_err
        provider.last_checked = time.time()
        if worst_kind == "quota" and _QUOTA_TTL > 0:
            # Long quarantine: an empty wallet stays empty for a long time.
            provider._quarantined_at = time.time()
            provider.disabled_until = time.time() + _QUOTA_TTL
            log.warning("LLM provider %s OUT OF CREDITS - quarantined for %ss: %s",
                        provider.name, int(_QUOTA_TTL), last_err)
        else:
            provider.disabled_until = time.time() + _UNHEALTHY_TTL
            log.warning("LLM provider %s unhealthy: %s", provider.name, last_err)
        errors.append(f"{provider.name}: {last_err}")

    log.error("All LLM providers failed: %s", " | ".join(errors))
    if strict:
        raise RuntimeError("All LLM providers failed: " + " | ".join(errors))
    return _local_fallback(messages)


async def llm_chat(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: Optional[float] = 0.4,
    timeout: float = 120.0,
    strict: bool = False,
) -> str:
    """Async wrapper — runs the blocking failover chain in the default executor.

    The HTTP calls inside are sync httpx; this keeps call sites simple while
    never blocking the event loop.
    """
    import asyncio

    return await asyncio.to_thread(
        llm_chat_sync, messages,
        model=model, max_tokens=max_tokens, temperature=temperature,
        timeout=timeout, strict=strict,
    )


# ---------------------------------------------------------------------------
# OpenAI-SDK-compatible shim (drop-in for `OpenAI(base_url, api_key)` users)
# ---------------------------------------------------------------------------

class _FailoverCompletions:
    def __init__(self, client: "FailoverOpenAI") -> None:
        self._client = client

    def create(self, *, model: Optional[str] = None, messages=None,
               max_tokens: int = 1024, temperature: Optional[float] = 0.4,
               timeout: float = 120.0, **_: Any) -> Any:
        reply = llm_chat_sync(
            messages, model=model, max_tokens=max_tokens,
            temperature=temperature, timeout=timeout,
        )

        class _Msg:
            def __init__(self, content: str) -> None:
                self.content = content
                self.role = "assistant"

        class _Choice:
            def __init__(self, content: str) -> None:
                self.message = _Msg(content)
                self.finish_reason = "stop"

        class _Resp:
            def __init__(self, content: str, model_used: str) -> None:
                self.choices = [_Choice(content)]
                self.model = model_used

        return _Resp(reply, model or "failover")


class _FailoverChat:
    def __init__(self, client: "FailoverOpenAI") -> None:
        self.completions = _FailoverCompletions(client)


class FailoverOpenAI:
    """Mimics openai.OpenAI enough for `.chat.completions.create(...)` calls."""

    def __init__(self, base_url: str = "", api_key: str = "") -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.chat = _FailoverChat(self)


def get_llm_client() -> FailoverOpenAI:
    """Drop-in replacement for `OpenAI(base_url=settings.LLM_BASE_URL, api_key=...)`.

    Returns None-equivalent (raises 503-style RuntimeError upstream) only when
    no provider is configured at all.
    """
    chain = _get_chain()
    if not chain:
        raise RuntimeError("No LLM provider configured in backend/.env")
    return FailoverOpenAI()


# ---------------------------------------------------------------------------
# Health snapshot (for /api/llm/health)
# ---------------------------------------------------------------------------

def health_snapshot() -> Dict[str, Any]:
    chain = _get_chain()
    providers = []
    for p in chain:
        providers.append({
            "name": p.name,
            "base_url": p.base_url,
            "configured_model": p.configured_model or None,
            "healthy": p.is_available(),
            "last_error": p.last_error or None,
            "fallback_models": p.fallback_models,
            # Surfaced so an operator can see at a glance which gateways are
            # merely blipping and which are genuinely out of money.
            "out_of_credits": _is_quota_exhausted(0, p.last_error or ""),
            "retry_in_s": max(0, int(p.disabled_until - time.time())) or None,
        })
    return {
        "providers": providers,
        "any_available": any(p.is_available() for p in chain),
        "active": next((p.name for p in chain if p.is_available()), None),
        "live_count": sum(1 for p in chain if p.is_available()),
        "total_count": len(chain),
    }
