"""Resolving "this", "it", "the first one", "the tab I was using".

Follow-up commands are short because the context makes them short. "Skip the
ad" only makes sense if Ultron knows the ad is on screen; "scroll down" only
means something against the page you are already looking at. So the command
needs a pointer back into the context, and this module is that pointer.

The design rule is narrow on purpose: **resolve, or ask - never guess.**

A confident wrong resolution is worse than a question, because the user has no
way to tell it happened. Clicking "the second one" on a search-results page
where the user meant a video from last week is a real action taken on a fiction.
So every resolver returns either a :class:`Resolution` with a real target, or
``resolved: False`` with a short question the user can answer in a word.

Resolvers, in priority order:

===========================  =========================================
phrase                       resolves against
===========================  =========================================
"the previous window"        the timeline's previous snapshot
"the tab i was using"        the previous snapshot when it was a browser
"this/that + file"           the open file / folder
"this/that + video/song"     the current item on a media/browser page
"the current page"           the current URL or tab title
"the first/second one"       the first/second matching UI element
"this/that/it/there"         the current focus, scoped by category
===========================  =========================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .awareness import context as context_manager

#: Ordinals users actually say.
_ORDINALS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
    "fifth": 4, "5th": 4, "five": 4,
}

#: Words that make a phrase a reference to the *previous* context.
_PREVIOUS_MARKERS = (
    "previous window", "last window", "previous tab", "last tab",
    "tab i was using", "tab i was on", "window i was using",
    "where i was", "go back to the",
)

#: Reference nouns that steer the resolution toward a specific field, and the
#: categories in which the noun is meaningful.
#:
#: The category restriction is not decoration. Without it, "play that video"
#: inside VS Code happily resolves to the open file, because ``item`` happens
#: to be set - and the assistant then tries to play a TypeScript file. A
#: reference to a video only means something in a browser or a media player.
_NOUN_HINTS: Dict[str, Any] = {
    "file": (("item", "folder", "project"), ("editor",)),
    "folder": (("folder", "project"), ("file_manager", "terminal", "editor")),
    "document": (("item",), ("document",)),
    "video": (("item", "site"), ("browser", "media")),
    "song": (("item", "site"), ("browser", "media")),
    "track": (("item", "site"), ("browser", "media")),
    "page": (("url", "item", "site"), ("browser",)),
    "site": (("site", "url"), ("browser",)),
    "website": (("site", "url"), ("browser",)),
    "window": (("item",), None),
    "tab": (("item", "site"), ("browser",)),
    "project": (("project", "folder"), ("editor", "terminal")),
}


@dataclass
class Resolution:
    """Where a reference pointed."""

    resolved: bool = False
    kind: str = ""                 # "previous" | "element" | "focus" | ...
    target: Optional[Dict[str, Any]] = None
    question: str = ""             # what to ask when not resolved
    #: Why it could not be resolved. Surfaced in the UI so an "I don't know" is
    #: explained rather than looking like the assistant ignored the sentence.
    reason: str = ""
    alternatives: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "resolved": self.resolved,
            "kind": self.kind,
            "target": self.target,
            "question": self.question,
            "reason": self.reason,
            "alternatives": self.alternatives[:5],
        }


def _has_reference(text: str) -> bool:
    lowered = f" {text.lower()} "
    words = (" this ", " that ", " it ", " there ", " these ", " those ")
    if any(word in lowered for word in words):
        return True
    if re.search(r"\b(first|second|third|fourth|fifth|1st|2nd|3rd)\b", lowered):
        return True
    return any(marker in lowered for marker in _PREVIOUS_MARKERS)


def resolve(text: str, elements: Optional[List[Dict[str, Any]]] = None) -> Resolution:
    """Resolve a reference in ``text`` against the current context.

    ``elements`` is the accessibility tree from the vision tier, used only for
    ordinal references ("the second one"). When it is absent, an ordinal
    reference resolves to a question rather than an invented target.
    """
    lowered = (text or "").lower()
    if not _has_reference(lowered):
        # Nothing to resolve - the caller should use the text as written.
        return Resolution(resolved=False, kind="none",
                          reason="no contextual reference in this phrase")

    snapshot = context_manager.snapshot()
    current = snapshot.get("current")
    if current is None:
        return Resolution(
            resolved=False, kind="unknown",
            question="What should I apply that to?",
            reason="no active window context, so 'this' has nothing to refer to",
        )
    context = current.get("context") or {}

    # 1. Explicit "previous" markers.
    for marker in _PREVIOUS_MARKERS:
        if marker in lowered:
            previous = snapshot.get("previous")
            if previous is None:
                return Resolution(
                    resolved=False, kind="previous",
                    question="There is no previous window yet - tell me which one.",
                    reason=("the user referred to the previous window but no "
                            "earlier context exists"),
                )
            prev_context = previous.get("context") or {}
            return Resolution(
                resolved=True, kind="previous",
                target={
                    "revision": previous.get("revision"),
                    "app_name": prev_context.get("app_name"),
                    "item": prev_context.get("item"),
                    "site": prev_context.get("site"),
                    "url": prev_context.get("url"),
                    "raw_title": previous.get("raw_title"),
                    "hwnd": previous.get("hwnd"),
                },
            )

    # 2. Ordinals - only answerable with a real element list.
    ordinal_match = re.search(
        r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|one|two|three)\b",
        lowered,
    )
    if ordinal_match:
        index = _ORDINALS.get(ordinal_match.group(1))
        # "the first one" on its own means "the first thing on screen".
        noun = next((n for n in _NOUN_HINTS if re.search(rf"\b{n}\b", lowered)), None)
        if elements:
            pool = [e for e in elements if e.get("name")]
            if noun:
                hints = _NOUN_HINTS[noun][0]
                narrowed = [e for e in pool
                            if any(h in str(e.get("name", "")).lower() for h in hints)]
                if narrowed:
                    pool = narrowed
            if index is not None and index < len(pool):
                return Resolution(resolved=True, kind="element", target=pool[index])
            return Resolution(
                resolved=False, kind="element",
                question=(f"I can only see {len(pool)} item(s) here. "
                          "Which one did you mean?"),
                reason="the ordinal is outside what the accessibility tree reports",
                alternatives=pool[:5],
            )
        return Resolution(
            resolved=False, kind="element",
            question="Which one? I can't see a list on this screen.",
            reason=("ordinal reference needs the accessibility tree, which was "
                    "not available for this window"),
        )

    # 3. Noun-scoped reference: "this file", "that video".
    noun = next((n for n in _NOUN_HINTS if re.search(rf"\b{n}\b", lowered)), None)
    if noun:
        fields, allowed_categories = _NOUN_HINTS[noun]
        category = context.get("category")
        if allowed_categories and category not in allowed_categories:
            # Wrong kind of application for this noun - ask rather than point
            # the action at whatever happens to be open.
            return Resolution(
                resolved=False, kind="focus_field",
                question=(f"There's no {noun} here - this is "
                          f"{context.get('app_name') or 'another application'}. "
                          f"Which {noun} do you mean?"),
                reason=(f"'{noun}' was named but the focused window is "
                        f"{context.get('app_name') or category}, where a "
                        f"{noun} is not meaningful"),
            )
        for field_name in fields:
            value = context.get(field_name)
            if value:
                return Resolution(resolved=True, kind="focus_field",
                                  target={"field": field_name, "value": value,
                                          "app_name": context.get("app_name"),
                                          "url": context.get("url"),
                                          "raw_title": current.get("raw_title"),
                                          "hwnd": current.get("hwnd")})
        return Resolution(
            resolved=False, kind="focus_field",
            question=(f"I don't have a {noun} open right now "
                      f"(this is {context.get('app_name') or 'an unknown window'}). "
                      "Which one?"),
            reason=f"'{noun}' was named but the context has no {noun}",
        )

    # 4. Bare "this/that/it/there" - the current focus, whatever it is.
    if context.get("url") or context.get("item") or context.get("app_name"):
        return Resolution(resolved=True, kind="focus", target={
            "app_name": context.get("app_name"),
            "item": context.get("item"),
            "site": context.get("site"),
            "url": context.get("url"),
            "category": context.get("category"),
            "task": current.get("task"),
            "last_action": current.get("last_action"),
            "raw_title": current.get("raw_title"),
            "hwnd": current.get("hwnd"),
        })

    return Resolution(
        resolved=False, kind="focus",
        question="What should I apply that to?",
        reason="the active window has no usable title, so there is nothing to "
               "anchor the reference to",
    )


def is_reference_only(text: str) -> bool:
    """True when a follow-up makes no sense without the context.

    Used to decide whether a short utterance should be handed to the resolver
    or treated as a fresh, self-contained goal.
    """
    words = re.findall(r"[\w']+", (text or "").lower())
    return len(words) <= 6 and _has_reference(text)
