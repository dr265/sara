# actions/ai_web_scraper.py
"""AI Web Scraper action - real scraping + LLM extraction from voice mode.

Calls the real backend scraper (POST /api/agent, NDJSON stream - the same
route the Hub workspace uses). Pages are actually fetched; the LLM extracts
the requested fields; the JSON/CSV dataset is saved to disk. Honest failure
messages when the target or the backend is unreachable.
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import requests

from core import hub_agents_client as hub

NAME = "AI Web Scraper"
STREAM_TIMEOUT = (15, 300)
MAX_ROWS_SAVED = 5000


def _build_payload(parameters: dict) -> Dict[str, Any]:
    p = parameters or {}
    url = str(p.get("url") or p.get("website") or "").strip()
    if not url:
        raise ValueError("I need the website address to scrape.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    prompt = str(p.get("prompt") or p.get("query") or p.get("fields") or "").strip()
    if not prompt:
        raise ValueError(
            "Tell me what to extract from the page - e.g. 'product names and prices'.")

    mode = str(p.get("mode") or "single").strip().lower()
    if mode not in ("single", "crawl", "pagination"):
        mode = "single"

    payload: Dict[str, Any] = {"url": url, "prompt": prompt[:2000], "mode": mode}
    try:
        max_pages = int(p.get("max_pages") or 0)
    except (TypeError, ValueError):
        max_pages = 0
    if max_pages > 0:
        payload["maxPages"] = max(1, min(50, max_pages))

    fields = p.get("schema_fields") or p.get("fields_list")
    if isinstance(fields, list) and fields:
        payload["schemaFields"] = [str(f)[:80] for f in fields[:25]]
    elif isinstance(fields, str) and fields.strip():
        payload["schemaFields"] = [f.strip() for f in fields.split(",") if f.strip()][:25]
    return payload


def _save_dataset(rows: List[Dict[str, Any]], url: str) -> str:
    """Persist the extracted rows (JSON + CSV). Returns the saved path or ''."""
    target_dir = Path.home() / "Documents" / "SARA Scrapes"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = target_dir / f"scrape_{stamp}"

    trimmed = rows[:MAX_ROWS_SAVED]
    try:
        (base.with_suffix(".json")).write_text(
            json.dumps({"source": url, "rows": trimmed}, indent=2,
                       ensure_ascii=False, default=str),
            encoding="utf-8")
    except Exception:
        return ""

    if trimmed and isinstance(trimmed[0], dict):
        headers: List[str] = []
        for row in trimmed:
            if isinstance(row, dict):
                for key in row.keys():
                    if key not in headers:
                        headers.append(str(key))
        try:
            with open(base.with_suffix(".csv"), "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
                writer.writeheader()
                for row in trimmed:
                    if isinstance(row, dict):
                        writer.writerow({k: hub.brief(row.get(k, ""), 300) for k in headers})
        except Exception:
            pass
    return str(base.with_suffix(".json"))


def scrape_web(parameters: dict) -> str:
    try:
        payload = _build_payload(parameters)
    except ValueError as exc:
        return str(exc)

    rows: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {}
    error_msg = ""

    try:
        with requests.post(
            hub.url("/api/agent"),
            json=payload,
            headers=hub.headers(),
            stream=True,
            timeout=STREAM_TIMEOUT,
        ) as resp:
            if resp.status_code >= 400:
                return hub.http_failure(NAME, resp)
            for raw in resp.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                if isinstance(raw, bytes):  # x-ndjson carries no charset
                    raw = raw.decode("utf-8", errors="replace")
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = str(event.get("type") or "")
                if etype == "done":
                    record = event.get("record") or {}
                    rows = record.get("rows") or []
                    stats = record.get("stats") or {}
                    break
                if etype == "error":
                    error_msg = str(event.get("message") or "the scrape failed")
                    break
    except requests.exceptions.RequestException as exc:
        if not rows and not stats:
            return hub.unreachable(NAME, exc)

    if error_msg and not stats:
        return f"AI Web Scraper could not finish that page ({hub.brief(error_msg, 200)})"
    if not stats:
        return ("AI Web Scraper returned no result - check the website address and "
                "try again, or open /hub/ai-web-scraper.")

    pages_ok = int(stats.get("pagesOk") or 0)
    pages_failed = int(stats.get("pagesFailed") or 0)
    rows_final = int(stats.get("rowsFinal") or len(rows))

    parts = [f"Scraped {pages_ok} page(s)"]
    if pages_failed:
        parts.append(f"{pages_failed} failed")
    parts.append(f"extracted {rows_final} row(s)")

    if rows:
        sample = hub.brief(rows[0], 220)
        parts.append(f"Sample: {sample}")
        saved = _save_dataset(rows, payload["url"])
        if saved:
            parts.append(f"Saved to {saved}")

    if error_msg:
        parts.append(f"Note: {hub.brief(error_msg, 150)}")
    return ". ".join(parts) + "."


TOOL = {
    "name": "ai_web_scraper",
    "description": (
        "Scrapes a real website and extracts structured data with AI - "
        "single page, multi-page crawl, or pagination. Extracted rows are saved "
        "as JSON and CSV under Documents/SARA Scrapes. "
        "Use when the user asks to scrape, collect, extract or harvest data from a website. "
        "To OPEN the scraper workspace page (not run it), use open_agency_workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "url": {
                "type": "STRING",
                "description": "The website address to scrape, e.g. 'https://example.com/products'.",
            },
            "prompt": {
                "type": "STRING",
                "description": "What to extract, e.g. 'product names, prices and ratings'.",
            },
            "mode": {
                "type": "STRING",
                "description": "Optional: 'single' (default), 'crawl', or 'pagination'.",
            },
            "max_pages": {
                "type": "NUMBER",
                "description": "Optional page budget for crawl/pagination (1-50).",
            },
            "schema_fields": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Optional exact field names to extract, e.g. ['name', 'price'].",
            },
        },
        "required": ["url", "prompt"],
    },
    "handler": scrape_web,
}
