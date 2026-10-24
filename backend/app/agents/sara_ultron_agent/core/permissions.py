"""Explicit, independent, revocable permissions for the always-on features.

Persistent vision and an always-on microphone are only acceptable if the user
can see exactly what is on, turn each one off on its own, and know that turning
it off actually stopped it. This module is the single place that decision lives.

Three gates, deliberately independent:

``microphone``
    the always-on voice loop may open the input device
``screen``
    the observer may read the active window, its title and its accessibility
    tree
``control``
    Ultron may move the mouse, type and hotkey

Two properties make this worth having as a module rather than three booleans
scattered in handlers:

**Default off, and visible.** Everything starts denied. A gate that is granted
is granted *by the user*, through the authenticated API, and the current state
is a first-class read (``GET /ultron/permissions``) so the UI can render three
independent switches rather than one "trust me" toggle.

**Revocation is immediate and enforced at the edge.** Every consumer calls
:func:`require` on the hot path, so a revoke takes effect on the very next
frame - the observer loop checks it between polls and the voice loop checks it
between utterances. Revocation also publishes on the bus, which is what lets the
UI drop a live indicator the moment the switch is thrown rather than when the
next poll happens to notice.

Raw audio and raw screenshots are never stored here. This module records only
*that* a capability is permitted, never what it saw.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("sara.ultron.permissions")

#: The complete set of gates. Anything not listed here cannot be granted.
GATES = ("microphone", "screen", "control")

#: Gates that require a reason, because they are the invasive ones.
SENSITIVE_GATES = frozenset({"microphone", "screen"})

#: Bounded audit log of every change. Kept in memory: this is a transparency
#: feature for "what did I just switch on", not a compliance record.
AUDIT_LIMIT = 200


class PermissionDenied(PermissionError):
    """Raised when a capability is used without the user having granted it."""


@dataclass
class GateState:
    allowed: bool = False
    since: Optional[float] = None
    reason: str = ""
    updated: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "since": self.since,
            "since_age_s": round(time.time() - self.since, 1) if self.since else None,
            "reason": self.reason,
            "updated": self.updated,
        }


class PermissionRegistry:
    """Thread-safe gate state, optionally mirrored to disk."""

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self._gates: Dict[str, GateState] = {g: GateState() for g in GATES}
        self._audit: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self._store_path = store_path
        self._load()

    # -- persistence ----------------------------------------------------
    def _load(self) -> None:
        """Restore previous grants so a restart does not silently revoke them.

        A grant that does not survive a restart would be worse than confusing:
        the mic switch would appear off while the loop was still running.
        """
        if not self._store_path:
            return
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - a bad file must not break boot
            log.warning("Permission store unreadable (%s); starting all denied.",
                        type(exc).__name__)
            return
        for name, entry in (raw.get("gates") or {}).items():
            if name not in self._gates or not isinstance(entry, dict):
                continue
            if entry.get("allowed"):
                self._gates[name] = GateState(
                    allowed=True,
                    since=entry.get("since"),
                    reason=str(entry.get("reason") or "restored after restart"),
                    updated=time.time(),
                )
                log.info("Permission %s restored as granted after restart.", name)

    def _persist(self) -> None:
        if not self._store_path:
            return
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "updated": time.time(),
                "gates": {n: {"allowed": g.allowed, "since": g.since,
                              "reason": g.reason}
                          for n, g in self._gates.items()},
            }
            self._store_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not persist permissions: %s", exc)

    # -- reads ----------------------------------------------------------
    def allowed(self, gate: str) -> bool:
        with self._lock:
            state = self._gates.get(gate)
            return bool(state and state.allowed)

    def require(self, gate: str, what: str = "") -> None:
        """Raise unless ``gate`` is granted. Call this on the hot path."""
        if gate not in self._gates:
            raise PermissionError(f"Unknown permission gate {gate!r}.")
        if not self.allowed(gate):
            suffix = f" to {what}" if what else ""
            raise PermissionDenied(
                f"Permission '{gate}' has not been granted{suffix}. "
                f"Grant it explicitly via POST /ultron/permissions/{gate}."
            )

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "gates": {n: g.as_dict() for n, g in self._gates.items()},
                "any_granted": any(g.allowed for g in self._gates.values()),
                "all_granted": all(g.allowed for g in self._gates.values()),
                "updated": time.time(),
            }

    def audit(self, limit: int = 25) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._audit[-limit:])[::-1]

    # -- writes ---------------------------------------------------------
    def set(self, gate: str, allowed: bool, reason: str = "",
            actor: str = "operator") -> Dict[str, Any]:
        """Grant or revoke one gate. Returns the new state.

        Idempotent: granting an already-granted gate reports ``changed: False``
        rather than pretending something happened, because a UI that flashes
        "enabled!" for a no-op is worse than one that stays quiet.
        """
        if gate not in self._gates:
            raise ValueError(
                f"Unknown permission gate {gate!r}. Use one of: {', '.join(GATES)}."
            )
        now = time.time()
        with self._lock:
            state = self._gates[gate]
            changed = state.allowed != bool(allowed)
            if changed:
                state.allowed = bool(allowed)
                state.since = now if allowed else None
                state.reason = reason or ("granted by operator" if allowed else "revoked by operator")
            state.updated = now
            entry = {
                "t": now,
                "gate": gate,
                "allowed": bool(allowed),
                "changed": changed,
                "actor": actor,
                "reason": reason,
            }
            self._audit.append(entry)
            if len(self._audit) > AUDIT_LIMIT:
                del self._audit[: len(self._audit) - AUDIT_LIMIT]
            if changed:
                self._persist()
            result = {**state.as_dict(), "changed": changed}
        if changed:
            log.info("Permission %s -> %s by %s (%s)", gate,
                     "GRANTED" if allowed else "REVOKED", actor, reason or "-")
            _publish(gate, bool(allowed), reason, actor)
        return result

    def revoke_all(self, actor: str = "global-stop") -> Dict[str, bool]:
        """Revoke everything. Used by the global stop button."""
        return {gate: self.set(gate, False, "global stop", actor)["allowed"]
                for gate in GATES}

    def disable_sensitive(self, actor: str = "global-stop") -> Dict[str, bool]:
        """Revoke only the invasive gates, leaving desktop control alone.

        Stop-all should silence the mic and the eye without also breaking an
        in-flight keyboard action the user explicitly approved.
        """
        return {gate: self.set(gate, False, "global stop", actor)["allowed"]
                for gate in SENSITIVE_GATES}


def _publish(gate: str, allowed: bool, reason: str, actor: str) -> None:
    """Tell the rest of the process, over the bus, that a gate moved.

    Imported lazily so this module stays importable (and testable) without the
    bus being constructed.
    """
    try:
        from .bus import bus

        bus.publish("permissions", "changed" if allowed else "revoked",
                    gate=gate, allowed=allowed, reason=reason, actor=actor)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a grant
        log.debug("Could not publish permission change: %s", exc)


def _default_store_path() -> Optional[Path]:
    """Where the grants are mirrored.

    ``$FULLAI_HOME`` is the same root the memory store uses, so Ultron's state
    stays in one place. If it is unset the registry simply stays in memory,
    which is honest: a restart then revokes, and the UI will say so.
    """
    root = os.environ.get("FULLAI_HOME", "").strip()
    if not root:
        return None
    return Path(root) / "sara_ultron_agent" / "permissions.json"


#: Process-wide registry. One set of gates per backend process, by design: a
#: second worker must not be able to hold permissions the first one revoked.
permissions = PermissionRegistry(_default_store_path())
