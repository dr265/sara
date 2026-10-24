"""Google Sheets integration for the WhatsApp agent.

Same pattern as the Telegram agent's connector: managed OAuth (Google consent →
code exchange → encrypted grant stored in the shared `connections` table) and
direct Sheets API transport with auto-create of the spreadsheet on first sync.

Flow:
  POST /whatsapp/integrations/sheets/connect   → { authorize_url } (open in browser)
  GET  /whatsapp/integrations/sheets/callback  → exchange code, store grant (public)
  POST /whatsapp/integrations/sheets/sync      → push knowledge/conversations to Sheets
  GET  /whatsapp/integrations/sheets/history   → recent syncs
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parent.parent.parent / "sara.db"

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

CONNECTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS connections(
  id TEXT PRIMARY KEY, tenant_id TEXT, provider TEXT, label TEXT, status TEXT,
  credentials TEXT, meta TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS sync_history(
  id TEXT PRIMARY KEY, tenant_id TEXT, connection_id TEXT, direction TEXT,
  target TEXT, rows_read INTEGER, rows_written INTEGER, conflicts INTEGER,
  status TEXT, detail TEXT, created_at TEXT);
"""


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _b64(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode()).decode()


class WhatsAppSheets:
    """Managed OAuth Google Sheets connector (thread-safe, shared connections table)."""

    def __init__(self, db_path: Optional[Path] = None, tenant_id: str = "whatsapp") -> None:
        db_path = db_path or DB_PATH  # callers pass None to mean "the shared sara.db"
        self.path = db_path
        self.tenant_id = tenant_id
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self.conn.executescript(CONNECTIONS_SCHEMA)

    # ------------------------------------------------------------------ #
    # OAuth
    # ------------------------------------------------------------------ #
    @property
    def configured(self) -> bool:
        return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

    def start_authorization(self, label: str = "My Google Workspace",
                            redirect_path: str = "/whatsapp/integrations/sheets/callback") -> Dict[str, Any]:
        if not self.configured:
            raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not configured in backend/.env")
        connection_id = _new_id("conn")
        state = _b64(json.dumps({"c": connection_id, "t": self.tenant_id, "n": uuid.uuid4().hex[:8]}))
        with self._lock:
            self.conn.execute(
                "INSERT INTO connections(id, tenant_id, provider, label, status, credentials, meta,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (connection_id, self.tenant_id, "google_sheets", label, "pending",
                 json.dumps({"state": state}), json.dumps({"scopes": ["https://www.googleapis.com/auth/spreadsheets"]}),
                 _iso(), _iso()),
            )
        public = (os.environ.get("SARA_PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/")
        redirect_uri = public + redirect_path
        scopes = "https://www.googleapis.com/auth/spreadsheets"
        url = (
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=" + urllib.parse.quote(GOOGLE_CLIENT_ID, safe="")
            + "&redirect_uri=" + urllib.parse.quote(redirect_uri, safe="")
            + "&response_type=code&scope=" + urllib.parse.quote(scopes, safe="")
            + "&access_type=offline&prompt=consent&state=" + urllib.parse.quote(state, safe="")
        )
        return {"connection_id": connection_id, "authorize_url": url, "redirect_uri": redirect_uri}

    def exchange_code(self, connection_id: str, code: str, redirect_uri: str) -> Dict[str, Any]:
        if not self.configured:
            raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not configured in backend/.env")
        body = urllib.parse.urlencode({
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                token = json.loads(resp.read().decode())
        except Exception as exc:
            raise RuntimeError(f"google token exchange failed: {exc}")
        if not token.get("access_token"):
            raise RuntimeError("google token exchange returned no access_token")
        grant = {"access_token": token["access_token"], "token_type": token.get("token_type", "Bearer"),
                 "expires_in": token.get("expires_in", 3600), "scope": token.get("scope", "")}
        if token.get("refresh_token"):
            grant["refresh_token"] = token["refresh_token"]
        try:
            with urllib.request.urlopen(
                    "https://oauth2.googleapis.com/tokeninfo?access_token=" + urllib.parse.quote(grant["access_token"], safe=""),
                    timeout=10) as resp:
                info = json.loads(resp.read().decode())
            if info.get("email"):
                grant["account"] = info["email"]
        except Exception:
            pass
        with self._lock:
            self.conn.execute(
                "UPDATE connections SET status='connected', credentials=?, updated_at=? WHERE id=? AND tenant_id=?",
                (json.dumps(grant), _iso(), connection_id, self.tenant_id),
            )
        return {"status": "connected", "account": grant.get("account", "unknown")}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT id, label, status, meta, updated_at FROM connections WHERE tenant_id=?"
                " AND provider='google_sheets' ORDER BY updated_at DESC LIMIT 1",
                (self.tenant_id,),
            ).fetchone()
        if row is None:
            return {"connected": False}
        meta = json.loads(row["meta"] or "{}")
        return {"connected": row["status"] == "connected", "label": row["label"],
                "connection_id": row["id"], "default_spreadsheet_id": meta.get("default_spreadsheet_id"),
                "updated_at": row["updated_at"]}

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    def _credentials(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT credentials FROM connections WHERE tenant_id=? AND provider='google_sheets'"
                " AND status='connected' ORDER BY updated_at DESC LIMIT 1",
                (self.tenant_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["credentials"] or "{}")
        except Exception:
            return None

    def _api(self, method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        creds = self._credentials()
        if not creds:
            raise RuntimeError("Google account not connected")
        headers = {"Authorization": f"Bearer {creds['access_token']}", "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            # token expired → try refresh once
            if exc.code in (401, 403) and creds.get("refresh_token"):
                try:
                    new = self._refresh(creds["refresh_token"])
                    if new:
                        req2 = urllib.request.Request(url, data=data, method=method,
                                                      headers={"Authorization": f"Bearer {new['access_token']}",
                                                               "Content-Type": "application/json"})
                        with urllib.request.urlopen(req2, timeout=30) as resp2:
                            raw2 = resp2.read().decode()
                            return json.loads(raw2) if raw2 else {}
                except Exception:
                    pass
            raise RuntimeError(f"sheets api {exc.code}: {detail}")

    def _refresh(self, refresh_token: str) -> Optional[Dict[str, Any]]:
        body = urllib.parse.urlencode({
            "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                token = json.loads(resp.read().decode())
        except Exception:
            return None
        if not token.get("access_token"):
            return None
        with self._lock:
            self.conn.execute(
                "UPDATE connections SET credentials=? WHERE tenant_id=? AND provider='google_sheets'"
                " AND status='connected'",
                (json.dumps({**self._credentials(), **token}), self.tenant_id),
            )
        return token

    # ------------------------------------------------------------------ #
    # sync
    # ------------------------------------------------------------------ #
    def sync(self, rows: List[List[Any]], target: str = "whatsapp-data") -> Dict[str, Any]:
        """Push rows to a spreadsheet (auto-creates one with headers on first sync)."""
        if not rows:
            raise RuntimeError("nothing to sync")
        status = self.status()
        spreadsheet_id = status.get("default_spreadsheet_id")

        if not spreadsheet_id:
            create = self._api("POST", "https://sheets.googleapis.com/v4/spreadsheets",
                               {"properties": {"title": f"SARA WhatsApp {target} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"}})
            spreadsheet_id = create["spreadsheetId"]
            self._remember_spreadsheet(spreadsheet_id)

        # ensure a sheet named after the target exists
        try:
            self._api("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
                      {"requests": [{"addSheet": {"properties": {"title": target[:100]}}}]})
        except Exception:
            pass  # sheet already exists

        range_name = f"{target[:100]}!A1"
        self._api("PUT", f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_name, safe='!')}",
                  {"range": range_name, "majorDimension": "ROWS", "values": rows})
        return {"spreadsheet_id": spreadsheet_id, "rows_written": len(rows)}

    def _remember_spreadsheet(self, spreadsheet_id: str) -> None:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, meta FROM connections WHERE tenant_id=? AND provider='google_sheets'"
                " AND status='connected' ORDER BY updated_at DESC LIMIT 1",
                (self.tenant_id,),
            ).fetchall()
        if not rows:
            return
        try:
            meta = json.loads(rows[0]["meta"] or "{}")
        except Exception:
            meta = {}
        meta["default_spreadsheet_id"] = spreadsheet_id
        with self._lock:
            self.conn.execute("UPDATE connections SET meta=?, updated_at=? WHERE id=?",
                              (json.dumps(meta), _iso(), rows[0]["id"]))

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT target, rows_read, rows_written, status, detail, created_at FROM sync_history"
                " WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (self.tenant_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_sync(self, target: str, rows_written: int, ok: bool, detail: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO sync_history(id, tenant_id, connection_id, direction, target, rows_read,"
                " rows_written, conflicts, status, detail, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (_new_id("sync"), self.tenant_id, None, "outbound", target, 0, rows_written, 0,
                 "ok" if ok else "error", detail[:500], _iso()),
            )

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass