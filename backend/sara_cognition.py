"""
SARA v6 Cognitive Layer — persistent cognitive desktop operating agent.

This module upgrades full_ai_v3 (SARA v5) from "planner + tools" into a
persistent, context-aware, goal-driven desktop agent. It is ADDITIVE by
design: nothing in full_ai_v3 is removed or renamed, so every v4/v5
compatibility surface (AUTH, REG, EXEC, MEM, LTM, BUS, SESSIONS, llm_plan,
run_plan, stream_desktop) keeps working.

Components
----------
CognitiveStore      SQLite (WAL) persistence: episodic/semantic/procedural/entity
                    memory, task graphs, undo journal, projects, conversation
                    summaries. One DB next to the v5 memory.db.
SemanticRetriever   Local embedding-free semantic scoring (char-ngram cosine +
                    token overlap + recency/frequency/importance/task-relevance).
EntityMemory        Aliased references: "the investor deck" -> real file path.
ProjectMemory       Ongoing projects with files, decisions, milestones.
TaskGraph           Persistent goal -> subgoal -> action/verification DAG with
                    resumable statuses (pending/ready/running/waiting/blocked/
                    completed/failed/cancelled/superseded).
UndoJournal         before_state/action/after_state for every mutating action,
                    with real inverse operations verified after undo.
IntentInterpreter   Classifies utterances (command/question/continuation/
                    correction/undo/preference/...) with LLM + deterministic
                    regex fallback (works offline).
ReferenceResolver   Resolves "it/that/him/the other one/last time/yesterday's
                    file" from conversation + entities + task graph + context.
CognitiveController The OBSERVE->THINK->PLAN->ACT->VERIFY->REFLECT loop that
                    owns the structured goal state and decides next actions.
"""

from __future__ import annotations

import difflib
import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("sara.cognition")

# --------------------------------------------------------------------------- #
# Locations (same workspace as the v5 engine)
# --------------------------------------------------------------------------- #
try:
    from full_ai_v3 import ROOT, STATE  # type: ignore
except Exception:  # standalone/test use
    ROOT = Path.home() / "FullAI_Workspace"
    STATE = ROOT / ".fullai"
    STATE.mkdir(parents=True, exist_ok=True)

COG_DB = Path(__file__).resolve().parent / "sara_cognition.db"

# ============================================================================ #
# 1. COGNITIVE STORE — episodic + semantic + procedural + entity + graph
# ============================================================================ #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
    id INTEGER PRIMARY KEY,
    ts REAL, request TEXT, intent TEXT, goal TEXT, task_id TEXT,
    apps TEXT, files TEXT, urls TEXT, actions TEXT, results TEXT,
    failures TEXT, corrections TEXT, outcome TEXT, evidence TEXT, feedback TEXT
);
CREATE TABLE IF NOT EXISTS semantic(
    id INTEGER PRIMARY KEY,
    kind TEXT, content TEXT, key TEXT, tags TEXT, importance REAL DEFAULT 1.0,
    created REAL, updated REAL, last_used REAL DEFAULT 0, hits INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1, superseded_by INTEGER, source TEXT
);
CREATE TABLE IF NOT EXISTS entities(
    id INTEGER PRIMARY KEY,
    etype TEXT, name TEXT, canonical TEXT, aliases TEXT,
    meta TEXT, created REAL, updated REAL, last_used REAL DEFAULT 0, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS entity_links(
    a INTEGER, b INTEGER, relation TEXT, created REAL,
    PRIMARY KEY(a, b, relation)
);
CREATE TABLE IF NOT EXISTS graphs(
    task_id TEXT PRIMARY KEY, goal TEXT, state TEXT,
    created REAL, updated REAL, data TEXT, summary TEXT, project_id TEXT
);
CREATE TABLE IF NOT EXISTS undo_journal(
    id INTEGER PRIMARY KEY, ts REAL, task_id TEXT, tool TEXT, args TEXT,
    before_state TEXT, after_state TEXT, inverse TEXT, undone INTEGER DEFAULT 0,
    undone_ts REAL, verified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projects(
    id TEXT PRIMARY KEY, name TEXT, objective TEXT, files TEXT, folders TEXT,
    apps TEXT, decisions TEXT, milestones TEXT, unfinished TEXT,
    created REAL, updated REAL, last_active REAL, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS summaries(
    id INTEGER PRIMARY KEY, scope TEXT, scope_id TEXT, ts REAL, text TEXT,
    UNIQUE(scope, scope_id)
);
CREATE INDEX IF NOT EXISTS ix_ep_ts ON episodes(ts DESC);
CREATE INDEX IF NOT EXISTS ix_sem_kind ON semantic(kind, active);
CREATE INDEX IF NOT EXISTS ix_ent_name ON entities(name);
CREATE INDEX IF NOT EXISTS ix_undo_task ON undo_journal(task_id);
"""


class CognitiveStore:
    """Thread-safe persistent store for everything cognitive."""

    def __init__(self, path: Path = COG_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False,
                                  timeout=15, isolation_level=None)
        with self.lock:
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(_SCHEMA)

    # -- episodes ------------------------------------------------------------
    def log_episode(self, request: str, intent: str, goal: str, task_id: str,
                    apps: List[str], files: List[str], urls: List[str],
                    actions: List[dict], results: List[dict],
                    failures: List[str], outcome: str,
                    evidence: List[str], feedback: str = "") -> int:
        now = time.time()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO episodes(ts,request,intent,goal,task_id,apps,files,urls,actions,results,failures,corrections,outcome,evidence,feedback) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, request[:1000], intent, goal[:500], task_id,
                 json.dumps(apps)[:400], json.dumps(files)[:1200],
                 json.dumps(urls)[:600], json.dumps(actions, default=str)[:4000],
                 json.dumps(results, default=str)[:4000], json.dumps(failures)[:1500],
                 "[]", outcome, json.dumps(evidence)[:1500], feedback[:500]))
            return int(cur.lastrowid or 0)

    def update_episode_outcome(self, episode_id: int, outcome: str,
                               feedback: str = "", corrections: Optional[List[str]] = None):
        with self.lock:
            if corrections is not None:
                self.db.execute("UPDATE episodes SET corrections=? WHERE id=?",
                                (json.dumps(corrections)[:1500], episode_id))
            self.db.execute("UPDATE episodes SET outcome=COALESCE(NULLIF(?,''),outcome), "
                            "feedback=CASE WHEN ?<>'' THEN ? ELSE feedback END WHERE id=?",
                            (outcome, feedback, feedback[:500], episode_id))

    def recent_episodes(self, limit: int = 20) -> List[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id, ts, request, intent, goal, task_id, apps, files, outcome, feedback "
                "FROM episodes ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for rid, ts, req, intent, goal, tid, apps, files, outcome, fb in rows:
            out.append({"id": rid, "when": datetime.fromtimestamp(ts).isoformat(timespec="minutes"),
                        "request": req, "intent": intent, "goal": goal, "task_id": tid,
                        "apps": json.loads(apps or "[]"), "files": json.loads(files or "[]"),
                        "outcome": outcome, "feedback": fb})
        return out

    # -- semantic memory -------------------------------------------------------
    def remember(self, kind: str, content: str, key: Optional[str] = None,
                 tags: Tuple[str, ...] = (), importance: float = 1.0,
                 source: str = "user") -> Optional[int]:
        content = " ".join(str(content).split())[:1500]
        if not content or kind not in ("preference", "rule", "fact", "lesson",
                                       "workflow", "context", "decision"):
            return None
        now = time.time()
        tags_s = ",".join(sorted({t.strip().lower() for t in tags if t}))
        with self.lock:
            if key:
                row = self.db.execute(
                    "SELECT id, content FROM semantic WHERE kind=? AND key=? AND active=1",
                    (kind, key)).fetchone()
                if row:
                    if row[1] == content:
                        self.db.execute("UPDATE semantic SET hits=hits+1, updated=? WHERE id=?",
                                        (now, row[0]))
                        return int(row[0])
                    # supersede, never delete
                    self.db.execute("UPDATE semantic SET active=0, superseded_by=NULL, updated=? WHERE id=?",
                                    (now, row[0]))
            cur = self.db.execute(
                "INSERT INTO semantic(kind,content,key,tags,importance,created,updated,source,active) "
                "VALUES(?,?,?,?,?,?,?,?,1)",
                (kind, content, key, tags_s, float(importance), now, now, source))
            return int(cur.lastrowid or 0)

    def _rows(self, kinds: Optional[List[str]] = None) -> List[tuple]:
        q = ("SELECT id, kind, content, key, tags, importance, hits, updated, last_used "
             "FROM semantic WHERE active=1")
        args: Tuple = ()
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args = tuple(kinds)
        with self.lock:
            return self.db.execute(q, args).fetchall()

    def touch(self, ids: List[int]):
        if not ids:
            return
        now = time.time()
        with self.lock:
            self.db.executemany("UPDATE semantic SET last_used=?, hits=hits+1 WHERE id=?",
                                [(now, i) for i in ids])

    def deactivate(self, memory_id: int):
        with self.lock:
            self.db.execute("UPDATE semantic SET active=0, updated=? WHERE id=?",
                            (time.time(), int(memory_id)))

    def bump(self, memory_id: int, delta: float):
        with self.lock:
            self.db.execute("UPDATE semantic SET importance=MAX(0.1, importance+?), updated=? WHERE id=?",
                            (float(delta), time.time(), int(memory_id)))

    def semantic_list(self, kinds: Optional[List[str]] = None, limit: int = 30) -> List[dict]:
        rows = self._rows(kinds)
        rows.sort(key=lambda r: -r[6])
        out = []
        for rid, kind, content, key, tags, imp, hits, updated, lu in rows[:limit]:
            out.append({"id": rid, "kind": kind, "content": content[:300], "key": key,
                        "tags": tags, "importance": imp, "hits": hits,
                        "updated": datetime.fromtimestamp(updated).isoformat(timespec="minutes")})
        return out

    def get_semantic(self, memory_id: int) -> Optional[dict]:
        with self.lock:
            row = self.db.execute(
                "SELECT id, kind, content, key, tags, importance, hits, updated FROM semantic WHERE id=?",
                (int(memory_id),)).fetchone()
        if not row:
            return None
        rid, kind, content, key, tags, imp, hits, updated = row
        return {"id": rid, "kind": kind, "content": content, "key": key, "tags": tags,
                "importance": imp, "hits": hits,
                "updated": datetime.fromtimestamp(updated).isoformat(timespec="minutes")}

    # -- entities --------------------------------------------------------------
    def upsert_entity(self, etype: str, name: str, canonical: Optional[str] = None,
                      aliases: Tuple[str, ...] = (), meta: Optional[dict] = None) -> int:
        now = time.time()
        name = name.strip()
        canonical = (canonical or name).strip()
        with self.lock:
            row = self.db.execute(
                "SELECT id, aliases FROM entities WHERE active=1 AND LOWER(name)=LOWER(?)",
                (name,)).fetchone()
            if row:
                eid, existing = row
                merged = list(json.loads(existing or "[]"))
                for a in aliases:
                    if a and a.lower() not in [x.lower() for x in merged]:
                        merged.append(a)
                self.db.execute(
                    "UPDATE entities SET aliases=?, meta=?, updated=?, last_used=? WHERE id=?",
                    (json.dumps(merged)[:800], json.dumps(meta or {}, default=str)[:1500],
                     now, now, eid))
                return int(eid)
            cur = self.db.execute(
                "INSERT INTO entities(etype,name,canonical,aliases,meta,created,updated,last_used,active) "
                "VALUES(?,?,?,?,?,?,?,?,1)",
                (etype, name, canonical, json.dumps(list(aliases))[:800],
                 json.dumps(meta or {}, default=str)[:1500], now, now, now))
            eid = int(cur.lastrowid or 0)
            for a in aliases:
                alias = a.strip()
                if alias and alias.lower() != name.lower():
                    self.db.execute(
                        "INSERT OR IGNORE INTO entities(etype,name,canonical,aliases,meta,created,updated,last_used,active) "
                        "VALUES(?,?,?,?,?,?,?,?,1)",
                        (etype, alias, canonical, "[]", "{}", now, now, now))
            return eid

    def find_entity(self, text: str, etype: Optional[str] = None) -> Optional[dict]:
        q = "SELECT e.id, e.etype, e.name, e.canonical, e.aliases, e.meta FROM entities e WHERE e.active=1"
        args: Tuple = ()
        if etype:
            q += " AND e.etype=?"
            args = (etype,)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY e.last_used DESC LIMIT 500", args).fetchall()
        ql = text.lower().strip()
        best, best_score = None, 0.0
        for rid, et, name, canonical, aliases_s, meta in rows:
            cands = [name] + json.loads(aliases_s or "[]")
            for c in cands:
                cl = c.lower().strip()
                if not cl:
                    continue
                if ql == cl:
                    s = 1.0
                elif cl in ql or ql in cl:
                    s = 0.85 if len(cl) >= 4 else 0.6
                else:
                    s = difflib.SequenceMatcher(None, cl, ql).ratio()
                if s > best_score:
                    best, best_score = (rid, et, name, canonical, aliases_s, meta), s
        if not best or best_score < 0.55:
            return None
        rid, et, name, canonical, aliases_s, meta = best
        with self.lock:
            self.db.execute("UPDATE entities SET last_used=? WHERE id=?", (time.time(), rid))
        return {"id": rid, "etype": et, "name": name, "canonical": canonical,
                "aliases": json.loads(aliases_s or "[]"), "meta": json.loads(meta or "{}"),
                "score": round(best_score, 2)}

    def link_entities(self, a: int, b: int, relation: str):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO entity_links(a,b,relation,created) VALUES(?,?,?,?)",
                            (int(a), int(b), relation, time.time()))

    # -- undo journal ------------------------------------------------------------
    def log_undo(self, task_id: str, tool: str, args: dict, before: dict,
                 after: dict, inverse: Optional[dict]) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO undo_journal(ts,task_id,tool,args,before_state,after_state,inverse) "
                "VALUES(?,?,?,?,?,?,?)",
                (time.time(), task_id, tool, json.dumps(args, default=str)[:800],
                 json.dumps(before, default=str)[:1200],
                 json.dumps(after, default=str)[:1200],
                 json.dumps(inverse, default=str)[:800] if inverse else None))
            return int(cur.lastrowid or 0)

    def latest_undoable(self) -> Optional[dict]:
        with self.lock:
            row = self.db.execute(
                "SELECT id, ts, task_id, tool, args, before_state, after_state, inverse "
                "FROM undo_journal WHERE undone=0 AND inverse IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        rid, ts, tid, tool, args, before, after, inverse = row
        return {"id": rid, "when": datetime.fromtimestamp(ts).isoformat(timespec="minutes"),
                "task_id": tid, "tool": tool, "args": json.loads(args or "{}"),
                "before_state": json.loads(before or "{}"),
                "after_state": json.loads(after or "{}"),
                "inverse": json.loads(inverse or "null")}

    def mark_undone(self, journal_id: int, verified: bool):
        with self.lock:
            self.db.execute("UPDATE undo_journal SET undone=1, undone_ts=?, verified=? WHERE id=?",
                            (time.time(), 1 if verified else 0, int(journal_id)))

    def undo_history(self, limit: int = 15) -> List[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id, ts, tool, args, undone, verified FROM undo_journal ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [{"id": r[0], "when": datetime.fromtimestamp(r[1]).isoformat(timespec="minutes"),
                 "tool": r[2], "args": json.loads(r[3] or "{}"), "undone": bool(r[4]),
                 "verified": bool(r[5])} for r in rows]

    # -- graphs ---------------------------------------------------------------
    def save_graph(self, data: dict):
        with self.lock:
            self.db.execute(
                "INSERT INTO graphs(task_id,goal,state,created,updated,data,summary,project_id) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET goal=excluded.goal, "
                "state=excluded.state, updated=excluded.updated, data=excluded.data, "
                "summary=excluded.summary, project_id=excluded.project_id",
                (data["task_id"], data["goal"][:500], data.get("state", "active"),
                 data.get("created", time.time()), time.time(),
                 json.dumps(data, default=str)[:60000],
                 data.get("summary", "")[:1000], data.get("project_id")))

    def load_graph(self, task_id: str) -> Optional[dict]:
        with self.lock:
            row = self.db.execute("SELECT data FROM graphs WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def open_graphs(self) -> List[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT task_id, goal, state, updated, summary FROM graphs "
                "WHERE state IN ('active','waiting','blocked') ORDER BY updated DESC LIMIT 20").fetchall()
        return [{"task_id": r[0], "goal": r[1], "state": r[2],
                 "updated": datetime.fromtimestamp(r[3]).isoformat(timespec="minutes"),
                 "summary": r[4]} for r in rows]

    def latest_open_graph(self) -> Optional[dict]:
        ops = self.open_graphs()
        return self.load_graph(ops[0]["task_id"]) if ops else None

    def latest_completed_graph(self) -> Optional[dict]:
        with self.lock:
            row = self.db.execute(
                "SELECT task_id FROM graphs WHERE state IN ('completed','failed') "
                "ORDER BY updated DESC LIMIT 1").fetchone()
        return self.load_graph(row[0]) if row else None

    def find_graph(self, query: str) -> Optional[dict]:
        """Semantic-ish search across all graphs by goal + summary."""
        with self.lock:
            rows = self.db.execute("SELECT task_id, goal, summary FROM graphs ORDER BY updated DESC LIMIT 100").fetchall()
        best, best_score = None, 0.0
        for tid, goal, summary in rows:
            s = SemanticRetriever.similarity(query, f"{goal} {summary or ''}")
            if s > best_score:
                best, best_score = tid, s
        if best and best_score >= 0.25:
            return self.load_graph(best)
        return None

    # -- projects ---------------------------------------------------------------
    def upsert_project(self, name: str, objective: str = "", **fields) -> str:
        pid = "prj_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40]
        now = time.time()
        with self.lock:
            row = self.db.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
            if row:
                sets, args = ["updated=?", "last_active=?"], [now, now]
                for k in ("objective", "files", "folders", "apps", "decisions",
                          "milestones", "unfinished"):
                    if k in fields:
                        sets.append(f"{k}=?")
                        args.append(json.dumps(fields[k], default=str)[:4000]
                                    if isinstance(fields[k], (list, dict)) else str(fields[k])[:2000])
                self.db.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", (*args, pid))
            else:
                self.db.execute(
                    "INSERT INTO projects(id,name,objective,files,folders,apps,decisions,milestones,unfinished,created,updated,last_active,active) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (pid, name[:120], objective[:1000],
                     json.dumps(fields.get("files", []))[:4000],
                     json.dumps(fields.get("folders", []))[:1500],
                     json.dumps(fields.get("apps", []))[:800],
                     json.dumps(fields.get("decisions", []))[:4000],
                     json.dumps(fields.get("milestones", []))[:1500],
                     json.dumps(fields.get("unfinished", []))[:2000],
                     now, now, now))
        return pid

    def find_project(self, query: str) -> Optional[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id, name, objective, files, folders, apps, decisions, milestones, unfinished, updated, last_active "
                "FROM projects WHERE active=1 ORDER BY last_active DESC LIMIT 100").fetchall()
        ql = query.lower().strip()
        best, best_score = None, 0.0
        for r in rows:
            pid, name, obj = r[0], r[1], r[2]
            files = json.loads(r[3] or "[]")
            hay = " ".join([name, obj or ""] + [str(f.get("path", f)) if isinstance(f, dict) else str(f) for f in files[:20]])
            s = SemanticRetriever.similarity(ql, hay)
            if ql and ql in name.lower():
                s = max(s, 0.95)
            if s > best_score:
                best, best_score = r, s
        if not best or best_score < 0.3:
            return None
        return {"id": best[0], "name": best[1], "objective": best[2],
                "files": json.loads(best[3] or "[]"), "folders": json.loads(best[4] or "[]"),
                "apps": json.loads(best[5] or "[]"), "decisions": json.loads(best[6] or "[]"),
                "milestones": json.loads(best[7] or "[]"), "unfinished": json.loads(best[8] or "[]"),
                "last_active": best[10]}

    def all_projects(self) -> List[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id, name, objective, unfinished, last_active FROM projects WHERE active=1 "
                "ORDER BY last_active DESC").fetchall()
        return [{"id": r[0], "name": r[1], "objective": r[2],
                 "unfinished": json.loads(r[3] or "[]"),
                 "last_active": r[4]} for r in rows]

    def attach_file_to_project(self, project_id: str, path: str, role: str = "artifact"):
        with self.lock:
            row = self.db.execute("SELECT files FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                return
            files = json.loads(row[0] or "[]")
            entry = {"path": path, "role": role, "ts": time.time()}
            files = [f for f in files if not (isinstance(f, dict) and f.get("path") == path)]
            files.insert(0, entry)
            self.db.execute("UPDATE projects SET files=?, updated=?, last_active=? WHERE id=?",
                            (json.dumps(files[:60])[:4000], time.time(), time.time(), project_id))

    # -- summaries ---------------------------------------------------------------
    def save_summary(self, scope: str, scope_id: str, text: str):
        with self.lock:
            self.db.execute(
                "INSERT INTO summaries(scope,scope_id,ts,text) VALUES(?,?,?,?) "
                "ON CONFLICT(scope,scope_id) DO UPDATE SET ts=excluded.ts, text=excluded.text",
                (scope, scope_id, time.time(), text[:4000]))

    def get_summary(self, scope: str, scope_id: str) -> Optional[str]:
        with self.lock:
            row = self.db.execute("SELECT text FROM summaries WHERE scope=? AND scope_id=?",
                                  (scope, scope_id)).fetchone()
        return row[0] if row else None

    def stats(self) -> dict:
        with self.lock:
            ep = self.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            sem = dict(self.db.execute(
                "SELECT kind, COUNT(*) FROM semantic WHERE active=1 GROUP BY kind").fetchall())
            ent = self.db.execute("SELECT COUNT(*) FROM entities WHERE active=1").fetchone()[0]
            g = dict(self.db.execute(
                "SELECT state, COUNT(*) FROM graphs GROUP BY state").fetchall())
            prj = self.db.execute("SELECT COUNT(*) FROM projects WHERE active=1").fetchone()[0]
            undo = self.db.execute("SELECT COUNT(*) FROM undo_journal WHERE undone=0").fetchone()[0]
        return {"episodes": ep, "semantic": sem, "entities": ent, "graphs": g,
                "projects": prj, "pending_undos": undo, "db": str(self.path)}

    def close(self):
        with self.lock:
            try:
                self.db.close()
            except Exception:
                pass


# ============================================================================ #
# 2. SEMANTIC RETRIEVER — char-ngram cosine + tokens + recency/frequency/etc.
# ============================================================================ #

class SemanticRetriever:
    """Local, dependency-free semantic scoring.

    score = 0.45*char_cosine + 0.25*token_jaccard + 0.10*importance_norm
          + 0.08*recency + 0.06*frequency + 0.06*entity/kind boost
    No model downloads, works fully offline, deterministic.
    """

    STOP = set("a an the is are was were be been being to of in on at for with and or "
               "not it this that these those i you he she they we do does did done "
               "my your his her their our me him them please can could would will "
               "just very really so now then than as by from".split())

    def __init__(self, store: CognitiveStore):
        self.store = store

    @staticmethod
    def _char_ngrams(text: str, n: int = 3) -> List[str]:
        t = re.sub(r"[^a-z0-9 ]", " ", text.lower())
        t = re.sub(r"\s+", " ", t).strip()
        return [t[i:i + n] for i in range(max(1, len(t) - n + 1))] or [t]

    @staticmethod
    def _tokens(text: str) -> set:
        return {w for w in re.findall(r"[a-z0-9]{2,}", text.lower())}

    @classmethod
    def similarity(cls, a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        ga, gb = cls._char_ngrams(a), cls._char_ngrams(b)
        fa: Dict[str, int] = {}
        fb: Dict[str, int] = {}
        for g in ga:
            fa[g] = fa.get(g, 0) + 1
        for g in gb:
            fb[g] = fb.get(g, 0) + 1
        dot = sum(v * fb.get(k, 0) for k, v in fa.items())
        na = math.sqrt(sum(v * v for v in fa.values())) or 1.0
        nb = math.sqrt(sum(v * v for v in fb.values())) or 1.0
        cos = dot / (na * nb)
        ta, tb = cls._tokens(a) - cls.STOP, cls._tokens(b) - cls.STOP
        jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
        return round(0.72 * cos + 0.28 * jac, 4)

    def recall(self, query: str, kinds: Optional[List[str]] = None, limit: int = 10,
               exclude_kinds_below_importance: float = 0.0) -> List[dict]:
        rows = self.store._rows(kinds)
        now = time.time()
        scored: List[Tuple[float, dict]] = []
        qn = query.strip()
        for rid, kind, content, key, tags, imp, hits, updated, last_used in rows:
            if imp < exclude_kinds_below_importance:
                continue
            sim = self.similarity(qn, f"{content} {key or ''} {tags or ''}")
            if sim <= 0.08:
                continue
            rec = 1.0 / (1 + max(0.0, now - updated) / 86400)
            freq = min(1.0, hits / 10.0)
            impn = min(1.0, imp / 3.0)
            score = 0.62 * sim + 0.10 * impn + 0.06 * rec + 0.04 * freq
            if kind in ("rule", "preference"):
                score += 0.08  # explicit user statements rank above learned heuristics
            scored.append((score, {"id": rid, "kind": kind, "content": content,
                                   "key": key, "score": round(score, 3),
                                   "similarity": sim,
                                   "when": datetime.fromtimestamp(updated).isoformat(timespec="minutes")}))
        scored.sort(key=lambda x: -x[0])
        top = [x[1] for x in scored[:limit]]
        self.store.touch([x[1]["id"] for x in scored[:limit]])
        return top


# ============================================================================ #
# 3. TASK GRAPH — persistent goal DAG
# ============================================================================ #

NODE_STATUSES = ("pending", "ready", "running", "waiting", "blocked",
                 "completed", "failed", "cancelled", "superseded")


@dataclass
class GraphNode:
    id: str
    kind: str                      # goal | subgoal | action | verification
    description: str
    status: str = "pending"
    dependencies: List[str] = field(default_factory=list)
    tool: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    evidence: List[str] = field(default_factory=list)
    retries: int = 0
    responsible: str = ""
    failure_reason: str = ""
    recovery: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def dump(self) -> dict:
        d = asdict(self)
        d["updated_iso"] = datetime.fromtimestamp(self.updated).isoformat(timespec="seconds")
        return d


@dataclass
class TaskGraph:
    task_id: str
    goal: str
    state: str = "active"          # active | waiting | blocked | completed | failed | cancelled
    project_id: Optional[str] = None
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    nodes: Dict[str, GraphNode] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)
    summary: str = ""
    expected_outcome: str = ""
    constraints: List[str] = field(default_factory=list)
    confidence: float = 0.5

    # -- node ops --------------------------------------------------------------
    def add(self, kind: str, description: str, tool: Optional[str] = None,
            args: Optional[dict] = None, deps: Optional[List[str]] = None,
            node_id: Optional[str] = None) -> GraphNode:
        nid = node_id or f"n{len(self.order) + 1}_{uuid.uuid4().hex[:4]}"
        node = GraphNode(id=nid, kind=kind, description=description[:500],
                         tool=tool, args=args or {}, dependencies=list(deps or []))
        self.nodes[nid] = node
        self.order.append(nid)
        return node

    def node(self, nid: str) -> Optional[GraphNode]:
        return self.nodes.get(nid)

    def _deps_satisfied(self, node: GraphNode) -> bool:
        return all((self.nodes.get(d) and self.nodes[d].status == "completed")
                   for d in node.dependencies)

    def next_ready(self, kind: Optional[str] = None) -> Optional[GraphNode]:
        for nid in self.order:
            n = self.nodes[nid]
            if kind and n.kind != kind:
                continue
            if n.status in ("ready", "running") and self._deps_satisfied(n):
                return n
        # promote pending nodes whose deps are done
        for nid in self.order:
            n = self.nodes[nid]
            if kind and n.kind != kind:
                continue
            if n.status == "pending" and self._deps_satisfied(n):
                n.status = "ready"
                return n
        return None

    def pending_nodes(self) -> List[GraphNode]:
        return [self.nodes[nid] for nid in self.order
                if self.nodes[nid].status in ("pending", "ready", "running", "waiting", "blocked")]

    def all_done(self) -> bool:
        return bool(self.order) and all(self.nodes[nid].status in
                                        ("completed", "cancelled", "superseded")
                                        for nid in self.order)

    def progress(self) -> Tuple[int, int]:
        done = sum(1 for nid in self.order
                   if self.nodes[nid].status in ("completed", "cancelled", "superseded"))
        return done, len(self.order)

    def summarize(self) -> str:
        done, total = self.progress()
        pending = [self.nodes[nid].description for nid in self.order
                   if self.nodes[nid].status in ("pending", "ready", "running", "waiting", "blocked")][:5]
        failed = [self.nodes[nid].description for nid in self.order if self.nodes[nid].status == "failed"][:3]
        parts = [f"Goal: {self.goal} ({done}/{total} done, state={self.state})"]
        if pending:
            parts.append("Remaining: " + "; ".join(p[:60] for p in pending))
        if failed:
            parts.append("Failed: " + "; ".join(f[:60] for f in failed))
        return " | ".join(parts)

    def dump(self) -> dict:
        return {"task_id": self.task_id, "goal": self.goal, "state": self.state,
                "project_id": self.project_id, "created": self.created,
                "updated": self.updated, "summary": self.summarize(),
                "expected_outcome": self.expected_outcome,
                "constraints": self.constraints, "confidence": self.confidence,
                "nodes": [self.nodes[nid].dump() for nid in self.order]}

    # -- persistence -----------------------------------------------------------
    def save(self, store: CognitiveStore):
        self.updated = time.time()
        self.summary = self.summarize()
        store.save_graph(self.dump())

    @classmethod
    def restore(cls, data: dict) -> "TaskGraph":
        g = cls(task_id=data["task_id"], goal=data.get("goal", ""),
                state=data.get("state", "active"), project_id=data.get("project_id"),
                created=data.get("created", time.time()), updated=data.get("updated", time.time()),
                summary=data.get("summary", ""), expected_outcome=data.get("expected_outcome", ""),
                constraints=data.get("constraints", []), confidence=data.get("confidence", 0.5))
        for nd in data.get("nodes", []):
            node = GraphNode(id=nd["id"], kind=nd.get("kind", "action"),
                             description=nd.get("description", ""),
                             status=nd.get("status", "pending"),
                             dependencies=nd.get("dependencies", []),
                             tool=nd.get("tool"), args=nd.get("args", {}),
                             outputs=nd.get("outputs", {}), evidence=nd.get("evidence", []),
                             retries=nd.get("retries", 0), responsible=nd.get("responsible", ""),
                             failure_reason=nd.get("failure_reason", ""),
                             recovery=nd.get("recovery", ""),
                             created=nd.get("created", time.time()),
                             updated=nd.get("updated", time.time()))
            g.nodes[node.id] = node
            g.order.append(node.id)
        return g


class GraphManager:
    def __init__(self, store: CognitiveStore):
        self.store = store
        self.lock = threading.RLock()
        self.current: Optional[TaskGraph] = None

    def bootstrap(self):
        """Resume the most recent open graph after restart."""
        data = self.store.latest_open_graph()
        if data:
            try:
                self.current = TaskGraph.restore(data)
                log.info("cognition: resumed task graph %s '%s' (%s)",
                         self.current.task_id, self.current.goal[:60], self.current.state)
            except Exception as e:
                log.warning("cognition: failed to restore graph: %s", e)

    def new_graph(self, goal: str, project_id: Optional[str] = None,
                  expected_outcome: str = "", constraints: Optional[List[str]] = None) -> TaskGraph:
        g = TaskGraph(task_id="t" + uuid.uuid4().hex[:10], goal=goal.strip(),
                      project_id=project_id, expected_outcome=expected_outcome,
                      constraints=constraints or [])
        g.add("goal", goal.strip())
        with self.lock:
            # supersede previous active graph
            if self.current and self.current.state == "active":
                self.current.state = "superseded"
                for n in self.current.nodes.values():
                    if n.status in ("pending", "ready", "running"):
                        n.status = "superseded"
                self.current.save(self.store)
            self.current = g
        g.save(self.store)
        return g

    def attach_plan(self, graph: TaskGraph, steps: List[dict], parent_id: Optional[str] = None):
        """Attach planner steps as action nodes (+ a verification node per subgoal)."""
        parent = parent_id or (graph.order[0] if graph.order else None)
        ids: List[str] = []
        for s in steps:
            n = graph.add("action", s.get("why") or f"{s.get('tool')} {json.dumps(s.get('args', {}))[:80]}",
                          tool=s.get("tool"), args=s.get("args", {}), deps=[parent] if parent else [])
            ids.append(n.id)
        if ids:
            graph.add("verification", "verify " + (parent and graph.nodes[parent].description[:60] or "outcome"),
                      deps=ids)
        graph.save(self.store)

    def record_result(self, graph: TaskGraph, node_id: str, ok: bool,
                      output: dict, error: str = "", evidence: Optional[List[str]] = None):
        node = graph.nodes.get(node_id)
        if not node:
            return
        node.updated = time.time()
        if ok:
            node.status = "completed"
            node.outputs = {k: (str(v)[:200]) for k, v in (output or {}).items()
                            if k in ("verified", "method", "requested", "actual", "path", "url", "note")}
            if evidence:
                node.evidence = [str(e)[:200] for e in evidence][:8]
        else:
            node.status = "failed"
            node.failure_reason = (error or "unknown")[:400]
            node.retries += 1
        graph.save(self.store)

    def fail_current(self, reason: str):
        with self.lock:
            g = self.current
            if not g:
                return
            for n in g.nodes.values():
                if n.status in ("running", "ready"):
                    n.status = "failed"
                    n.failure_reason = reason[:400]
            g.state = "failed"
            g.save(self.store)


# ============================================================================ #
# 4. INTENT INTERPRETER
# ============================================================================ #

@dataclass
class Intent:
    kind: str          # command|question|continuation|correction|undo|preference|memory_store|memory_query|cancel|clarification|comparison|planning|delegation|chat
    text: str
    resolved_text: str = ""
    confidence: float = 0.5
    slots: Dict[str, Any] = field(default_factory=dict)
    ongoing: bool = False
    durable: bool = False


class IntentInterpreter:
    """LLM-assisted classification with a deterministic offline fallback."""

    PATTERNS: List[Tuple[str, re.Pattern]] = [
        ("undo", re.compile(r"\b(undo|revert|rollback|put (it|that) back|restore (what|the) .*(chang|did)|take back)\b", re.I)),
        ("cancel", re.compile(r"^(stop|cancel|abort|never mind|nevermind|forget it)\b", re.I)),
        ("continuation", re.compile(r"\b(continue|carry on|go on|keep going|pick up|resume|where (we|you|i) (stopped|left)|from where .*(stopped|left)|next step|what.*(next|remains|left))\b", re.I)),
        ("memory_query", re.compile(r"^(what (do|did|have) you (remember|know|complete|completed|finish|finished|do|done|learn)|what('s| is| was) (the )?status|show (memory|memories|task|tasks)|task status|what happened|what did (we|i) (do|finish|complete)|where (did|were) we)\b", re.I)),
        ("memory_store", re.compile(r"^(remember|note|keep in mind|don't forget|dont forget)\b", re.I)),
        ("preference", re.compile(r"\b(from now on|always|never|by default|i prefer|i like|my (default|usual|favorite|favourite)|whenever i)\b", re.I)),
        ("correction", re.compile(r"^(no[,!. ]|nope|wrong|not that|that's wrong|thats wrong|i meant|i mean (the )?(other|different)|not this one|the other (one|file|document|version))", re.I)),
        ("question", re.compile(r"^(what|why|how|when|where|which|who|is|are|can|could|do|does|did|should)\b.*\?*$", re.I)),
        ("planning", re.compile(r"\b(plan|how (do|would|can) (we|i)|what('s| is) the (plan|approach)|steps to|roadmap)\b", re.I)),
        ("delegation", re.compile(r"\b(take care of|handle (everything|all|it)|do whatever (is|it) (needed|takes)|deal with|manage (it|everything)|end to end)\b", re.I)),
    ]
    ONGOING = re.compile(r"\b(help me|guide me|walk me|workflow|keep|until|whenever|while i|step by step|throughout|monitor|watch for)\b", re.I)
    DURABLE = re.compile(r"\b(from now on|always|never|remember (that|to|this|my)|i prefer|by default|do not ever|don't ever)\b", re.I)

    def __init__(self, llm: Optional[Callable[..., str]] = None):
        self.llm = llm  # full_ai_v3.llm_call, injected

    def classify(self, text: str) -> Intent:
        t = text.strip()
        for kind, pat in self.PATTERNS:
            m = pat.search(t)
            if m:
                conf = 0.85 if kind in ("undo", "cancel", "memory_store") else 0.7
                return Intent(kind=kind, text=t, resolved_text=t, confidence=conf,
                              slots={"match": m.group(0)}, ongoing=bool(self.ONGOING.search(t)),
                              durable=bool(self.DURABLE.search(t)))
        if self.ONGOING.search(t):
            return Intent(kind="command", text=t, resolved_text=t, confidence=0.6, ongoing=True,
                          durable=bool(self.DURABLE.search(t)))
        if self.llm:
            try:
                raw = self.llm(
                    "Classify the user's utterance for a desktop agent. Reply ONLY JSON: "
                    '{"kind":"command|question|continuation|correction|undo|preference|memory_store|memory_query|cancel|clarification|comparison|planning|delegation|chat",'
                    '"confidence":<0..1>,"core":"<imperative restatement or empty>"}',
                    t, 0.0, 20)
                m = re.search(r"\{.*\}", raw, re.S)
                if m:
                    d = json.loads(m.group(0))
                    kind = d.get("kind", "command")
                    if kind in ("command", "question", "continuation", "correction", "undo",
                                "preference", "memory_store", "memory_query", "cancel",
                                "clarification", "comparison", "planning", "delegation", "chat"):
                        core = str(d.get("core") or "").strip()
                        return Intent(kind=kind, text=t, resolved_text=core or t,
                                      confidence=float(d.get("confidence", 0.75)),
                                      ongoing=bool(self.ONGOING.search(t)),
                                      durable=bool(self.DURABLE.search(t)))
            except Exception as e:
                log.debug("intent llm failed: %s", e)
        return Intent(kind="command", text=t, resolved_text=t, confidence=0.5,
                      ongoing=bool(self.ONGOING.search(t)), durable=bool(self.DURABLE.search(t)))


# ============================================================================ #
# 5. REFERENCE RESOLVER
# ============================================================================ #

class ReferenceResolver:
    """Resolves pronouns/deictic phrases against conversation, entities,
    task graph, recent actions and screen context."""

    IT = re.compile(r"\b(it|that|this|them|the (?:above|aforementioned))\b", re.I)
    FILEISH = re.compile(r"\b(that|this|the) (file|document|doc|pdf|pptx?|xlsx?|presentation|spreadsheet|deck|report|one|version)\b", re.I)
    PERSON = re.compile(r"\b(him|her|them)\b", re.I)
    FOLDER = re.compile(r"\b(that|this|the same) (folder|directory|place|location)\b", re.I)
    OTHER = re.compile(r"\b(the (other|previous|last|first|second|latest|newest|older|newer) (one|file|version|document|tab|window))\b", re.I)
    TEMPORAL = re.compile(r"\b(yesterday|last (time|week|night|session)|earlier|before|previously|the (?:file|doc|deck|report) (?:from|we) .*(?:yesterday|earlier|last))\b", re.I)

    def __init__(self, store: CognitiveStore, retriever: SemanticRetriever,
                 graphs: GraphManager, entities: "EntityMemoryFacade"):
        self.store = store
        self.ret = retriever
        self.graphs = graphs
        self.entities = entities

    def resolve(self, text: str, conversation: List[dict],
                screen: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        original = text
        notes: Dict[str, Any] = {}
        g = self.graphs.current

        # "the other one/version/file" -> alternate among recent candidates
        m = self.OTHER.search(text)
        if m:
            alt = self._alternate(text, conversation, g)
            if alt:
                text = text.replace(m.group(0), alt)
                notes["other_resolved"] = alt

        # explicit file-ish references -> best entity/file from memory
        m = self.FILEISH.search(text)
        if m:
            ref = self._best_reference(text, g, "file")
            if ref:
                text = re.sub(self.FILEISH, f'"{ref}"', text, count=1)
                notes["file_resolved"] = ref

        m = self.FOLDER.search(text)
        if m:
            ref = self._best_reference(text, g, "folder")
            if ref:
                text = re.sub(self.FOLDER, f'"{ref}"', text, count=1)
                notes["folder_resolved"] = ref

        # people
        m = self.PERSON.search(text)
        if m:
            ent = self.store.find_entity(" ".join([c.get("text", "") for c in conversation[-8:]]), etype="person")
            if ent:
                text = re.sub(self.PERSON, ent["canonical"], text, count=1)
                notes["person_resolved"] = ent["canonical"]

        # bare "it/that/this" -> last task object of the current graph
        if re.fullmatch(r"(?i)\s*(yes|yeah|ok|okay|sure|go ahead|do it)?\s*(please\s*)?(it|that|this|them)\s*.?", text) or \
           re.search(r"(?i)\b(open|run|update|save|close|send|delete|print|edit|continue|summarize|summarise|rename|undo)\s+(it|that|this|them)\b", text):
            obj = self._current_object(g)
            if obj:
                text = re.sub(r"(?i)\b(it|that|this|them)\b", obj, text, count=1)
                notes["it_resolved"] = obj

        # temporal references -> find episode/graph from that period
        m = self.TEMPORAL.search(text)
        if m:
            ref = self._temporal_reference(text, conversation)
            if ref:
                text = text.replace(m.group(0), ref)
                notes["temporal_resolved"] = ref

        if text != original:
            log.info("resolver: '%s' -> '%s' (%s)", original[:60], text[:80], notes)
        return text, notes

    def _current_object(self, g: Optional[TaskGraph]) -> Optional[str]:
        if not g:
            return None
        # prefer the most recent completed action with a file-ish output
        for nid in reversed(g.order):
            n = g.nodes[nid]
            if n.status == "completed":
                for k in ("path", "actual", "requested"):
                    v = n.outputs.get(k)
                    if v and re.search(r"\.(pptx?|docx?|xlsx?|pdf|txt|md|csv|zip|png|jpe?g)", str(v), re.I):
                        return str(v)
                v = n.outputs.get("requested") or n.outputs.get("actual")
                if v:
                    return str(v)
        return None

    def _best_reference(self, text: str, g: Optional[TaskGraph], kind: str) -> Optional[str]:
        # 1. entities matching the text
        ent = self.store.find_entity(text)
        if ent and ent["etype"] == kind:
            meta = ent.get("meta", {})
            if meta.get("path"):
                return meta["path"]
            return ent["canonical"]
        # 2. current graph outputs
        ref = self._current_object(g)
        if ref and ((kind == "file" and not ref.endswith(("/", "\\"))) or kind == "folder"):
            return ref
        # 3. semantic recall over recent episodes
        eps = self.store.recent_episodes(12)
        for ep in eps:
            for f in ep.get("files", []):
                return f
        return None

    def _alternate(self, text: str, conversation: List[dict], g: Optional[TaskGraph]) -> Optional[str]:
        """User rejected a candidate; return the next-most-recent alternative."""
        rejected = None
        for c in reversed(conversation[-6:]):
            if c.get("role") == "sara" and re.search(r"\.(pptx?|docx?|xlsx?|pdf)\b", str(c.get("text", "")), re.I):
                m = re.search(r"[\w\-. ]+\.(pptx?|docx?|xlsx?|pdf)", str(c["text"]), re.I)
                if m:
                    rejected = m.group(0).strip()
                    break
        eps = self.store.recent_episodes(15)
        candidates: List[str] = []
        for ep in eps:
            for f in ep.get("files", []):
                if f not in candidates:
                    candidates.append(f)
        if g:
            for nid in reversed(g.order):
                n = g.nodes[nid]
                v = n.outputs.get("path") or n.outputs.get("actual")
                if v and v not in candidates:
                    candidates.append(str(v))
        for c in candidates:
            if not rejected or c.lower() != rejected.lower():
                return c
        return None

    def _temporal_reference(self, text: str, conversation: List[dict]) -> Optional[str]:
        eps = self.store.recent_episodes(40)
        if not eps:
            return None
        # pick the most relevant older episode by semantic similarity
        target = self.ret.similarity(text, " ".join(e["goal"] for e in eps))
        best = None
        for ep in eps[2:]:   # skip the very latest; "yesterday" implies older
            s = self.ret.similarity(text, ep["goal"] + " " + ep["request"])
            if s > 0.2 and (best is None or s > best[0]):
                best = (s, ep)
        if best:
            files = best[1].get("files") or []
            if files:
                return files[0]
            return None
        return None


class EntityMemoryFacade:
    """Thin helper the resolver uses; real entity ops live on the store."""

    def __init__(self, store: CognitiveStore):
        self.store = store

    def remember_file(self, path: str, aliases: Tuple[str, ...] = ()):
        p = Path(path)
        self.store.upsert_entity("file", p.name, canonical=str(p),
                                 aliases=aliases, meta={"path": str(p)})

    def remember_folder(self, path: str, aliases: Tuple[str, ...] = ()):
        p = Path(path)
        self.store.upsert_entity("folder", p.name or str(p), canonical=str(p),
                                 aliases=aliases, meta={"path": str(p)})


# ============================================================================ #
# 6. UNDO ENGINE — real inverses for real actions
# ============================================================================ #

class UndoEngine:
    """Records before/after state for mutating tools and executes real inverses."""

    def __init__(self, store: CognitiveStore):
        self.store = store

    def compute_inverse(self, tool: str, args: dict, before: dict, after: dict) -> Optional[dict]:
        a = _norm(args.get("action", ""))
        if tool == "files":
            if a == "write" and before.get("existed"):
                return {"tool": "files", "args": {"action": "write", "path": args.get("path"),
                                                  "content": before.get("content", "")[:100000]}}
            if a == "write" and not before.get("existed"):
                return {"tool": "files", "args": {"action": "delete", "path": args.get("path")}}
            if a == "rename":
                # args["path"] is the ORIGINAL (pre-rename) path
                return {"tool": "files", "args": {"action": "rename", "path": after.get("actual"),
                                                  "new_name": Path(str(args.get("path", ""))).name}}
            if a == "move" and not args.get("copy"):
                return {"tool": "files", "args": {"action": "move", "path": after.get("actual"),
                                                  "dest": str(before.get("requested", ""))}}
            if a == "delete":
                return None  # recycle-bin restore handled by OS; can't automate safely
        if tool == "window":
            if a == "close":
                return {"tool": "app", "args": {"action": "open", "name": before.get("process") or before.get("title", "")}}
        return None

    def capture_before(self, tool: str, args: dict) -> dict:
        a = _norm(args.get("action", ""))
        before: dict = {}
        try:
            if tool == "files":
                p = Path(str(args.get("path", "")).strip().strip('"')).expanduser()
                if a == "write" and p.exists():
                    if p.stat().st_size <= 200_000 and p.suffix.lower() in (".txt", ".md", ".csv", ".json", ".log", ".ini", ".py", ".js", ".ts", ".html", ".css", ".yaml", ".yml"):
                        before = {"existed": True, "content": p.read_text("utf-8", errors="replace")[:200000]}
                    else:
                        before = {"existed": True, "size": p.stat().st_size, "binary": True}
                else:
                    before = {"existed": p.exists()}
            elif tool == "window":
                try:
                    from full_ai_v3 import WATCHER  # type: ignore
                    ctx = WATCHER.current or {}
                    before = {"title": ctx.get("title"), "process": ctx.get("process")}
                except Exception:
                    pass
        except Exception as e:
            log.debug("undo capture_before: %s", e)
        return before

    def capture_after(self, tool: str, args: dict, output: dict) -> dict:
        return {"requested": output.get("requested"), "actual": output.get("actual"),
                "verified": output.get("verified"), "method": output.get("method")}

    def execute_undo(self) -> dict:
        rec = self.store.latest_undoable()
        if not rec:
            return {"ok": False, "error": "No reversible action found to undo.", "verified": False}
        inv = rec.get("inverse") or {}
        tool_name, inv_args = inv.get("tool"), inv.get("args", {})
        if not tool_name:
            return {"ok": False, "error": "This action cannot be undone automatically.", "verified": False}
        try:
            from full_ai_v3 import REG  # type: ignore
            t = REG.get(tool_name)
            if t is None:
                return {"ok": False, "error": f"Tool '{tool_name}' no longer registered.", "verified": False}
            out = t.run(inv_args)
            ok = bool(out.get("verified", True))
            self.store.mark_undone(int(rec["id"]), ok)
            return {"ok": ok, "undid": f"{rec['tool']} {rec['args'].get('action', '')}",
                    "via": f"{tool_name} {inv_args.get('action', '')}", "output": out,
                    "verified": ok}
        except Exception as e:
            return {"ok": False, "error": f"Undo failed: {e}", "verified": False}


def _norm(s: Any) -> str:
    return " ".join(str(s or "").lower().split()).strip(" .,!?")


# ============================================================================ #
# 7. COGNITIVE CONTROLLER — the executive loop
# ============================================================================ #

class CognitiveController:
    """Owns the structured goal state and the
    OBSERVE -> INTERPRET -> RESOLVE -> RECALL -> PLAN -> ACT -> VERIFY ->
    REFLECT -> REMEMBER loop. Wraps (does not replace) the v5 planner/executor."""

    def __init__(self, engine: Any):
        # engine = full_ai_v3 module
        self.eng = engine
        self.store = CognitiveStore()
        self.retriever = SemanticRetriever(self.store)
        self.graphs = GraphManager(self.store)
        self.entities = EntityMemoryFacade(self.store)
        self.resolver = ReferenceResolver(self.store, self.retriever, self.graphs, self.entities)
        self.undo = UndoEngine(self.store)
        self.interpreter = IntentInterpreter(llm=getattr(engine, "llm_call", None))
        self.conversation: List[dict] = []
        self.lock = threading.RLock()
        self._bootstrap()

    # ------------------------------------------------------------------ state
    def _bootstrap(self):
        self.graphs.bootstrap()

    def goal_state(self, request: str, intent: Intent, notes: Dict[str, Any]) -> dict:
        g = self.graphs.current
        done, total = g.progress() if g else (0, 0)
        pending = [n.description for n in g.pending_nodes()][:6] if g else []
        failed = [n.description for n in g.nodes.values() if n.status == "failed"][:4] if g else []
        mem = self.retriever.recall(request, limit=8)
        prefs = [m["content"] for m in mem if m["kind"] in ("preference", "rule")]
        screen = {}
        try:
            screen = {k: self.eng.WATCHER.current.get(k) for k in ("app", "title", "url", "host")}
        except Exception:
            pass
        return {
            "user_intent": intent.kind,
            "primary_goal": g.goal if g else request,
            "subgoals": pending,
            "constraints": (g.constraints if g else []) + prefs,
            "preferences": prefs[:6],
            "referenced_entities": notes,
            "assumptions": [],
            "known_facts": [m["content"] for m in mem if m["kind"] == "fact"][:6],
            "required_information": [],
            "completed_steps": total and f"{done}/{total}" or "0/0",
            "failed_steps": failed,
            "pending_steps": pending,
            "dependencies": [],
            "blockers": [],
            "evidence": [m["content"][:120] for m in mem[:4]],
            "expected_outcome": (g.expected_outcome if g else ""),
            "current_context": screen,
            "confidence": intent.confidence,
            "authorization_state": "locked" if self.eng.AUTH.locked else "normal",
            "next_best_action": pending[0] if pending else None,
            "task_id": g.task_id if g else None,
            "graph_state": g.state if g else None,
        }

    # ------------------------------------------------------------- main entry
    def handle(self, text: str, trigger: str = "user") -> str:
        with self.lock:
            return self._handle_locked(text, trigger)

    def _handle_locked(self, text: str, trigger: str) -> str:
        eng = self.eng
        text = (text or "").strip()
        if not text:
            return ""
        self.conversation.append({"role": "user", "text": text, "ts": time.time()})
        self.conversation = self.conversation[-40:]

        intent = self.interpreter.classify(text)

        # ---- instant-handled intents that need no planner -------------------
        if intent.kind == "undo":
            reply = self._do_undo()
            self.conversation.append({"role": "sara", "text": reply, "ts": time.time()})
            return reply

        if intent.kind == "memory_query":
            reply = self._answer_memory_query(text)
            self.conversation.append({"role": "sara", "text": reply, "ts": time.time()})
            return reply

        if intent.kind == "cancel":
            reply = self._do_cancel()
            self.conversation.append({"role": "sara", "text": reply, "ts": time.time()})
            return reply

        # ---- resolve references BEFORE anything else -----------------------
        screen = {}
        try:
            screen = eng.WATCHER.refresh(True) or eng.WATCHER.current or {}
        except Exception:
            screen = {}
        resolved, notes = self.resolver.resolve(text, self.conversation, screen)
        if resolved != text:
            eng.MEM.set("resolved_intent", resolved[:200], quiet=True)
        intent.resolved_text = resolved

        # ---- durable preference/rule capture -------------------------------
        if intent.kind in ("preference", "memory_store") or intent.durable:
            self._learn_durable(resolved, intent)
            if intent.kind in ("preference", "memory_store"):
                reply = ("Noted and remembered permanently." if intent.kind == "preference"
                         else "Got it — I'll remember that.")
                self.conversation.append({"role": "sara", "text": reply, "ts": time.time()})
                return reply

        # ---- corrections ----------------------------------------------------
        if intent.kind == "correction":
            reply = self._handle_correction(resolved, intent)
            self.conversation.append({"role": "sara", "text": reply, "ts": time.time()})
            return reply

        # ---- continuation: resume the open graph ----------------------------
        if intent.kind == "continuation":
            reply = self._handle_continuation(resolved, screen)
            self.conversation.append({"role": "sara", "text": reply, "ts": time.time()})
            return reply

        # ---- default: goal-driven plan through the v5 planner ---------------
        state = self.goal_state(resolved, intent, notes)
        eng.BUS.emit("intent_detected", kind=intent.kind, confidence=intent.confidence,
                     resolved=_redact(resolved)[:200], state=json.dumps(state, default=str)[:1500])

        # Project linking: does this look like an existing project?
        project = self.store.find_project(resolved)
        if project and intent.kind in ("command", "delegation", "planning", "continuation"):
            state["project"] = {"id": project["id"], "name": project["name"]}

        # ensure a task graph exists for multi-step goals
        graph = self._ensure_graph(resolved, intent, state, project)

        reply = self._plan_and_act(resolved, intent, state, graph, screen, trigger)
        self.conversation.append({"role": "sara", "text": reply, "ts": time.time()})
        return reply

    # --------------------------------------------------------------- helpers
    def _ensure_graph(self, resolved: str, intent: Intent,
                      state: dict, project: Optional[dict]) -> TaskGraph:
        g = self.graphs.current
        multi = intent.kind in ("delegation", "planning") or intent.ongoing or \
            len(resolved.split()) > 6 or bool(project)
        if g and g.state == "active" and self._same_goal(g.goal, resolved):
            return g
        if g and g.state == "active" and not multi:
            # unrelated one-shot command: keep graph open, don't supersede on a whim
            return g
        if multi or not g or g.state != "active":
            g = self.graphs.new_graph(
                resolved, project_id=project["id"] if project else None,
                expected_outcome=state.get("expected_outcome", ""))
            eng_log = "cognition: new graph %s for '%s'"
            log.info(eng_log, g.task_id, resolved[:60])
            try:
                self.eng.BUS.emit("goal_created", task_id=g.task_id, goal=resolved[:200])
            except Exception:
                pass
        return g

    @staticmethod
    def _same_goal(a: str, b: str) -> bool:
        return SemanticRetriever.similarity(a, b) >= 0.72

    def _plan_and_act(self, resolved: str, intent: Intent, state: dict,
                      graph: TaskGraph, screen: dict, trigger: str) -> str:
        eng = self.eng
        extra = {"cognitive_state": {k: state[k] for k in
                                     ("user_intent", "primary_goal", "pending_steps",
                                      "completed_steps", "preferences", "task_id") if state.get(k)},
                 "reference_notes": state.get("referenced_entities"),
                 "recent_conversation": [
                     {"role": c["role"], "text": str(c.get("text", ""))[:160]}
                     for c in self.conversation[-8:]]}
        try:
            plan = eng.PLANNER.plan(resolved, screen, extra=extra)
        except eng.ActionFailed as e:
            return f"Planning failed: {e}"
        eng.BUS.emit("plan", goal=_redact(resolved), source=plan.source,
                     steps=[s.dump() for s in plan.steps], say=plan.say,
                     task_id=graph.task_id if graph else None)
        if plan.say and not plan.steps:
            self._remember_episode(resolved, intent, graph, [], [], "answered", [plan.say])
            return plan.say

        # attach plan steps to the graph
        parent = graph.nodes[graph.order[0]].id if graph and graph.order else None
        node_ids: List[str] = []
        if graph and plan.steps:
            for s in plan.steps:
                n = graph.add("action", s.why or f"{s.tool}", tool=s.tool, args=s.args,
                              deps=[parent] if parent else [])
                node_ids.append(n.id)
            graph.save(self.store)

        # execute (Executor.run is wrapped by the integration to journal undo state)
        results = eng.EXEC.run(plan.steps, trigger=trigger, ctx=screen)

        # learn + persist
        status = eng.LEARN.after_task(resolved, plan.steps, results, trigger,
                                      graph.task_id if graph else "-",
                                      screen, {"learn": plan.learn})
        outcome = "completed" if status == "completed" else ("failed" if all(
            not r.get("ok") for r in results) and results else status)
        if graph:
            for nid, rec in zip(node_ids, results):
                self.graphs.record_result(graph, nid, bool(rec.get("ok")),
                                          rec.get("output") or {}, str(rec.get("error") or ""))
            if graph.all_done():
                graph.state = "completed" if outcome == "completed" else "failed"
            graph.save(self.store)
        self._remember_episode(resolved, intent, graph, plan.steps, results, outcome, [])

        # verification pass: expected vs actual
        self._verify(graph, results, plan)

        reply = eng.AGENT._report(plan, results) if hasattr(eng.AGENT, "_report") else (
            "Done." if all(r.get("ok") for r in results) else "Finished with errors.")
        return reply

    def _verify(self, graph: Optional[TaskGraph], results: List[dict], plan):
        """Compare expected vs actual; emit verification events."""
        ok = bool(results) and all(r.get("ok") for r in results)
        try:
            self.eng.BUS.emit("verification_passed" if ok else "verification_failed",
                              task_id=graph.task_id if graph else None,
                              steps=len(results))
        except Exception:
            pass
        if not ok and graph:
            # remember what failed for the next attempt
            self.store.remember("lesson",
                                "Task '" + graph.goal[:80] + "' failed at: " +
                                "; ".join(f"{r.get('tool')}: {str(r.get('error'))[:100]}"
                                          for r in results if not r.get("ok"))[:400],
                                tags=("auto-lesson",), importance=0.8, source="learned")

    def _remember_episode(self, request: str, intent: Intent, graph: Optional[TaskGraph],
                          steps, results: List[dict], outcome: str, evidence: List[str]):
        try:
            screen = self.eng.WATCHER.current or {}
        except Exception:
            screen = {}
        apps = [screen.get("app")] if screen.get("app") else []
        urls = [screen.get("url")] if screen.get("url") else []
        files = []
        for r in results:
            o = r.get("output") or {}
            for k in ("requested", "actual", "path"):
                v = o.get(k)
                if v and re.search(r"(\.[A-Za-z0-9]{2,4}$|[:\\/])", str(v)):
                    files.append(str(v))
        actions = [{"tool": s.tool if hasattr(s, "tool") else s.get("tool"),
                    "args": _redact(getattr(s, "args", s) if not isinstance(s, dict) else s.get("args"))}
                   for s in steps]
        eid = self.store.log_episode(
            request=request, intent=intent.kind, goal=graph.goal if graph else request,
            task_id=graph.task_id if graph else "-", apps=apps, files=files, urls=urls,
            actions=actions, results=[{"tool": r.get("tool"), "ok": r.get("ok"),
                                       "error": str(r.get("error") or "")[:120]} for r in results],
            failures=[str(r.get("error"))[:200] for r in results if not r.get("ok")],
            outcome=outcome, evidence=evidence)
        # attach artifacts to project
        if graph and graph.project_id and files:
            for f in files[:5]:
                self.store.attach_file_to_project(graph.project_id, f)
                self.entities.remember_file(f)
        return eid

    # ------------------------------------------------------------ undo/cancel
    def _do_undo(self) -> str:
        out = self.undo.execute_undo()
        if out.get("ok"):
            self.store.remember("lesson", "User requested undo; the previous action was reverted via " +
                                str(out.get("via", "")), tags=("undo",), importance=0.6, source="learned")
            return f"Undone: {out.get('undid')}. Verified: {out.get('verified')}."
        return str(out.get("error") or "Couldn't undo that.")

    def _do_cancel(self) -> str:
        eng = self.eng
        msg = []
        if eng.EXEC.busy.is_set():
            eng.EXEC.stop()
            msg.append("Stopped the running actions.")
        g = self.graphs.current
        if g and g.state == "active":
            g.state = "cancelled"
            for n in g.nodes.values():
                if n.status in ("pending", "ready", "running", "waiting", "blocked"):
                    n.status = "cancelled"
            g.save(self.store)
            msg.append(f"Cancelled task '{g.goal[:60]}'.")
        if eng.SESSIONS.active():
            eng.SESSIONS.finish("cancelled", quiet=True)
            msg.append("Closed the task session.")
        return " ".join(msg) or "Nothing was running."

    # ------------------------------------------------------- memory answers
    def _answer_memory_query(self, text: str) -> str:
        parts: List[str] = []
        g = self.graphs.current
        if g:
            done, total = g.progress()
            pending = [n.description for n in g.pending_nodes()][:4]
            parts.append(f"Current task: '{g.goal[:70]}' ({done}/{total} steps done, {g.state})." +
                         (" Remaining: " + "; ".join(p[:50] for p in pending) if pending else ""))
        elif (lg := self.store.latest_completed_graph()):
            parts.append(f"Last finished task: '{lg['goal'][:70]}' ({lg['state']}).")
        mem = self.retriever.recall(text, limit=5)
        if mem:
            parts.append("Related memories: " + "; ".join(
                f"{m['kind']}: {m['content'][:70]}" for m in mem[:3]))
        eps = self.store.recent_episodes(3)
        if eps:
            parts.append("Recent activity: " + "; ".join(
                f"{e['request'][:40]} ({e['outcome']})" for e in eps[:2]))
        return "\n".join(parts) if parts else "I don't have any memory of previous tasks yet."

    # ------------------------------------------------------------- correction
    def _handle_correction(self, resolved: str, intent: Intent) -> str:
        g = self.graphs.current
        # mark recent failed/incorrect node and learn
        lesson_target = g.goal[:80] if g else "the previous action"
        if g:
            for nid in reversed(g.order):
                n = g.nodes[nid]
                if n.status == "completed":
                    n.status = "superseded"
                    break
            g.save(self.store)
        self.store.remember("lesson",
                            f"Correction for '{lesson_target}': user said '{resolved[:150]}'. "
                            f"The previous target/approach was wrong.",
                            tags=("correction",), importance=1.4, source="user")
        try:
            self.eng.LEARN.apply_correction(resolved)
        except Exception:
            pass
        # try to re-resolve with "other" semantics and re-run
        alt, notes = self.resolver.resolve("the other one " + resolved, self.conversation, {})
        if notes.get("other_resolved"):
            return f"Got it — using {notes['other_resolved']} instead. Say 'continue' to proceed."
        return "Understood — I've noted the correction. What should I use instead?"

    # ---------------------------------------------------------- continuation
    def _handle_continuation(self, resolved: str, screen: dict) -> str:
        g = self.graphs.current
        if not g or g.state not in ("active", "waiting", "blocked"):
            # find an older graph semantically
            g2 = self.store.find_graph(resolved) or self.store.latest_completed_graph()
            if g2 and g2.get("state") in ("active", "waiting", "blocked"):
                g = TaskGraph.restore(g2)
                self.graphs.current = g
            elif g2:
                summary = g2.get("summary") or g2.get("goal", "")
                return (f"That task is already closed ({g2.get('state')}). Summary: {summary[:200]} "
                        f"Tell me what's next.")
            else:
                return "I don't have an unfinished task to continue. What would you like to start?"
        pending = g.pending_nodes()
        done, total = g.progress()
        if not pending:
            return f"'{g.goal[:60]}' looks complete ({done}/{total}). Say 'task status' for details."
        # resume through planner with graph context
        state = self.goal_state(resolved, Intent(kind="continuation", text=resolved, resolved_text=resolved), {})
        reply = self._plan_and_act(resolved, Intent(kind="continuation", text=resolved,
                                                    resolved_text=resolved, confidence=0.8),
                                   state, g, screen, "user")
        return f"Continuing '{g.goal[:50]}' ({done}/{total} done). {reply}".strip()

    # ------------------------------------------------------------- learning
    def _learn_durable(self, resolved: str, intent: Intent):
        body = re.sub(r"(?i)^(remember|note|keep in mind|don't forget|dont forget)\s+(that\s+)?", "", resolved).strip()
        if not body:
            return
        kind = "preference" if re.search(r"(?i)\b(prefer|like|favorite|favourite|usual|default|call me)\b", body) else "rule"
        mid = self.store.remember(kind, body, tags=("user-stated",), importance=2.0)
        try:
            self.eng.LTM.remember(kind, body, tags=("user-stated",))
        except Exception:
            pass
        if mid:
            try:
                self.eng.BUS.emit("memory_learned", kind=kind, content=body[:200])
            except Exception:
                pass
            log.info("cognition: learned %s: %s", kind, body[:80])

    # ------------------------------------------------- stock-path bridge
    def note_external_outcome(self, goal: str, reply: str, ctx: Optional[dict] = None):
        """Record an episode for goals handled by the stock v5 path, so the
        cognitive layer learns from them too (episodes + project artifacts)."""
        try:
            ctx = ctx or (self.eng.WATCHER.current or {})
            files = re.findall(r"[\w:\\/. -]+\.(?:pptx?|docx?|xlsx?|pdf|txt|md|csv|zip|png|jpe?g)",
                               reply or "", re.I)
            eid = self.store.log_episode(
                request=goal, intent="command", goal=goal, task_id="-",
                apps=[ctx.get("app")] if ctx.get("app") else [],
                files=files[:5], urls=[ctx.get("url")] if ctx.get("url") else [],
                actions=[], results=[], failures=[],
                outcome="completed" if not re.search(r"(?i)couldn't|failed", reply or "") else "failed",
                evidence=[(reply or "")[:200]])
            g = self.graphs.current
            if g and g.project_id and files:
                for f in files[:3]:
                    self.store.attach_file_to_project(g.project_id, f)
            return eid
        except Exception as e:
            log.debug("note_external_outcome: %s", e)
            return None

    # ------------------------------------------------------------ explainability
    def why_remembered(self, memory_id: int) -> str:
        m = self.store.get_semantic(int(memory_id))
        if not m:
            return "I don't have that memory."
        src = "you told me" if "user-stated" in (m.get("tags") or "") else "I learned it from our tasks"
        return (f"I remember '{m['content'][:120]}' because {src} "
                f"(kind={m['kind']}, importance={m['importance']}, used {m['hits']}x, "
                f"last updated {m['updated']}).")


# --------------------------------------------------------------------------- #
# redaction re-export for local use
# --------------------------------------------------------------------------- #
try:
    from full_ai_v3 import _redact  # type: ignore  # noqa: F401
except Exception:
    _SECRET_VALS = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,})")
    def _redact(obj: Any) -> Any:  # type: ignore[misc]
        if isinstance(obj, str):
            return _SECRET_VALS.sub("[REDACTED]", obj)
        if isinstance(obj, dict):
            return {k: _redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact(x) for x in obj]
        return obj


# ============================================================================ #
# 8. SINGLETON + v5 INTEGRATION HOOKS (called from full_ai_v3)
# ============================================================================ #
_COG: Optional[CognitiveController] = None
_COG_LOCK = threading.Lock()


def get_cognition(engine: Any) -> CognitiveController:
    global _COG
    if _COG is None:
        with _COG_LOCK:
            if _COG is None:
                _COG = CognitiveController(engine)
    return _COG


def cognitive_handle(text: str, trigger: str = "user") -> str:
    """Entry point for the Agent.handle hook in full_ai_v3."""
    if _COG is None:
        return ""   # not wired; caller falls back to stock behaviour
    return _COG.handle(text, trigger)


def cognitive_status() -> dict:
    if _COG is None:
        return {"enabled": False}
    return {"enabled": True, **_COG.store.stats(),
            "task": _COG.graphs.current.dump() if _COG.graphs.current else None}
