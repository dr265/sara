"""
AI Web Scrapper — SARA Nexus Hub agent (backend).

Natural-language web scraping:
  * single page / multi-page crawl (BFS, depth + page caps, same-domain) /
    pagination following (rel=next, "next" anchors, page-number heuristic)
  * retries, timeouts, throttling, size caps, duplicate-URL prevention
  * LLM extraction through an OpenAI-compatible chat endpoint — configured
    ONLY via backend/.env (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL, optional
    OPENAI_API_KEY, OLLAMA_BASE_URL / OLLAMA_MODEL / OLLAMA_EMBEDDING_MODEL)
  * deterministic normalization (flatten, dedupe, source traceability),
    validation stats, JSONL history persistence under backend/data/

Security model (inherited from the original single-file app):
  * API keys are read exclusively from the environment — never from the API
  * scraped content and model output are untrusted DATA: parsed, normalized,
    serialized — never executed (no eval/exec anywhere)
  * SSRF guard: loopback / link-local / cloud-metadata always blocked;
    RFC1918/CGNAT blocked unless the request explicitly allows private targets

Wire protocol (consumed by frontend/src/app/hub/ai-web-scraper):
  GET  /api/agent          -> provider status (openai/ollama reachability)
  POST /api/agent          -> NDJSON event stream:
        {"type":"status","phase","message"} {"type":"progress","done","total"}
        {"type":"page","page":{...}} {"type":"warning","message"}
        {"type":"done","record":{...}} {"type":"error","message"}
  GET  /api/agent/history  -> persisted runs (newest first)
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Comment
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

try:  # urllib3 retry adapter (present in any requests install)
    from urllib3.util.retry import Retry
    from requests.adapters import HTTPAdapter

    _URLLIB3_OK = True
except Exception:  # pragma: no cover
    Retry = None  # type: ignore
    HTTPAdapter = None  # type: ignore
    _URLLIB3_OK = False

router = APIRouter(prefix="/api/agent", tags=["scraping-agent"])

# --------------------------------------------------------------------------- #
# Configuration — environment only
# --------------------------------------------------------------------------- #

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 SARAScrapeAgent/1.0"
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
HISTORY_FILE = DATA_DIR / "scraper_history.jsonl"
HISTORY_LIMIT = 30


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _cloud_brain() -> Dict[str, Any]:
    """Primary cloud brain: OPENAI_* or the shared LLM_* config from .env."""
    openai_key = _env("OPENAI_API_KEY")
    if openai_key:
        return {
            "kind": "openai",
            "api_key": openai_key,
            "base_url": (_env("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
            "model": _env("OPENAI_MODEL", "gpt-4o"),
        }
    llm_key = _env("LLM_API_KEY")
    if llm_key:
        return {
            "kind": "openai",
            "api_key": llm_key,
            "base_url": (_env("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
            "model": _env("LLM_MODEL", "gpt-4o"),
        }
    return {"kind": "openai", "api_key": "", "base_url": "", "model": _env("OPENAI_MODEL", "gpt-4o")}


def _ollama_base() -> str:
    return (_env("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")


def _ollama_model() -> str:
    return _env("OLLAMA_MODEL", "llama3.2")


def _ollama_embed() -> str:
    return _env("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")


def mask_secret(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


# --------------------------------------------------------------------------- #
# URL utilities: validation, canonicalization, SSRF guard
# --------------------------------------------------------------------------- #

_BLOCKED_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
)
_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
)

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "ref", "source", "mc_cid", "mc_eid",
}
_SKIP_EXTENSIONS = re.compile(
    r"\.(pdf|zip|gz|tar|rar|7z|jpg|jpeg|png|gif|webp|svg|ico|mp3|mp4|avi|mov|"
    r"webm|docx?|xlsx?|pptx?|exe|dmg|iso|css|js|json)$",
    re.IGNORECASE,
)
_PAGE_QUERY_KEYS = ("page", "p", "pg", "paged")
_UNWRAP_KEYS = ("items", "results", "data", "products", "records", "listings",
                "entries", "books")
_NEXT_ANCHOR_TEXTS = (
    "next", "next page", "next ›", "next »", "next page ›", "next page »",
    "›", "»", "→", "older", "older posts", "next posts", "load more",
)


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        return url.strip()
    port = parts.port
    try:
        default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    except Exception:
        default = False
    netloc = host
    if port and not default:
        netloc = f"{host}:{port}"
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() not in _TRACKING_PARAMS]
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def _ip_blocked(ip: Any, allow_private: bool) -> bool:
    if any(ip in net for net in _BLOCKED_NETS):
        return True
    if not allow_private and any(ip in net for net in _PRIVATE_NETS):
        return True
    return False


def validate_url(raw: str, allow_private: bool = False) -> tuple[bool, str, str]:
    """Syntax + scheme + DNS + SSRF check. Returns (ok, message, canonical)."""
    url = (raw or "").strip()
    if not url:
        return False, "URL is empty.", ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return False, "URL must start with http:// or https://", ""
    try:
        parts = urlsplit(url)
        if not parts.netloc:
            return False, "URL has no hostname.", ""
        host = (parts.hostname or "").lower()
        if host == "localhost":
            return False, "Blocked: 'localhost' is never allowed as a target.", ""
        try:
            ip: Any = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is None:
            try:
                resolved = socket.gethostbyname(host)
                ip = ipaddress.ip_address(resolved)
            except socket.gaierror:
                return False, f"Host '{host}' could not be resolved (DNS).", ""
        if ip is not None and _ip_blocked(ip, allow_private):
            return False, (
                f"Blocked: '{host}' resolves to {ip}, which is a loopback / "
                "link-local / private / metadata address."
            ), ""
        return True, "OK", canonicalize_url(url)
    except Exception as exc:  # noqa: BLE001
        return False, f"Invalid URL ({type(exc).__name__}: {exc}).", ""


def domain_key(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def same_domain(url_a: str, url_b: str) -> bool:
    try:
        return domain_key(url_a) == domain_key(url_b)
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Link discovery + pagination detection (deterministic)
# --------------------------------------------------------------------------- #

def extract_links(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: List[str] = []
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        absolute = urljoin(base_url, href)
        if not re.match(r"^https?://", absolute, re.IGNORECASE):
            continue
        if _SKIP_EXTENSIONS.search(urlsplit(absolute).path):
            continue
        links.append(canonicalize_url(absolute))
    return links


def extract_page_number(url: str) -> Optional[int]:
    parts = urlsplit(url)
    query = {k.lower(): v for k, v in parse_qsl(parts.query, keep_blank_values=True)}
    for key in _PAGE_QUERY_KEYS:
        value = (query.get(key) or "").strip()
        if re.fullmatch(r"\d+", value):
            return int(value)
    match = re.search(r"/(?:page|p(?:age)?)[/_](\d+)/?$", parts.path)
    if match:
        return int(match.group(1))
    match = re.search(r"/(\d+)/?$", parts.path)
    if match:
        return int(match.group(1))
    return None


def set_page_number(url: str, number: int) -> str:
    parts = urlsplit(url)
    has_key = any(k.lower() in _PAGE_QUERY_KEYS for k, _ in parse_qsl(parts.query))
    if has_key:
        query = urlencode(
            [(k, str(number) if k.lower() in _PAGE_QUERY_KEYS else v)
             for k, v in parse_qsl(parts.query, keep_blank_values=True)]
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    match = re.search(r"^(.*/)(?:page|p(?:age)?)([/_])(\d+)(/?)$", parts.path)
    if match:
        path = f"{match.group(1)}page{match.group(2)}{number}{match.group(4)}"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    match = re.search(r"^(.*/)(\d+)(/?)$", parts.path)
    if match:
        path = f"{match.group(1)}{number}{match.group(3)}"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    query = urlencode(parse_qsl(parts.query, keep_blank_values=True) + [("page", str(number))])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def find_next_page_url(current_url: str, html: str, links: List[str]) -> Optional[str]:
    current_canonical = canonicalize_url(current_url)
    soup = BeautifulSoup(html, "html.parser")

    for node in soup.find_all(["a", "link"], rel=True, href=True):
        rels = {
            r.strip().lower()
            for r in " ".join(node.get("rel", []) if isinstance(node.get("rel"), list)
                              else [str(node.get("rel", ""))]).split()
        }
        if "next" in rels:
            candidate = canonicalize_url(urljoin(current_url, node.get("href", "")))
            if candidate and candidate != current_canonical:
                return candidate

    for anchor in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip().lower()
        if text in _NEXT_ANCHOR_TEXTS or (len(text) <= 16 and text.startswith(("next", "older"))):
            candidate = canonicalize_url(urljoin(current_url, anchor.get("href", "")))
            if candidate and candidate != current_canonical:
                return candidate

    page_number = extract_page_number(current_url)
    if page_number is not None:
        expected = canonicalize_url(set_page_number(current_url, page_number + 1))
        if expected in links:
            return expected
    return None


# --------------------------------------------------------------------------- #
# Value helpers: number parsing, normalization
# --------------------------------------------------------------------------- #

_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def parse_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    text = str(value)
    for symbol in ("₹", "₨", "$", "€", "£", "¥", "Rs", "INR", "USD", "EUR"):
        text = text.replace(symbol, "")
    match = _NUMBER_RE.search(text.replace(" ", ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _to_plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _to_plain(model_dump())
        except Exception:  # noqa: BLE001
            pass
    legacy_dump = getattr(value, "dict", None)
    if callable(legacy_dump):
        try:
            return _to_plain(legacy_dump())
        except Exception:  # noqa: BLE001
            pass
    return value


def _json_safe(value: Any) -> Any:
    import math

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        converted = [_json_safe(v) for v in value]
        if converted and all(isinstance(v, (str, int, float, bool, type(None)))
                             for v in converted):
            return ", ".join(str(v) for v in converted if v not in (None, ""))
        return json.dumps(converted, ensure_ascii=False, default=str)
    if isinstance(value, dict):
        return json.dumps(_to_plain(value), ensure_ascii=False, default=str)
    return str(value)


def flatten_row(item: dict, source_url: str) -> dict:
    out: Dict[str, Any] = {}

    def walk(node: dict, prefix: str) -> None:
        for key, value in node.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict) and value:
                walk(value, name)
            else:
                out[name] = _json_safe(value)

    walk(item, "")
    out["source_url"] = source_url
    return out


def dedupe_rows(rows: List[dict]) -> List[dict]:
    seen = set()
    unique: List[dict] = []
    for row in rows:
        payload = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
        if digest not in seen:
            seen.add(digest)
            unique.append(row)
    return unique


def normalize_result(raw: Any, source_url: str) -> List[dict]:
    raw = _to_plain(raw)

    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        if text.startswith(("{", "[")):
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                raw = text
        else:
            raw = text

    rows: List[dict] = []
    if isinstance(raw, dict):
        for key in _UNWRAP_KEYS:
            payload = raw.get(key)
            if isinstance(payload, list):
                unwrapped = [flatten_row(i, source_url) for i in payload if isinstance(i, dict)]
                if unwrapped:
                    return dedupe_rows(unwrapped)
        rows = [flatten_row(raw, source_url)]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                rows.append(flatten_row(item, source_url))
            elif item is not None and item != "":
                rows.append({"value": _json_safe(item), "source_url": source_url})
    elif raw not in (None, ""):
        rows = [{"value": _json_safe(raw), "source_url": source_url}]

    rows = [
        r for r in rows
        if any(v not in (None, "") for k, v in r.items() if k != "source_url")
    ]
    return dedupe_rows(rows)


# --------------------------------------------------------------------------- #
# HTTP fetching layer — retries, timeouts, throttling, caps
# --------------------------------------------------------------------------- #

class WebFetcher:
    def __init__(self, timeout: float = 30.0, retries: int = 2, delay: float = 0.0,
                 max_bytes: int = 3_000_000, user_agent: Optional[str] = None,
                 verify_ssl: bool = True):
        self.timeout = timeout
        self.delay = max(0.0, delay)
        self.max_bytes = max_bytes
        self.verify_ssl = verify_ssl
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": user_agent or DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if _URLLIB3_OK and retries > 0 and Retry is not None and HTTPAdapter is not None:
            retry = Retry(
                total=retries, connect=retries, read=retries,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)
        self._last_request_ts = 0.0

    def fetch(self, url: str) -> Dict[str, Any]:
        started = time.monotonic()
        wait = self.delay - (started - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)
        try:
            response = self._session.get(
                url, timeout=(self.timeout, self.timeout),
                allow_redirects=True, verify=self.verify_ssl, stream=True,
            )
            try:
                body = response.raw.read(self.max_bytes + 1, decode_content=True)
                truncated = len(body) > self.max_bytes
                body = body[: self.max_bytes]
            finally:
                response.close()
            content_type = (response.headers.get("Content-Type") or "").lower()
            encoding = response.encoding
            if not encoding or encoding.lower() in ("iso-8859-1", "latin-1"):
                # requests defaults to latin-1 when no charset is declared;
                # prefer the real detection (meta charset / BOM / chardet).
                encoding = response.apparent_encoding or "utf-8"
            html = body.decode(encoding or "utf-8", errors="replace")
            looks_html = bool(re.search(
                r"<(?:!doctype|html|body|head|div|table|main)[\s>]", html, re.I))
            base: Dict[str, Any] = {
                "status": response.status_code,
                "final_url": str(response.url),
                "html": html,
                "truncated": truncated,
                "seconds": time.monotonic() - started,
            }
            if response.status_code >= 400:
                base["ok"] = False
                base["error"] = f"HTTP {response.status_code}"
                return base
            if "text/html" not in content_type and "xml" not in content_type and not looks_html:
                base["ok"] = False
                base["error"] = f"Non-HTML content-type: {content_type or 'unknown'}"
                return base
            base["ok"] = True
            base["error"] = ""
            return base
        except requests.RequestException as exc:
            return {"ok": False, "status": None, "final_url": url, "html": "",
                    "truncated": False, "error": f"{type(exc).__name__}: {exc}",
                    "seconds": time.monotonic() - started}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": None, "final_url": url, "html": "",
                    "truncated": False, "error": f"{type(exc).__name__}: {exc}",
                    "seconds": time.monotonic() - started}
        finally:
            self._last_request_ts = time.monotonic()


def preprocess_html(html: str, strip_noise: bool, max_chars: int) -> tuple[str, int, int]:
    original = len(html)
    if strip_noise:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg", "template",
                             "iframe", "head"]):
                tag.decompose()
            for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
                comment.extract()
            html = str(soup)
        except Exception:  # noqa: BLE001
            pass
    used = len(html)
    if used > max_chars:
        html = html[:max_chars]
        used = len(html)
    return html, original, used


# --------------------------------------------------------------------------- #
# LLM extraction — OpenAI-compatible endpoint configured from .env only
# --------------------------------------------------------------------------- #

_OLLAMA_PROBE_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_OLLAMA_PROBE_LOCK = threading.Lock()


def ollama_health(base_url: Optional[str] = None, timeout: float = 3.0) -> Dict[str, Any]:
    """Probe Ollama's real /api/tags endpoint (10s cache)."""
    base = (base_url or _ollama_base()).rstrip("/")
    now = time.monotonic()
    with _OLLAMA_PROBE_LOCK:
        cached = _OLLAMA_PROBE_CACHE.get("data")
        if cached and now - float(_OLLAMA_PROBE_CACHE["ts"]) < 10 and cached.get("base") == base:
            return cached
    try:
        response = requests.get(f"{base}/api/tags", timeout=timeout)
        if response.ok:
            models = [str(m.get("name", "")) for m in response.json().get("models", [])
                      if m.get("name")]
            data = {"base": base, "reachable": True, "installed": models, "error": ""}
        else:
            data = {"base": base, "reachable": False, "installed": [],
                    "error": f"HTTP {response.status_code}"}
    except requests.RequestException as exc:
        data = {"base": base, "reachable": False, "installed": [],
                "error": f"{type(exc).__name__}: {exc}"}
    with _OLLAMA_PROBE_LOCK:
        _OLLAMA_PROBE_CACHE["ts"] = time.monotonic()
        _OLLAMA_PROBE_CACHE["data"] = data
    return data


def _resolve_provider(choice: str) -> tuple[str, List[str]]:
    """Map the requested provider to a concrete backend + setup warnings."""
    warnings: List[str] = []
    cloud = _cloud_brain()
    if choice == "openai":
        if cloud["api_key"]:
            return "cloud", warnings
        warnings.append(
            "OpenAI/cloud provider requested but no key is configured — set "
            "OPENAI_API_KEY or LLM_API_KEY in backend/.env. Falling back to Ollama."
        )
        return "ollama", warnings
    if choice == "ollama":
        return "ollama", warnings
    # auto
    if cloud["api_key"]:
        return "cloud", warnings
    ollama = ollama_health()
    if ollama["reachable"]:
        return "ollama", warnings
    warnings.append(
        "No LLM backend available: set OPENAI_API_KEY or LLM_API_KEY in "
        "backend/.env (cloud), or start Ollama at " + ollama["base"] + "."
    )
    return "none", warnings


def _chat_completion(base_url: str, api_key: str, model: str,
                     system: str, user: str, llm_timeout: float) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    last_error = ""
    for attempt in (1, 2):  # one retry for transient 5xx from the provider
        try:
            response = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers, json=payload, timeout=(llm_timeout, llm_timeout),
            )
            if response.status_code < 400:
                data = response.json()
                return str(data["choices"][0]["message"]["content"])
            if response.status_code >= 500 and attempt == 1:
                last_error = f"LLM HTTP {response.status_code}"
                time.sleep(1.5)
                continue
            raise RuntimeError(
                f"LLM HTTP {response.status_code}: {response.text[:300]}")
        except requests.RequestException as exc:
            if attempt == 1:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(1.5)
                continue
            raise RuntimeError(f"LLM request failed: {exc}") from exc
    raise RuntimeError(last_error or "LLM request failed")


