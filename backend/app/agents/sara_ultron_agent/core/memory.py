"""Ultron long-term memory - a thin adapter over SARA's PersistentMemory.

SARA already ships a per-agent memory store in
``app.core.memory_manager.PersistentMemory``: JSON files under
``$FULLAI_HOME/memory/<agent_id>/`` holding past experiences and learned
behavioural rules. Ultron reuses it rather than shipping a parallel database.

Two additions on top of the shared store, both additive:

* a ``threading.Lock`` - the base store does read-modify-write without one,
  and Ultron writes from a background executor thread;
* a lexical ``recall`` scorer. The base ``get_context_for_prompt`` matches
  keywords by naive substring, which both over- and under-matches. Ultron
  scores experiences by overlapping tokens so recall stays ordered and
  bounded.

The base class is left untouched - other agents keep using it unchanged.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from app.core.memory_manager import PersistentMemory

from .tenancy import LOCAL_TENANT_ID

log = logging.getLogger("sara.ultron.memory")

#: agent_id used for the on-disk memory folder.
AGENT_MEMORY_ID = "sara_ultron_agent"

#: How many past experiences ``recall`` may return.
RECALL_LIMIT = 5

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Characters that only ever appear in serialised payloads (a tool's error text
# is often a raw JSON error envelope). A learned rule is injected into every
# future planning prompt, so brace noise and truncated payload fragments must
# not survive into it.
_STRUCTURAL_CHARS = "{}[]\"'`\\"

# A run of characters long enough to be a hash, key or base64 blob.
_OPAQUE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")

#: Minimum length for a sanitized failure to be worth learning from.
_MIN_LESSON_CHARS = 12


#: Unicode replacement characters and stray control bytes. A rule is injected
#: into every future planning prompt, so a mangled em-dash from an upstream
#: gateway must not ride along with it.
_JUNK_CHARS = "\ufffd\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f"


def clean_text(text: str) -> str:
    """Drop replacement characters and control bytes, then collapse whitespace."""
    out = text or ""
    for char in _JUNK_CHARS:
        out = out.replace(char, " ")
    return " ".join(out.split())


def sanitize_lesson(text: str, limit: int = 200) -> str:
    """Reduce a raw failure string to a short, readable, structural-free line.

    Tool failures arrive as prose with JSON envelopes embedded in them, e.g.
    ``Delegation failed: HTTPException: 500: LLM Error: {"error":{"message":
    "This premium model requires...``. Persisting that verbatim produces a rule
    that is both unreadable and full of half a payload, so the structural
    characters are stripped, opaque tokens are masked, and the result is cut at
    a word boundary instead of mid-token.
    """
    cleaned = (text or "")
    for char in _STRUCTURAL_CHARS:
        cleaned = cleaned.replace(char, " ")
    cleaned = _OPAQUE_TOKEN_RE.sub("[opaque]", cleaned)
    cleaned = clean_text(cleaned)
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"

# Very common words carry no routing signal; excluding them stops
# "the user asked to search the web" from matching every memory on disk.
_STOPWORDS = frozenset(
    """a an and are as at be but by do does for from has have how i in is it
    its of on or that the their then there this to was were what when where
    which who will with you your""".split()
)


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS]


def _haystack(exp: Dict[str, Any]) -> str:
    """Flatten an experience's goal + plan into one searchable string.

    ``PersistentMemory.save_experience`` stores the plan as a list of *dicts*,
    so its elements are not guaranteed to be strings. Every element is coerced
    before joining, otherwise reading back a record this agent itself wrote
    would raise on ``' '.join`` as soon as a real run had a plan.
    """
    parts: List[str] = [str(exp.get("goal", ""))]
    plan = exp.get("plan")
    if isinstance(plan, (list, tuple)):
        for step in plan:
            if isinstance(step, str):
                parts.append(step)
            else:
                try:
                    parts.append(json.dumps(step, ensure_ascii=False, default=str))
                except (TypeError, ValueError):
                    parts.append(str(step))
    elif plan is not None:
        parts.append(str(plan))
    return " ".join(parts)


class UltronMemory:
    """Read/write wrapper around SARA's shared per-agent memory store.

    Tenant isolation comes free from the store's own namespacing: the folder is
    ``$FULLAI_HOME/memory/<agent_id>/``, so naming the instance per tenant keeps
    each customer's experiences and learned rules physically separate. No schema
    change and no shared-file filtering was needed - the seam was already there.
    """

    def __init__(self, agent_id: str = AGENT_MEMORY_ID,
                 owner_id: str = LOCAL_TENANT_ID) -> None:
        self._lock = threading.Lock()
        self.owner_id = str(owner_id or LOCAL_TENANT_ID)
        # The LOCAL tenant keeps the historical folder name, so an existing
        # install does not appear to lose all its memory after an upgrade.
        self._store = PersistentMemory(
            agent_id if self.owner_id == LOCAL_TENANT_ID
            else f"{agent_id}_{self._safe_tenant(self.owner_id)}"
        )

    @staticmethod
    def _safe_tenant(owner_id: str) -> str:
        """Filesystem-safe tenant key.

        Clerk ids are opaque strings, so they can contain characters that are
        illegal in a path. Reduce to a stable, reversible-enough token; two
        tenants must never collide onto one folder.
        """
        import hashlib
        cleaned = "".join(c for c in owner_id if c.isalnum() or c in "-_")
        if cleaned and len(cleaned) <= 32:
            return cleaned
        return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32]

    # -- properties -----------------------------------------------------
    @property
    def memory_dir(self) -> Path:
        return self._store.memory_dir

    @property
    def experience_file(self) -> Path:
        return self._store.experience_file

    # -- reads ----------------------------------------------------------
    def _load(self, path: Path, default: Any) -> Any:
        try:
            # utf-8-sig: the store was once written with a UTF-8 BOM, which
            # makes strict utf-8 json.loads fail on every read.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            log.warning("Ultron memory: unreadable store at %s - using default", path)
            return default
        # Older builds could persist a bare JSON string (e.g. an error text
        # learned as a "skill"). Wrap it instead of discarding the content.
        if isinstance(data, str):
            # Legacy bare-string store - keep the content, normalized.
            data = [data]
        elif not isinstance(data, list):
            return default
        return data

    def experiences(self) -> List[Dict[str, Any]]:
        """All stored experiences, with unusable records filtered out.

        The shared store is a plain JSON list with no schema, so a hand-edited
        or partially-written file can contain non-dict entries. Callers treat
        every element as a record, so the filtering happens once, here.
        """
        data = self._load(self._store.experience_file, [])
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict)]

    # -- structured learned rules -----------------------------------------
    # Mirrors Brahma-Echo's core/learned_rules.py semantics (structured
    # records with id/category/origin/active + toggle/delete + numbered
    # prompt injection) while storing inside this agent's existing store.
    # Legacy plain-string entries are migrated on first read, never
    # discarded.
    def _raw_rules(self) -> List[Dict[str, Any]]:
        """Rules as structured records, migrating legacy strings."""
        data = self._load(self._store.skills_file, [])
        rules: List[Dict[str, Any]] = []
        changed = False
        for entry in data:
            if isinstance(entry, dict) and entry.get("rule"):
                rules.append(entry)
                continue
            if isinstance(entry, str) and entry.strip():
                rules.append({
                    "id": uuid.uuid4().hex[:8],
                    "rule": entry.strip(),
                    "category": "general",
                    "origin": "auto_learn",
                    "active": True,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                })
                changed = True
        if changed:
            try:
                self._store.skills_file.write_text(
                    json.dumps(rules, indent=2), encoding="utf-8")
            except OSError as exc:
                log.warning("Ultron memory: could not migrate rules: %s",
                            type(exc).__name__)
        return rules

    def _save_rules(self, rules: List[Dict[str, Any]]) -> None:
        try:
            self._store.skills_file.write_text(
                json.dumps(rules, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("Ultron memory: could not save rules: %s",
                        type(exc).__name__)

    def rules(self) -> List[str]:
        """Active rule texts (compatibility view over the structured store)."""
        return [r["rule"] for r in self._raw_rules() if r.get("active", True)]

    def recall(self, query: str, limit: int = RECALL_LIMIT) -> List[Dict[str, Any]]:
        """Return past experiences ranked by lexical overlap with ``query``.

        Falls back to the most recent experiences when nothing matches, so a
        brand-new goal still gets some grounding rather than nothing.
        """
        exps = self.experiences()
        if not exps:
            return []

        wanted = set(_tokens(query))
        scored: List[tuple[int, int, Dict[str, Any]]] = []
        for idx, exp in enumerate(exps):
            overlap = len(wanted & set(_tokens(_haystack(exp))))
            if overlap:
                scored.append((overlap, idx, exp))

        if not scored:
            return exps[-limit:]
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [exp for _, _, exp in scored[:limit]]

    def context(self, goal: str) -> str:
        """Prompt-ready memory block: learned rules + the most relevant past runs."""
        rules = self.rules()
        hits = self.recall(goal)
        if not rules and not hits:
            return "No prior Ultron memory for this task."

        parts = ["PAST LEARNINGS & RULES:"]
        if rules:
            # Numbered block, exactly the format Brahma-Echo injects from
            # core/learned_rules.py get_prompt_injections().
            parts.append("USER PREFERENCES & LEARNED BEHAVIORAL DIRECTIVES:")
            parts.append("\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)))
        if hits:
            parts.append("SIMILAR PAST EXPERIENCES:")
            for exp in hits:
                result = "SUCCESS" if exp.get("success") else f"FAILED ({exp.get('errors', '')})"
                parts.append(f"- Goal: {exp.get('goal', '')} | Result: {result}")
        return "\n".join(parts)

    # -- writes ---------------------------------------------------------
    def remember(self, goal: str, plan: List[Dict[str, Any]], success: bool,
                 error_logs: str = "") -> None:
        """Persist the outcome of a run (delegates to SARA's shared store)."""
        with self._lock:
            try:
                self._store.save_experience(goal, plan, success, error_logs)
            except OSError as exc:
                log.warning("Ultron memory: could not save experience: %s", type(exc).__name__)

    def learn_rule(self, rule: str, category: str = "general",
                   origin: str = "auto_learn") -> bool:
        """Record a behavioural rule as a structured record.

        Returns True when newly added; re-adding an existing rule re-activates
        it (Brahma-Echo learned_rules.add_rule semantics).
        """
        # Normalised once, here, so every writer (the tool, the API route,
        # auto_learn) stores the same clean text.
        rule = clean_text(rule)
        if not rule:
            return False
        with self._lock:
            rules = self._raw_rules()
            for existing in rules:
                if existing.get("rule", "").lower() == rule.lower():
                    if not existing.get("active", True):
                        existing["active"] = True
                        existing["updated_at"] = time.time()
                        self._save_rules(rules)
                        return True
                    return False
            rules.append({
                "id": uuid.uuid4().hex[:8],
                "rule": rule,
                "category": category or "general",
                "origin": origin or "user_correction",
                "active": True,
                "created_at": time.time(),
                "updated_at": time.time(),
            })
            self._save_rules(rules)
        return True

    def toggle_rule(self, rule_id: str) -> bool:
        """Activate/deactivate a rule by id. False when the id is unknown."""
        with self._lock:
            rules = self._raw_rules()
            for existing in rules:
                if existing.get("id") == rule_id:
                    existing["active"] = not existing.get("active", True)
                    existing["updated_at"] = time.time()
                    self._save_rules(rules)
                    return True
        return False

    def delete_rule(self, rule_id: str) -> bool:
        """Delete a rule permanently by id. False when the id is unknown."""
        with self._lock:
            rules = self._raw_rules()
            remaining = [r for r in rules if r.get("id") != rule_id]
            if len(remaining) != len(rules):
                self._save_rules(remaining)
                return True
        return False

    def list_rules_full(self) -> List[Dict[str, Any]]:
        """All rule records (including inactive) for inspection surfaces."""
        return self._raw_rules()

    def forget(self, timestamp: float, goal: str = "") -> bool:
        """Delete one stored experience.

        Matched on the record's exact timestamp, with the goal text as a
        secondary confirmation when the caller supplies it. Timestamps are
        written by the store itself, so this cannot delete the wrong run by
        index drift the way a positional delete could. Returns False when
        nothing matched, rather than reporting a deletion that did not happen.
        """
        with self._lock:
            try:
                data = json.loads(self._store.experience_file.read_text(
                    encoding="utf-8-sig"))
            except (OSError, ValueError):
                log.warning("Ultron memory: unreadable store - nothing forgotten")
                return False
            if not isinstance(data, list):
                return False
            keep = [
                record for record in data
                if not (
                    isinstance(record, dict)
                    and float(record.get("timestamp", -1)) == float(timestamp)
                    and (not goal or str(record.get("goal", "")) == goal)
                )
            ]
            if len(keep) == len(data):
                return False
            try:
                self._store.experience_file.write_text(
                    json.dumps(keep, indent=2), encoding="utf-8")
            except OSError as exc:
                log.warning("Ultron memory: could not forget: %s", type(exc).__name__)
                return False
        return True

    def auto_learn(self, goal: str, error: str) -> bool:
        """Turn a genuine, specific failure into a durable rule.

        Deliberately conservative, because a bad rule is injected into every
        future prompt and cannot be unlearned cheaply. A rule is only written
        when the failure is:

        * substantive after sanitising (not a bare error code, and not the
          remains of a JSON error envelope);
        * not a user decision - "denied by the user" records the operator's
          choice, not an agent mistake, and learning it would push future runs
          to avoid actions that are actually fine;
        * not a transient condition, which self-corrects on the next attempt.
        """
        raw = (error or "").strip()
        if len(raw) < 8:
            return False

        low = raw.lower()
        # A refusal is a valid outcome, not a lesson.
        for marker in ("denied by the user", "denied by user", "stopped by user",
                       "stopped by the user", "manual override stop"):
            if marker in low:
                return False
        # Transient conditions resolve themselves; persisting them would make
        # the agent refuse work that would have succeeded on a retry.
        for marker in ("timed out", "timeout", "try again", "temporarily",
                       "rate limit", "too many requests", "connection reset"):
            if marker in low:
                return False

        # Sanitise before the length test: an unsanitized payload can be long
        # while carrying almost no readable meaning.
        lesson = sanitize_lesson(raw)
        if len(lesson) < _MIN_LESSON_CHARS:
            return False
        subject = sanitize_lesson(goal, limit=80) or "a similar task"
        return self.learn_rule(f"When '{subject}' fails, the cause was: {lesson}")

    # -- tool shim ------------------------------------------------------
    def as_tool(self, args: Dict[str, Any]) -> str:
        """Handler for the ``ultron_remember`` tool.

        Supports ``action`` = remember (default) | toggle_rule | delete_rule |
        list_rules, matching Brahma-Echo's rule-management verbs.
        """
        args = args or {}
        action = str(args.get("action", "remember") or "remember").strip().lower()
        # The planner invents action names for the remember path - observed in
        # a live run as `action: "goal"`. Those are normalised so a legitimate
        # remember call is not rejected over wording. Anything else is
        # reported: guessing at an unrecognised verb could turn a mistyped
        # management call into a write, which is the worse failure.
        if action in ("remember", "goal", "note", "preference", "remember_rule", ""):
            action = "remember"
        if action in ("toggle_rule", "delete_rule"):
            rule_id = str(args.get("rule_id", "")).strip()
            if not rule_id:
                return "A non-empty 'rule_id' is required."
            ok = self.toggle_rule(rule_id) if action == "toggle_rule" \
                else self.delete_rule(rule_id)
            return (f"Rule {rule_id} {'toggled' if action == 'toggle_rule' else 'deleted'}."
                    if ok else f"Rule ID '{rule_id}' not found.")
        if action == "list_rules":
            records = self.list_rules_full()
            if not records:
                return "No rules stored yet."
            lines = [
                f"{r['id']} | {'active' if r.get('active', True) else 'inactive'}"
                f" | {r.get('category', 'general')} | {r.get('rule', '')[:80]}"
                for r in records
            ]
            return "\n".join(lines)
        if action != "remember":
            return (f"Unknown action '{action}'. Use remember, list_rules, "
                    "toggle_rule or delete_rule.")
        # The `rule` branch is checked first, because the tool's own contract
        # says `rule` alone is a valid call. It used to sit behind the goal
        # check, so the documented usage was rejected for a missing goal and no
        # rule was ever stored.
        if str(args.get("rule", "")).strip():
            return "Rule learned." if self.learn_rule(str(args["rule"])) \
                else "Rule already known."
        goal = str(args.get("goal", "")).strip()
        if not goal:
            return ("ultron_remember needs a non-empty 'rule' to store a "
                    "behavioural directive, or a non-empty 'goal'.")
        # A call that names a goal but reports no outcome is a statement to
        # carry forward, not the result of a run. Defaulting `success` to True
        # here used to record an experience for a task that was never executed
        # - labeled as a success, no less - which then inflated the success
        # rate and fed a fabricated memory into every future plan.
        if "success" not in args:
            return ("Stored as a standing rule." if self.learn_rule(goal)
                    else "An identical rule already existed.")
        success = bool(args.get("success"))
        self.remember(goal, [], success, sanitize_lesson(str(args.get("errors", ""))))
        return "Experience recorded."

    def snapshot(self) -> Dict[str, Any]:
        """Safe summary for /capabilities and /health - never returns content."""
        exps = self.experiences()
        successes = sum(1 for e in exps if isinstance(e, dict) and e.get("success"))
        return {
            "available": True,
            "memory_dir": str(self.memory_dir),
            "experiences": len(exps),
            "rules": len(self.rules()),
            "success_rate": round(successes / len(exps), 3) if exps else None,
        }
