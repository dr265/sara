"""SQLite JSON document repository for EduLearner AI learner state.

One table (``edulearner_documents``) stores JSON documents per user. Each user
has one row per collection so writes stay atomic per collection and the store
works on the same SQLite conventions as the rest of the SARA backend
(WAL mode, busy timeout).

Collections: profile, skills, gaps, roadmap, plan, progress, assessments,
memory, practice_history, remediation, adaptations, chat_history, reports.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, event, text

logger = logging.getLogger("sara.edulearner")

BACKEND_DIR = Path(__file__).resolve().parents[3]
DEFAULT_DB = BACKEND_DIR / "edulearner.db"

COLLECTIONS = (
    "profile", "skills", "gaps", "roadmap", "plan", "progress", "assessments",
    "memory", "practice_history", "remediation", "adaptations", "chat_history",
    "reports",
)


class Repository:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or DEFAULT_DB
        self.engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"timeout": 30}
        )

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        with self.engine.begin() as connection:
            connection.execute(text(
                """
                CREATE TABLE IF NOT EXISTS edulearner_documents (
                    user_id    TEXT NOT NULL,
                    collection TEXT NOT NULL,
                    payload    TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (user_id, collection)
                )
                """
            ))

    # -- document operations ---------------------------------------------------
    def put(self, collection: str, user_id: str, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO edulearner_documents (user_id, collection, payload, updated_at) "
                    "VALUES (:u, :c, :p, datetime('now')) "
                    "ON CONFLICT(user_id, collection) DO UPDATE SET "
                    "payload=excluded.payload, updated_at=excluded.updated_at"
                ),
                {"u": user_id, "c": collection, "p": data},
            )

    def get(self, collection: str, user_id: str) -> Optional[Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT payload FROM edulearner_documents "
                     "WHERE user_id = :u AND collection = :c"),
                {"u": user_id, "c": collection},
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            logger.warning("edulearner: corrupt document %s/%s", user_id, collection)
            return None

    def delete_user(self, user_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM edulearner_documents WHERE user_id = :u"),
                {"u": user_id},
            )

    def users(self) -> List[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT DISTINCT user_id FROM edulearner_documents ORDER BY user_id")
            ).fetchall()
        return [row[0] for row in rows]

    # -- log-style collections (append-only, capped) ----------------------------
    def append(self, collection: str, user_id: str, item: Any,
               limit: int = 500) -> List[Any]:
        items = self.get(collection, user_id)
        if not isinstance(items, list):
            items = []
        items.append(item)
        items = items[-limit:]
        self.put(collection, user_id, items)
        return items

    def health(self) -> str:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return "up"
        except Exception:  # noqa: BLE001
            return "down"


_repo: Optional[Repository] = None


def get_repository() -> Repository:
    global _repo
    if _repo is None:
        _repo = Repository()
    return _repo