def html_to_text(html: str) -> str:
    """Readable visible text with light structure markers — small, LLM-friendly."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
        node.decompose()
    lines: List[str] = []
    title = soup.find("title")
    if title and title.get_text(strip=True):
        lines.append(f"PAGE TITLE: {title.get_text(strip=True)}")
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "a"]):
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if not text:
            continue
        tag = el.name
        if tag in ("h1", "h2", "h3", "h4"):
            lines.append(f"[{text}]")
        elif tag == "li":
            lines.append(f"- {text}")
        elif tag in ("td", "th"):
            lines.append(text)
        else:
            lines.append(text)
    # collapse consecutive duplicates/empties, cap runaway repetition
    out: List[str] = []
    prev = None
    for line in lines:
        if line == prev:
            continue
        out.append(line)
        prev = line
    return "\n".join(out)


def llm_extract(html: str, prompt: str, fields: List[str], backend: str,
                llm_timeout: float) -> Any:
    """Run one extraction pass over cleaned HTML; returns parsed JSON-ish data."""
    if backend == "cloud":
        brain = _cloud_brain()
        base_url, api_key, model = brain["base_url"], brain["api_key"], brain["model"]
    else:
        base_url = _ollama_base() + "/v1"
        api_key = ""
        model = _ollama_model()
    if not base_url:
        raise RuntimeError("No LLM backend configured — add OPENAI_API_KEY or "
                           "LLM_API_KEY to backend/.env.")

    field_line = ""
    if fields:
        field_line = (
            " Each item object must use EXACTLY these keys: "
            + ", ".join(f'"{f}"' for f in fields) + "."
        )
    system = (
        "You are a web-scraping extraction engine. You receive the visible text "
        "of ONE web page and an instruction. Extract only data present in the "
        "page. Respond with STRICT JSON only — no markdown, no commentary. "
        'Shape: {"items": [ {field: value, ...}, ... ]} where items holds every '
        "record found on the page (empty array if none)." + field_line
        + " Values must be short strings or numbers; never invent data."
    )
    content = (
        f"Instruction: {prompt}\n\n"
        f"Page content:\n\n{html_to_text(html)[:60_000]}"
    )
    raw = _chat_completion(base_url, api_key, model, system, content, llm_timeout)
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return raw  # normalize_result handles strings gracefully


# --------------------------------------------------------------------------- #
# Request models + history persistence
# --------------------------------------------------------------------------- #

class ScrapeConfig(BaseModel):
    url: str = ""                                    # legacy single-URL field (still honoured)
    urls: List[str] = Field(default_factory=list)     # one or many target URLs
    prompt: str
    mode: str = "single"                # single | crawl | pagination
    maxPages: int = 5
    maxDepth: int = 2
    sameDomain: bool = True
    timeoutSec: float = 30
    retries: int = 2
    delayMs: int = 500
    llmTimeoutSec: float = 180
    schemaFields: List[str] = Field(default_factory=list)
    stripNoise: bool = True
    provider: str = "auto"              # auto | openai | ollama
    allowPrivate: bool = False


def _append_history(record: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines() if HISTORY_FILE.exists() else []
        lines.append(json.dumps(record, ensure_ascii=False, default=str))
        HISTORY_FILE.write_text("\n".join(lines[-HISTORY_LIMIT:]) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 — history is best-effort
        pass


def _read_history() -> List[dict]:
    try:
        if not HISTORY_FILE.exists():
            return []
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        records = []
        for line in reversed(lines[-HISTORY_LIMIT:]):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- #
# Status endpoint
# --------------------------------------------------------------------------- #

@router.get("/health")
def agent_health() -> dict:
    """Liveness probe for the hub registry audit (cheap, no scraping)."""
    return {"status": "ok", "agent": "ai-web-scraper"}


@router.get("")
def agent_status() -> dict:
    cloud = _cloud_brain()
    ollama = ollama_health()
    return {
        "serverTime": datetime.now(timezone.utc).isoformat(),
        "openai": {
            "configured": bool(cloud["api_key"]),
            "model": cloud["model"],
            "maskedKey": mask_secret(cloud["api_key"]),
        },
        "ollama": {
            "baseUrl": ollama["base"],
            "model": _ollama_model(),
            "embeddingModel": _ollama_embed(),
            "reachable": ollama["reachable"],
            "installed": ollama["installed"],
            "error": ollama["error"],
        },
    }


@router.get("/history")
def agent_history() -> dict:
    return {"runs": _read_history()}


# --------------------------------------------------------------------------- #
# Scrape pipeline — NDJSON event stream
# --------------------------------------------------------------------------- #

def _sse_line(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"


@router.post("")
def run_scrape(config: ScrapeConfig) -> StreamingResponse:
    def event_stream() -> Generator[str, None, None]:
        started = time.monotonic()
        run_id = f"scrape_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        started_at = datetime.now(timezone.utc).isoformat()
        warnings: List[dict] = []
        pages: List[dict] = []
        rows: List[dict] = []

        def status(message: str, phase: str = "working") -> str:
            return _sse_line({"type": "status", "phase": phase, "message": message})

        def warn(message: str) -> str:
            warnings.append(message)
            return _sse_line({"type": "warning", "message": message})

        try:
            # 1) Validate every target URL (multi-URL run) ---------------------
            requested: List[str] = []
            for candidate in [*(config.urls or []), config.url]:
                text = (candidate or "").strip()
                if text and text not in requested:
                    requested.append(text)
            if not requested:
                yield _sse_line({"type": "error",
                                 "message": "Provide at least one target URL."})
                return
            roots: List[str] = []
            for candidate in requested:
                ok, message, canonical = validate_url(candidate, config.allowPrivate)
                if ok:
                    if canonical not in roots:
                        roots.append(canonical)
                else:
                    yield warn(f"Skipped {candidate} — {message}")
            if not roots:
                yield _sse_line({
                    "type": "error",
                    "message": "Every target URL was rejected by the SSRF/DNS guard.",
                })
                return
            prompt = (config.prompt or "").strip()
            if len(prompt) < 3:
                yield _sse_line({"type": "error",
                                 "message": "Provide scraping instructions (a few characters at least)."})
                return

            # 2) Resolve the LLM backend --------------------------------------
            backend, setup_warnings = _resolve_provider(config.provider)
            for w in setup_warnings:
                yield warn(w)
            if backend == "none":
                yield _sse_line({
                    "type": "error",
                    "message": ("No LLM backend available. Set OPENAI_API_KEY or "
                                "LLM_API_KEY in backend/.env, or start Ollama."),
                })
                return
            cloud = _cloud_brain()
            model_name = cloud["model"] if backend == "cloud" else _ollama_model()

            yield status(f"{len(roots)} URL(s) validated", "validate")
            yield status(f"Backend: {backend} · {model_name}", "plan")

            # 3) Plan and execute the page collection -------------------------
            # Single mode scrapes each requested URL once; crawl/pagination
            # expands from every seed within one shared page budget.
            n_seeds = len(roots)
            if config.mode == "single":
                effective_max = n_seeds
            else:
                effective_max = max(max(1, min(50, config.maxPages)), n_seeds)
            effective_max = min(effective_max, 100)
            max_depth = max(0, min(10, config.maxDepth))
            fetcher = WebFetcher(
                timeout=max(5.0, min(120.0, config.timeoutSec)),
                retries=max(0, min(5, config.retries)),
                delay=max(0.0, config.delayMs / 1000.0),
                user_agent=_env("SCRAPER_USER_AGENT") or None,
                verify_ssl=_env("SCRAPER_VERIFY_SSL", "true").lower() not in ("0", "false", "no"),
            )

            queue: deque = deque([(seed, 0) for seed in roots])
            root_domains = {domain_key(seed) for seed in roots}
            seen = set()
            skipped_dupes = 0
            skipped_external = 0
            pages_done = 0
            raw_row_count = 0
            queue_budget = effective_max * 25

            yield _sse_line({"type": "progress", "done": 0, "total": effective_max})

            while queue and pages_done < effective_max:
                url, depth = queue.popleft()
                if url in seen:
                    skipped_dupes += 1
                    continue
                seen.add(url)

                yield status(f"Fetching {url}", "fetch")
                fetch_started = time.monotonic()
                fetched = fetcher.fetch(url)
                fetch_ms = (time.monotonic() - fetch_started) * 1000

                if not fetched["ok"]:
                    page = {
                        "url": url, "finalUrl": fetched["final_url"] or url,
                        "depth": depth, "ok": False,
                        "statusCode": fetched["status"], "error": fetched["error"],
                        "fetchMs": round(fetch_ms), "extractMs": 0,
                        "htmlChars": len(fetched["html"]), "htmlUsed": 0,
                        "rowCount": 0, "raw": None,
                    }
                    pages.append(page)
                    yield _sse_line({"type": "page", "page": page})
                    pages_done += 1
                    yield _sse_line({"type": "progress", "done": pages_done,
                                     "total": effective_max})
                    continue

                display_url = fetched["final_url"] or url
                html, original_chars, used_chars = preprocess_html(
                    fetched["html"], config.stripNoise, 200_000)

                yield status(f"Extracting with {model_name} …", "extract")
                extract_started = time.monotonic()
                error = ""
                result: Any = None
                try:
                    result = llm_extract(html, prompt, config.schemaFields,
                                         backend, max(30.0, config.llmTimeoutSec))
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {re.sub(chr(10), ' ', str(exc))[:300]}"
                extract_ms = (time.monotonic() - extract_started) * 1000

                if error:
                    page = {
                        "url": url, "finalUrl": display_url, "depth": depth,
                        "ok": False, "statusCode": fetched["status"], "error": error,
                        "fetchMs": round(fetch_ms), "extractMs": round(extract_ms),
                        "htmlChars": original_chars, "htmlUsed": used_chars,
                        "rowCount": 0, "raw": None,
                    }
                    pages.append(page)
                    yield _sse_line({"type": "page", "page": page})
                else:
                    page_rows = normalize_result(result, display_url)
                    raw_row_count += len(page_rows)
                    rows.extend(page_rows)
                    page = {
                        "url": url, "finalUrl": display_url, "depth": depth,
                        "ok": True, "statusCode": fetched["status"], "error": "",
                        "fetchMs": round(fetch_ms), "extractMs": round(extract_ms),
                        "htmlChars": original_chars, "htmlUsed": used_chars,
                        "rowCount": len(page_rows), "raw": result,
                    }
                    pages.append(page)
                    yield _sse_line({"type": "page", "page": page})

                pages_done += 1
                yield _sse_line({"type": "progress", "done": pages_done,
                                 "total": effective_max})
                if pages_done >= effective_max:
                    break

                # 4) Discover next pages --------------------------------------
                if config.mode == "pagination":
                    links = extract_links(fetched["html"], display_url)
                    next_url = find_next_page_url(display_url, fetched["html"], links)
                    if next_url:
                        queue.append((next_url, 0))
                        yield status(f"Pagination → next page {next_url}", "discover")
                    else:
                        yield status("No next-page link detected — pagination finished.", "discover")
                elif config.mode == "crawl":
                    if depth >= max_depth:
                        yield status(f"Depth limit ({max_depth}) reached at {display_url}.",
                                     "discover")
                        continue
                    links = extract_links(fetched["html"], display_url)
                    added = 0
                    for link in links:
                        if config.sameDomain and domain_key(link) not in root_domains:
                            skipped_external += 1
                            continue
                        if link in seen:
                            continue
                        if len(queue) < queue_budget:
                            queue.append((link, depth + 1))
                            added += 1
                    yield status(f"Discovered {added} new link(s) at depth {depth + 1}.",
                                 "discover")

            # 5) Global dedupe + finalize -------------------------------------
            before = len(rows)
            rows = dedupe_rows(rows)
            removed = before - len(rows)
            if removed:
                yield warn(f"Removed {removed} duplicate record(s).")

            stats = {
                "urlsRequested": len(requested),
                "urlsAccepted": len(roots),
                "pagesAttempted": pages_done,
                "pagesOk": sum(1 for p in pages if p["ok"]),
                "pagesFailed": sum(1 for p in pages if not p["ok"]),
                "rowsRaw": raw_row_count,
                "rowsFinal": len(rows),
                "duplicateUrlsSkipped": skipped_dupes,
                "externalSkipped": skipped_external,
                "fetchMs": round(sum(p["fetchMs"] for p in pages)),
                "extractMs": round(sum(p["extractMs"] for p in pages)),
                "htmlChars": sum(p["htmlChars"] for p in pages),
                "durationMs": round((time.monotonic() - started) * 1000),
            }

            if not pages:
                yield _sse_line({"type": "error", "message": "No pages were processed."})
                return

            record = {
                "id": run_id,
                "startedAt": started_at,
                "endedAt": datetime.now(timezone.utc).isoformat(),
                "status": "completed",
                "config": {
                    "url": roots[0], "urls": roots,
                    "prompt": prompt, "mode": config.mode,
                    "maxPages": config.maxPages, "maxDepth": config.maxDepth,
                    "sameDomain": config.sameDomain, "timeoutSec": config.timeoutSec,
                    "retries": config.retries, "delayMs": config.delayMs,
                    "llmTimeoutSec": config.llmTimeoutSec,
                    "schemaFields": config.schemaFields,
                    "stripNoise": config.stripNoise, "provider": backend,
                    "model": model_name,
                },
                "pages": pages,
                "rows": rows,
                "stats": stats,
                "warnings": warnings,
            }
            _append_history(record)
            yield _sse_line({"type": "done", "record": record})
        except Exception as exc:  # noqa: BLE001 — never leak a stack to the stream
            yield _sse_line({
                "type": "error",
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            })

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
