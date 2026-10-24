"""Knowledge base for the WhatsApp agent.

Shares the sara.db tables used by the Telegram agent (documents, chunks, facts)
so the whole ecosystem keeps one store. Supports CSV / TXT / JSON uploads, and
BM25-style lexical retrieval over the WhatsApp tenant's chunk index.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parent.parent.parent / "sara.db"

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "for", "to", "of",
    "in", "on", "at", "with", "is", "are", "was", "were", "be", "been", "it", "this",
    "that", "i", "you", "we", "they", "he", "she", "me", "my", "your", "our", "their",
    "do", "does", "did", "can", "could", "will", "would", "should", "have", "has",
    "had", "not", "no", "yes", "please", "what", "when", "where", "how", "why",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  id TEXT, tenant_id TEXT, version INTEGER, filename TEXT, ext TEXT, mime TEXT,
  size INTEGER, sha256 TEXT, status TEXT, route TEXT, storage_path TEXT,
  created_at TEXT, PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY, tenant_id TEXT, doc_id TEXT, doc_version INTEGER,
  ordinal INTEGER, text TEXT, locator TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS facts(
  id TEXT PRIMARY KEY, tenant_id TEXT, entity TEXT, key TEXT, value TEXT,
  numeric_value REAL, currency TEXT, valid_from TEXT, valid_to TEXT,
  confidence REAL, doc_id TEXT, doc_version INTEGER, locator TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_wa_chunks_tenant ON chunks(tenant_id, doc_id);
"""


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _slug(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:max_len] or "item"


