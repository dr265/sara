"""LLM integration for EduLearner AI.

Reuses the shared SARA LLM brain (``LLM_API_KEY`` / ``LLM_BASE_URL`` /
``LLM_MODEL`` from backend/.env, OpenAI-compatible chat completions). Every
feature works fully offline with deterministic logic; when a key is present
the LLM *enhances* grounded answers instead of replacing the evidence.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger("sara.edulearner")

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = (os.environ.get("LLM_BASE_URL") or "https://api.xkiro.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL") or "deepseek/deepseek-v4-flash"
TIMEOUT = 45
MAX_RETRIES = 2


class LLMClient:
    """OpenAI-compatible chat client with graceful, honest degradation.

    The configured ``LLM_MODEL`` is tried first; if the gateway rejects it
    (premium model without balance, retired model id) the client walks a
    fallback chain of models proven to work with the same key. The model that
    actually answered is exposed as :attr:`active_model` so the UI never
    claims a model it did not use.
    """

    FALLBACK_MODELS = (
        "qwen/qwen3.5-397b-a17b:free",   # verified free-tier on xKiro
        "qwen/qwen3.6-35b-a3b:free",
        "cohere/tiny-aya-global",
    )

    def __init__(self, api_key: str = "", base_url: str = "", model: str = "") -> None:
        self.api_key = api_key if api_key else LLM_API_KEY
        self.base_url = (base_url or LLM_BASE_URL).rstrip("/")
        self.model = model or LLM_MODEL
        self.active_model = ""

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def candidates(self) -> list:
        preferred = os.environ.get("EDULEARNER_LLM_MODEL", "").strip()
        chain = [preferred, self.model] + list(self.FALLBACK_MODELS)
        seen, ordered = set(), []
        for item in chain:
            if item and item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    def chat(self, messages, temperature: float = 0.4, max_tokens: int = 900) -> str:
        """Call the chat endpoint with a model fallback chain.

        Raises RuntimeError only when every candidate model failed; callers
        fall back to the deterministic local answer, so the LLM can never
        silently fabricate.
        """

        if not self.api_key:
            raise RuntimeError("LLM_API_KEY is not configured")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error = ""
        for model in self.candidates():
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    response = requests.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers, json=payload, timeout=TIMEOUT,
                    )
                    if response.status_code == 200:
                        content = response.json()["choices"][0]["message"]["content"]
                        if content and content.strip():
                            if model != self.model:
                                logger.info("edulearner LLM fallback model in use: %s", model)
                            self.active_model = model
                            self.model = model
                            return content.strip()
                        last_error = f"{model}: empty response"
                        break
                    last_error = f"{model}: HTTP {response.status_code} {response.text[:160]}"
                    # Auth/premium rejection will not improve on retry.
                    if response.status_code in (401, 402, 403, 404):
                        break
                except Exception as exc:  # noqa: BLE001 - retried below
                    last_error = f"{model}: {type(exc).__name__}"
                logger.debug("LLM attempt %d/%d failed on %s: %s",
                             attempt, MAX_RETRIES, model, last_error)
        raise RuntimeError(f"LLM unavailable across {len(self.candidates())} models: {last_error}")


_client: "LLMClient | None" = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
