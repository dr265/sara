"""Screen understanding, cheapest tier first.

The instruction this module implements is "do not run an expensive vision model
unless something relevant changed". So the pipeline is a ladder, and it climbs
only as far as it must:

1. **Accessibility tree** - UI Automation (installed here) gives real control
   names, roles and values with no pixels and no model. This answers "is there a
   button called Skip Ad?" for free.
2. **Application facts** - what the adapters already know (window title, tab,
   URL). Often the whole answer.
3. **Screenshot** - only when the tiers above cannot answer the question, and
   even then downscaled and transient.

Two rules make it safe to run continuously:

**Nothing is retained.** A screenshot exists as bytes inside one call, is
optionally sent to a model, and is dropped. Nothing is written to disk and
nothing is cached between polls. The only thing that survives is a small
*description* - control names, roles, a short text summary - which is what the
HUD and the voice loop need.

**A screenshot is only taken when the question needs pixels.** ``analyse()``
takes the reason it was called and refuses the screenshot tier for a question
the accessibility tree can answer. If a caller asks for vision with no context
change and no specific question, it gets a refusal rather than a capture.

The ladder is also what makes this cheap: on an idle screen, tier 1 returns the
same tree as last time, the diff is empty, and no model is called at all.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .permissions import permissions

log = logging.getLogger("sara.ultron.vision")

#: Accessibility elements kept per read. Enough to answer real questions,
#: bounded so a pathological app (a 50,000-node tree) cannot stall the loop.
MAX_ELEMENTS = 120

#: Longest control name retained. Long accessibility names are descriptions,
#: not labels, and they bloat every downstream prompt.
NAME_LIMIT = 80

#: If the last tree's digest is unchanged, vision is a no-op and says so.
TIER_ACCESSIBILITY = "accessibility"
TIER_APP_FACTS = "app_facts"
TIER_SCREENSHOT = "screenshot"
TIER_NONE = "none"

#: Environment flag. Screenshot capture is opt-in even with the permission
#: granted, because it is the tier that touches pixels.
ALLOW_SCREENSHOT = os.environ.get("ULTRON_ALLOW_SCREENSHOT", "").lower() in (
    "1", "true", "yes",
)


@dataclass
class VisionResult:
    tier: str = TIER_NONE
    question: str = ""
    elements: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    changed: bool = False
    screenshot_taken: bool = False
    duration_ms: float = 0.0
    digest: str = ""
    reason: str = ""
    errors: List[str] = field(default_factory=list)
    #: The context revision these elements were read from. Carried so a
    #: consumer can refuse to act on a tree that describes a window the user
    #: has already left - "the second one" resolved against the previous
    #: window's controls is a confident answer to the wrong question.
    revision: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "question": self.question,
            "elements": self.elements[:MAX_ELEMENTS],
            "element_count": len(self.elements),
            "summary": self.summary,
            "changed": self.changed,
            "screenshot_taken": self.screenshot_taken,
            "duration_ms": round(self.duration_ms, 1),
            "reason": self.reason,
            "errors": list(self.errors),
            "revision": self.revision,
            # Deliberately no image bytes, no path, no base64. The description
            # is the artefact.
            "retained": False,
        }


def _digest(parts: List[str]) -> str:
    import hashlib

    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Tier 1 - accessibility tree
# ---------------------------------------------------------------------------
def read_accessibility(hwnd: int, depth: int = 3) -> List[Dict[str, Any]]:
    """Real control names and roles from UI Automation.

    Returns ``[]`` when the platform has no UI Automation, the window has no
    tree, or the read fails - all of which mean "climb the ladder", not "the
    screen is empty".
    """
    if not hwnd or os.name != "nt":
        return []
    try:
        import uiautomation as auto
    except Exception as exc:  # noqa: BLE001
        log.debug("uiautomation unavailable: %s", exc)
        return []

    elements: List[Dict[str, Any]] = []
    try:
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return []
        queue = [(root, 0)]
        while queue and len(elements) < MAX_ELEMENTS:
            control, level = queue.pop(0)
            try:
                name = (control.Name or "").strip()[:NAME_LIMIT]
                role = control.ControlTypeName or ""
                value = ""
                try:
                    pattern = control.GetValuePattern()
                    value = str(pattern.Value or "")[:NAME_LIMIT]
                except Exception:  # noqa: BLE001 - most controls have no value
                    value = ""
            except Exception:  # noqa: BLE001
                continue
            if name or value:
                elements.append({
                    "role": role,
                    "name": name,
                    "value": value,
                    "depth": level,
                })
            if level < depth:
                try:
                    for child in control.GetChildren():
                        queue.append((child, level + 1))
                except Exception:  # noqa: BLE001
                    continue
    except Exception as exc:  # noqa: BLE001
        log.debug("Accessibility read failed: %s", exc)
        return []
    return elements


def find_elements(elements: List[Dict[str, Any]], needle: str) -> List[Dict[str, Any]]:
    """Case-insensitive substring search over control names and values."""
    if not needle:
        return []
    query = needle.strip().lower()
    return [e for e in elements
            if query in str(e.get("name", "")).lower()
            or query in str(e.get("value", "")).lower()]


# ---------------------------------------------------------------------------
# Tier 3 - screenshot, opt-in and transient
# ---------------------------------------------------------------------------
def capture_screenshot(hwnd: int = 0) -> Dict[str, Any]:
    """Downscaled screenshot bytes. Never written to disk, never retained.

    Requires *both* the ``screen`` permission and ``ULTRON_ALLOW_SCREENSHOT``,
    because this is the only tier that touches pixels.
    """
    permissions.require("screen", "take a screenshot")
    if not ALLOW_SCREENSHOT:
        return {"ok": False,
                "error": "screenshots are disabled. Set ULTRON_ALLOW_SCREENSHOT=1 "
                         "to enable this tier."}
    try:
        import pyautogui

        image = pyautogui.screenshot()
        # Downscale immediately: a 4K full-res frame is ~30 MB and is thrown
        # away seconds later, so keeping it large is pure waste.
        image.thumbnail((1280, 1280))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
            image.save(tmp.name, format="PNG")
            with open(tmp.name, "rb") as handle:
                data = handle.read()
        return {"ok": True, "bytes": data, "size": (image.width, image.height)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------
class VisionEngine:
    """Chooses the cheapest tier that can answer the question."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_digest = ""
        self._last: Optional[VisionResult] = None
        self.calls = 0
        self.tier_counts: Dict[str, int] = {}
        self.screenshots = 0

    def _bump(self, tier: str) -> None:
        self.calls += 1
        self.tier_counts[tier] = self.tier_counts.get(tier, 0) + 1

    def analyse(
        self,
        snapshot: Dict[str, Any],
        question: str = "",
        force: bool = False,
    ) -> VisionResult:
        """Answer ``question`` about the current context, as cheaply as possible.

        ``snapshot`` is ``ContextManager.snapshot()``.
        """
        started = time.perf_counter()
        result = VisionResult(question=question)

        try:
            permissions.require("screen", "look at the screen")
        except Exception as exc:  # noqa: BLE001
            result.tier = TIER_NONE
            result.reason = str(exc)
            with self._lock:
                self._bump(TIER_NONE)
            return result

        current = snapshot.get("current") or {}
        context = current.get("context") or {}
        hwnd = int(current.get("hwnd") or 0)
        result.revision = int(current.get("revision") or 0)

        # Tier 2: what we already know costs nothing.
        facts = [
            context.get("app_name"), context.get("item"),
            context.get("site"), context.get("url"),
            context.get("project"), context.get("folder"),
        ]
        fact_parts = [str(f) for f in facts if f]
        if fact_parts:
            result.tier = TIER_APP_FACTS
            result.summary = " · ".join(fact_parts)

        # Tier 1: the accessibility tree.
        elements = read_accessibility(hwnd)
        if elements:
            result.tier = TIER_ACCESSIBILITY
            result.elements = elements
            if question:
                hits = find_elements(elements, question)
                result.summary = (
                    f"{len(hits)} matching control(s) for {question!r}"
                    if hits else
                    f"no control matching {question!r}; {len(elements)} inspected"
                )
            else:
                roles: Dict[str, int] = {}
                for element in elements:
                    roles[element["role"]] = roles.get(element["role"], 0) + 1
                top = sorted(roles.items(), key=lambda kv: -kv[1])[:4]
                result.summary = f"{len(elements)} controls: " + ", ".join(
                    f"{role}x{count}" for role, count in top
                )

        # Did anything actually change since last time?
        result.digest = _digest([
            str(current.get("revision")),
            *fact_parts,
            *[f"{e['role']}|{e['name']}|{e['value']}" for e in elements[:MAX_ELEMENTS]],
        ])
        with self._lock:
            result.changed = force or result.digest != self._last_digest
            unchanged = not result.changed
            self._last_digest = result.digest
            self._bump(result.tier)

        if unchanged and not force:
            result.reason = "context unchanged since the last read; nothing re-analysed"
        elif question and not result.elements:
            # Only now is a screenshot defensible: the tree could not answer it.
            shot = capture_screenshot(hwnd)
            if shot.get("ok"):
                result.screenshot_taken = True
                result.tier = TIER_SCREENSHOT
                result.summary = (
                    f"accessibility tree was empty; captured {shot['size'][0]}x"
                    f"{shot['size'][1]} for {question!r}"
                )
                # The bytes die here. Nothing is kept, written or cached.
                shot["bytes"] = b""
                with self._lock:
                    self.screenshots += 1
            else:
                result.errors.append(str(shot.get("error", "")))

        result.duration_ms = (time.perf_counter() - started) * 1000
        with self._lock:
            self._last = result
        return result

    def latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._last.as_dict() if self._last else None

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "calls": self.calls,
                "by_tier": dict(self.tier_counts),
                "screenshots": self.screenshots,
                "screenshot_allowed": ALLOW_SCREENSHOT,
                "retained_images": 0,
            }

    def reset(self) -> None:
        with self._lock:
            self._last_digest = ""
            self._last = None


#: Process-wide vision engine.
vision = VisionEngine()