class KnowledgeRepository:
    """Document ingestion + BM25 retrieval, scoped to one tenant."""

    def __init__(self, db_path: Optional[Path] = None, tenant_id: str = "whatsapp") -> None:
        db_path = db_path or DB_PATH  # callers pass None to mean "the shared sara.db"
        self.path = db_path
        self.tenant_id = tenant_id
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self.conn:
            self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ #
    # ingestion
    # ------------------------------------------------------------------ #
    def ingest(self, filename: str, data: bytes, mime: str = "") -> Dict[str, Any]:
        """Parse CSV/TXT/JSON bytes into chunks + facts, store and index them."""
        if not data:
            raise ValueError("empty file")
        ext = Path(filename).suffix.lower().lstrip(".") or "txt"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")

        chunks: List[str] = []
        facts: List[Dict[str, str]] = []
        lines = text.splitlines()

        if ext == "pdf":
            try:
                from pypdf import PdfReader

                reader = PdfReader(io.BytesIO(data))
                for page in reader.pages:
                    page_text = (page.extract_text() or "").strip()
                    if page_text:
                        chunks.append(page_text[:8000])
            except Exception as exc:
                raise ValueError(f"could not parse PDF: {exc}")
            if not chunks:
                raise ValueError("no text found in PDF (scanned/image-only?)")

        elif ext == "csv":
            try:
                rows = list(csv.reader(io.StringIO(text)))
                if rows:
                    header = [h.strip().lower() for h in rows[0]]
                    for i, row in enumerate(rows[1:], start=2):
                        if not any(c.strip() for c in row):
                            continue
                        chunks.append(" | ".join(f"{h}: {c.strip()}" for h, c in zip(header, row) if c.strip()))
                        for h, c in zip(header, row):
                            val = c.strip()
                            if not val:
                                continue
                            if h in ("type", "room", "service", "item"):
                                facts.append({"entity": "catalog", "key": _slug(val), "value": val})
                            elif h in ("rate", "price", "cost", "amount"):
                                num = re.sub(r"[^\d.]", "", val)
                                if num:
                                    facts.append({"entity": "rate", "key": _slug(rows[i - 1][0]) if rows[i - 1] else "item", "value": num})
            except Exception:
                chunks = [l.strip() for l in lines if l.strip()][:500]

        elif ext == "json":
            try:
                data_obj = json.loads(text)
                chunks = [json.dumps(data_obj, indent=2, ensure_ascii=False)]
                if isinstance(data_obj, dict):
                    for key, value in data_obj.items():
                        if isinstance(value, (str, int, float)) and key not in ("name", "address"):
                            facts.append({"entity": "catalog", "key": _slug(key), "value": str(value)})
            except Exception:
                chunks = [l.strip() for l in lines if l.strip()][:500]
        else:  # txt / md / others
            chunks = [l.strip() for l in lines if l.strip()][:500]

        if not chunks:
            raise ValueError("no parseable content found in file")

        doc_id = f"doc_{uuid.uuid4().hex[:16]}"
        version = 1
        with self.conn:
            self.conn.execute(
                "INSERT INTO documents(id, tenant_id, version, filename, ext, mime, size,"
                " sha256, status, route, storage_path, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, self.tenant_id, version, filename[:255], ext, mime or "application/octet-stream",
                 len(data), _sha256(data), "ingested", "indexed", None, _iso()),
            )
            for ordinal, chunk in enumerate(chunks):
                self.conn.execute(
                    "INSERT INTO chunks(id, tenant_id, doc_id, doc_version, ordinal, text, locator, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (f"chk_{uuid.uuid4().hex[:16]}", self.tenant_id, doc_id, version, ordinal,
                     chunk[:4000], f"{doc_id}@v{version}:row{ordinal + 1}", _iso()),
                )
            for fact in facts:
                self.conn.execute(
                    "INSERT INTO facts(id, tenant_id, entity, key, value, numeric_value, currency,"
                    " valid_from, valid_to, confidence, doc_id, doc_version, locator, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"fact_{uuid.uuid4().hex[:16]}", self.tenant_id, fact["entity"], fact["key"],
                     fact["value"][:1000], None, None, None, None, 0.8, doc_id, version,
                     f"{doc_id}@v{version}", _iso()),
                )
        return {"doc_id": doc_id, "filename": filename, "chunks": len(chunks), "facts": len(facts)}

    def list_documents(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.conn:
            rows = self.conn.execute(
                "SELECT id, filename, ext, size, status, created_at FROM documents WHERE tenant_id=?"
                " ORDER BY created_at DESC LIMIT ?",
                (self.tenant_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_document(self, doc_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM chunks WHERE tenant_id=? AND doc_id=?", (self.tenant_id, doc_id))
            self.conn.execute("DELETE FROM documents WHERE id=? AND tenant_id=?", (doc_id, self.tenant_id))
        return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # retrieval (BM25-style, same as the Telegram agent)
    # ------------------------------------------------------------------ #
    @staticmethod
    def tokenize(text: str) -> List[str]:
        return [
            t for t in re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE)
            if t not in STOPWORDS and len(t) > 1
        ]

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        terms = self.tokenize(query)
        if not terms:
            return []
        with self.conn:
            rows = [dict(r) for r in self.conn.execute(
                "SELECT text, locator FROM chunks WHERE tenant_id=?", (self.tenant_id,)).fetchall()]
        docs = [self.tokenize(r["text"]) for r in rows]
        if not docs:
            return []
        n = len(docs)
        dfs = {t: sum(1 for d in docs if t in d) for t in terms}
        avg_len = sum(len(d) for d in docs) / n
        k1, b = 1.5, 0.75
        results: List[Dict[str, Any]] = []
        for row, tokens in zip(rows, docs):
            score, dl = 0.0, len(tokens) or 1
            for t in terms:
                tf = tokens.count(t)
                if not tf or not dfs.get(t):
                    continue
                idf = max(0.01, (n - dfs[t] + 0.5) / (dfs[t] + 0.5)) ** 0.5
                score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / max(1.0, avg_len)))
            if score > 0:
                results.append({"text": row["text"], "score": round(score, 4), "source_ref": row["locator"]})
        results.sort(key=lambda r: r["score"], reverse=True)
        best = results[0]["score"] if results else 0.0
        for r in results:
            r["score"] = round(r["score"] / best, 4) if best else 0.0
        return results[:limit]

    def knowledge_context(self, query: str, max_chars: int = 1200) -> str:
        """Grounded context block for the LLM prompt."""
        hits = self.search(query, limit=4)
        if not hits:
            return ""
        parts = []
        used = 0
        for h in hits:
            snippet = h["text"][:400]
            if used + len(snippet) > max_chars:
                break
            parts.append(snippet)
            used += len(snippet)
        return "\n\n".join(parts)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass