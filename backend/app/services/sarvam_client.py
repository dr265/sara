"""Sarvam AI client — the voice pipeline's speech provider.

Covers every Sarvam stage used by the SARA voice agent:

  * speech-to-text            POST /speech-to-text              (saaras)
  * speech-to-text-translate  POST /speech-to-text-translate    (saaras -> English)
  * text-to-speech            POST /text-to-speech              (bulbul, base64 wav)
  * translate                 POST /translate                   (mayura)
  * transliterate             POST /transliterate

Design rules (matching the rest of SARA):
  * API key comes from backend/.env only (SARVAM_API_KEY) — never hardcoded,
    never logged.
  * Structured errors: SarvamAuthError / SarvamRateLimitError / SarvamServerError /
    SarvamTimeout / SarvamBadResponse — callers can react precisely instead of
    string-matching exception text.
  * Retries: transient network errors and 5xx get exponential backoff;
    429 honors Retry-After; auth (401/403) fails immediately (no retry).
  * Every call logs latency + outcome with session/call context when provided.
  * Long texts are chunked on sentence boundaries for TTS so replies are never
    truncated; each chunk is synthesized separately and returned in order.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger("sara.voice.sarvam")

DEFAULT_BASE_URL = "https://api.sarvam.ai"


class SarvamError(Exception):
    """Base class for every Sarvam failure. `kind` is machine-readable."""

    kind = "error"

    def __init__(self, message: str, *, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail

    def safe_message(self) -> str:
        """Short, caller-safe description (no URLs, no keys, no stack details)."""
        return f"Sarvam {self.kind} error" + (f" (HTTP {self.status})" if self.status else "")


class SarvamAuthError(SarvamError):
    kind = "auth"


class SarvamRateLimitError(SarvamError):
    kind = "rate_limit"

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class SarvamServerError(SarvamError):
    kind = "server"


class SarvamTimeout(SarvamError):
    kind = "timeout"


class SarvamNetworkError(SarvamError):
    kind = "network"


class SarvamBadResponse(SarvamError):
    kind = "bad_response"


@dataclass
class Transcript:
    """STT result with the detected language when Sarvam reports one."""

    text: str
    language_code: Optional[str] = None
    language_probability: Optional[float] = None
    request_id: Optional[str] = None
    latency_ms: int = 0


@dataclass
class Speech:
    """TTS result — one wav segment per text chunk, in order."""

    segments: list[bytes] = field(default_factory=list)
    sample_rate: int = 22050
    latency_ms: int = 0

    @property
    def audio(self) -> bytes:
        """Single-segment convenience access (the common case)."""
        return self.segments[0] if self.segments else b""


# ---------------------------------------------------------------------------
# Text chunking for TTS
# ---------------------------------------------------------------------------

_SENTENCE_RE = re.compile(r"[^.!?।]+[.!?।]*\s*")


def chunk_text(text: str, limit: int = 480) -> list[str]:
    """Split text into <=limit-char chunks on sentence boundaries.

    Sarvam TTS models accept bounded input lengths; spoken replies from the
    agent are short, but knowledge answers can be long, so chunk defensively.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for part in _SENTENCE_RE.findall(text):
        part = part.strip()
        if not part:
            continue
        # A single over-long sentence gets hard-split.
        while len(part) > limit:
            cut = part.rfind(" ", 0, limit)
            cut = cut if cut > int(limit * 0.5) else limit
            piece, part = part[:cut].strip(), part[cut:].strip()
            if current:
                chunks.append(current)
                current = ""
            if piece:
                chunks.append(piece)
        if not part:
            continue
        if len(current) + len(part) + 1 <= limit:
            current = f"{current} {part}".strip()
        else:
            if current:
                chunks.append(current)
            current = part
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SarvamClient:
    """Async Sarvam API client built from environment configuration."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 2,
        stt_model: str = "saaras:v3",
        tts_model: str = "bulbul:v3",
        tts_speaker: str = "priya",
        tts_language: str = "en-IN",
        translate_model: str = "mayura:v1",
        transliterate_model: str = "interactive-transliteration-v1",
        tts_sample_rate: int = 22050,
        tts_chunk_chars: int = 480,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        # Hard ceiling across all attempts, so a slow provider can never hold a
        # call open past the point where the caller's webhook is abandoned.
        self.total_deadline = float(
            os.getenv("SARVAM_TOTAL_DEADLINE", str(max(8.0, timeout * (1 + self.max_retries))))
        )
        self.stt_model = stt_model
        self.tts_model = tts_model
        self.tts_speaker = tts_speaker
        self.tts_language = tts_language
        self.translate_model = translate_model
        self.transliterate_model = transliterate_model
        self.tts_sample_rate = tts_sample_rate
        self.tts_chunk_chars = tts_chunk_chars
        self._client: httpx.AsyncClient | None = None

    # -- construction --------------------------------------------------------

    @classmethod
    def from_env(cls) -> "SarvamClient":
        return cls(
            api_key=os.getenv("SARVAM_API_KEY", ""),
            base_url=os.getenv("SARVAM_BASE_URL", DEFAULT_BASE_URL),
            # A 60 s TTS timeout is unusable for a live phone call: Twilio's
            # whole webhook budget is 15 s, so any render slower than that
            # guarantees "an application error has occurred". Callers get a
            # fast, honest fallback (<Say>) instead of dead air. Raise it via
            # SARVAM_TIMEOUT only if your provider is reliably fast.
            timeout=float(os.getenv("SARVAM_TIMEOUT", "12")),
            max_retries=int(os.getenv("SARVAM_MAX_RETRIES", "2")),
            stt_model=os.getenv("SARVAM_STT_MODEL", "saaras:v3"),
            tts_model=os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"),
            tts_speaker=os.getenv("SARVAM_TTS_SPEAKER", "priya"),
            tts_language=os.getenv("SARVAM_TTS_LANGUAGE", "en-IN"),
            translate_model=os.getenv("SARVAM_TRANSLATE_MODEL", "mayura:v1"),
            tts_sample_rate=int(os.getenv("SARVAM_TTS_SAMPLE_RATE", "22050")),
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # NOTE: no default Content-Type here. httpx merges client headers
            # *underneath* per-request headers, so a client-level
            # "Content-Type: application/json" would still be sent on multipart
            # requests (and stripping it from the per-request dict does not help,
            # because the merge puts the client value back). Every STT call
            # then failed with `body.file : Field required`. Letting httpx derive
            # the correct Content-Type — with its multipart boundary — per
            # request is the fix.
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"api-subscription-key": self.api_key},
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- transport with retry/backoff ----------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        files: dict | None = None,
        data: dict | None = None,
        operation: str = "",
        context: dict | None = None,
    ) -> dict:
        """POST/GET with retries. Raises the precise Sarvam*Error on failure."""
        if not self.api_key:
            raise SarvamAuthError(
                "SARVAM_API_KEY is not configured — set it in backend/.env",
                status=None,
            )
        client = self._ensure_client()
        ctx = context or {}
        attempt = 0
        last_error: SarvamError | None = None
        # Wall-clock budget for the whole call including retries.
        started_all = time.perf_counter()

        while attempt <= self.max_retries:
            attempt += 1
            started = time.perf_counter()
            try:
                kwargs: dict[str, Any] = {}
                if json_body is not None:
                    kwargs["json"] = json_body
                if files is not None:
                    # Multipart: pass files straight through. The client no
                    # longer pins a Content-Type, so httpx generates the correct
                    # one (with boundary) for this request on its own.
                    kwargs["files"] = files
                    kwargs["data"] = data or {}
                response = await client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                last_error = SarvamTimeout(f"Sarvam {operation} timed out after {self.timeout}s")
                log.warning(
                    "sarvam.timeout op=%s attempt=%d latency_ms=%d err=%s %s",
                    operation, attempt, int((time.perf_counter() - started) * 1000), exc,
                    {k: v for k, v in ctx.items() if k in ("call_id", "session_key", "business_id")},
                )
            except httpx.HTTPError as exc:
                last_error = SarvamNetworkError(f"Sarvam {operation} network failure")
                log.warning(
                    "sarvam.network op=%s attempt=%d latency_ms=%d err=%s %s",
                    operation, attempt, int((time.perf_counter() - started) * 1000), exc,
                    {k: v for k, v in ctx.items() if k in ("call_id", "session_key", "business_id")},
                )
            else:
                latency = int((time.perf_counter() - started) * 1000)
                status = response.status_code
                if status == 429:
                    retry_after = _retry_after_seconds(response)
                    last_error = SarvamRateLimitError(
                        "Sarvam rate limit reached",
                        status=429,
                        retry_after=retry_after,
                        detail=_safe_body(response),
                    )
                    log.warning(
                        "sarvam.rate_limit op=%s attempt=%d latency_ms=%d retry_after=%s %s",
                        operation, attempt, latency, retry_after,
                        {k: v for k, v in ctx.items() if k in ("call_id", "session_key", "business_id")},
                    )
                elif status in (401, 403):
                    # Auth failures never retry — the key itself is wrong/expired.
                    log.error(
                        "sarvam.auth op=%s status=%d latency_ms=%d body=%s",
                        operation, status, latency, _safe_body(response),
                    )
                    raise SarvamAuthError(
                        f"Sarvam rejected the API key (HTTP {status})",
                        status=status,
                        detail=_safe_body(response),
                    )
                elif status >= 500:
                    last_error = SarvamServerError(
                        f"Sarvam server error (HTTP {status})",
                        status=status,
                        detail=_safe_body(response),
                    )
                    log.warning(
                        "sarvam.server op=%s status=%d attempt=%d latency_ms=%d %s",
                        operation, status, attempt, latency,
                        {k: v for k, v in ctx.items() if k in ("call_id", "session_key", "business_id")},
                    )
                elif status >= 400:
                    # 4xx (bad request, quota model errors...) — no retry.
                    raise SarvamBadResponse(
                        f"Sarvam rejected the request (HTTP {status})",
                        status=status,
                        detail=_safe_body(response),
                    )
                else:
                    log.info(
                        "sarvam.ok op=%s status=%d latency_ms=%d %s",
                        operation, status, latency,
                        {k: v for k, v in ctx.items() if k in ("call_id", "session_key", "business_id")},
                    )
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise SarvamBadResponse(
                            f"Sarvam returned a malformed body for {operation}",
                            status=status,
                        ) from exc

            if last_error is not None and attempt <= self.max_retries:
                # Total wall-clock budget across ALL attempts. Without this a
                # slow provider costs timeout x (1 + retries) — measured at
                # 107 s — which guarantees the caller's webhook is abandoned
                # long before it finishes. Give up and let the caller hear the
                # instant <Say> fallback instead of dead air.
                if time.perf_counter() - started_all >= self.total_deadline:
                    log.warning(
                        "sarvam.deadline op=%s exceeded %.0fs after %d attempt(s)",
                        operation, self.total_deadline, attempt,
                    )
                    break
                delay = _backoff_delay(attempt, last_error)
                if isinstance(last_error, SarvamRateLimitError) and last_error.retry_after:
                    delay = max(delay, min(last_error.retry_after, 30.0))
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    # -- speech-to-text -------------------------------------------------------

    async def transcribe(
        self,
        audio: bytes,
        *,
        language_code: str | None = None,
        model: str | None = None,
        with_transliteration: bool | None = None,
        context: dict | None = None,
    ) -> Transcript:
        """Batch STT (saaras). Handles Indian languages + Hinglish code-mixing."""
        if not audio:
            raise SarvamBadResponse("no audio supplied for transcription")
        data: dict[str, Any] = {"model": model or self.stt_model}
        if language_code and language_code not in ("unknown", "auto", ""):
            data["language_code"] = language_code
        if with_transliteration is not None:
            data["with_tn_or_itn"] = "true" if with_transliteration else "false"
        payload = await self._request(
            "POST",
            "/speech-to-text",
            files={"file": ("audio.wav", audio, "audio/wav")},
            data=data,
            operation="stt",
            context=context,
        )
        return Transcript(
            text=(payload.get("transcript") or "").strip(),
            language_code=payload.get("language_code"),
            language_probability=payload.get("language_probability"),
            request_id=payload.get("request_id"),
            latency_ms=int(payload.get("_latency_ms", 0)),
        )

    async def transcribe_translate(
        self,
        audio: bytes,
        *,
        model: str | None = None,
        context: dict | None = None,
    ) -> Transcript:
        """STT + translate to English text (any Indic audio -> English transcript)."""
        payload = await self._request(
            "POST",
            "/speech-to-text-translate",
            files={"file": ("audio.wav", audio, "audio/wav")},
            data={"model": model or self.stt_model},
            operation="stt_translate",
            context=context,
        )
        return Transcript(
            text=(payload.get("transcript") or "").strip(),
            language_code=payload.get("language_code"),
            language_probability=payload.get("language_probability"),
            request_id=payload.get("request_id"),
        )

    # -- text-to-speech --------------------------------------------------------

    async def speak(
        self,
        text: str,
        *,
        target_language_code: str | None = None,
        speaker: str | None = None,
        pitch: float | None = None,
        pace: float | None = None,
        loudness: float | None = None,
        context: dict | None = None,
    ) -> Speech:
        """TTS (bulbul). Long texts are chunked; every chunk returns a wav segment."""
        text = (text or "").strip()
        if not text:
            raise SarvamBadResponse("no text supplied for TTS")
        chunks = chunk_text(text, self.tts_chunk_chars)
        segments: list[bytes] = []
        started = time.perf_counter()
        for chunk in chunks:
            body: dict[str, Any] = {
                "inputs": [chunk],
                "target_language_code": target_language_code or self.tts_language,
                "speaker": speaker or self.tts_speaker,
                "model": self.tts_model,
                "speech_sample_rate": self.tts_sample_rate,
            }
            if pitch is not None:
                body["pitch"] = pitch
            if pace is not None:
                body["pace"] = pace
            if loudness is not None:
                body["loudness"] = loudness
            payload = await self._request(
                "POST", "/text-to-speech", json_body=body,
                operation="tts", context=context,
            )
            audios = payload.get("audios") or []
            if not audios or not audios[0]:
                raise SarvamBadResponse("Sarvam TTS returned no audio", detail=payload)
            try:
                segments.append(base64.b64decode(audios[0]))
            except Exception as exc:
                raise SarvamBadResponse("Sarvam TTS audio could not be decoded") from exc
        return Speech(
            segments=segments,
            sample_rate=self.tts_sample_rate,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # -- text APIs --------------------------------------------------------------

    async def translate(
        self,
        text: str,
        *,
        source_language_code: str = "en-IN",
        target_language_code: str = "hi-IN",
        context: dict | None = None,
    ) -> str:
        """Text translation (mayura) — used to localize spoken replies."""
        payload = await self._request(
            "POST",
            "/translate",
            json_body={
                "input": text,
                "source_language_code": source_language_code,
                "target_language_code": target_language_code,
                "model": self.translate_model,
            },
            operation="translate",
            context=context,
        )
        return (payload.get("translated_text") or "").strip()

    async def transliterate(
        self,
        text: str,
        *,
        source_language_code: str = "en-IN",
        target_language_code: str = "hi-IN",
        numerals: str = "default",
        spoken_form: bool = False,
        context: dict | None = None,
    ) -> str:
        payload = await self._request(
            "POST",
            "/transliterate",
            json_body={
                "input": text,
                "source_language_code": source_language_code,
                "target_language_code": target_language_code,
                "numerals_format": numerals,
                "spoken_form": spoken_form,
                "model": self.transliterate_model,
            },
            operation="transliterate",
            context=context,
        )
        return (payload.get("output_text") or "").strip()


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _backoff_delay(attempt: int, error: SarvamError) -> float:
    """Linear-exponential backoff: 0.75s, 1.5s, 3s... capped."""
    base = 0.75 * (2 ** max(0, attempt - 1))
    return min(base, 8.0)


def _safe_body(response: httpx.Response) -> Any:
    """Truncated body for logs — never include auth headers or the key."""
    try:
        text = response.text
    except Exception:
        return None
    return text[:300] if text else None


# Module-level singleton for the voice engine (built from backend/.env).
sarvam_client = SarvamClient.from_env()
