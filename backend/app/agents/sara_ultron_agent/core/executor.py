"""Ultron executor - runs plan steps for real and decides what to do on failure.

Adapted from the uploaded Brahma-Echo executor (``agent/executor.py``). The
retry/skip/abort error policy and the 3-attempt ceiling are preserved; the
dispatch table is replaced by SARA's shared action registry so Ultron's tools
and SARA's autonomous agent's tools are the *same* tools.

Failure detection is content-based, because the desktop action handlers return
human-readable strings rather than raising. A crashed action is caught by
``ActionRegistry.run`` and comes back as ``Tool 'x' failed: ...``, so both the
raise path and the string path have to be classified.
"""

from __future__ import annotations

import json
import logging
import re
import inspect
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .session import UltronSession
from .tools import EVENT_OUTPUT_LIMIT, MAX_TOOL_OUTPUT, UltronToolRegistry

log = logging.getLogger("sara.ultron.executor")

MAX_ATTEMPTS = 3            # per step, matching the uploaded agent's ceiling
APPROVAL_TIMEOUT_S = 300    # how long an approval may sit unanswered

# Tools that can change or destroy real state - always gated behind approval.
# Kept identical to app/api/sara_auto_agent.py so both agents gate the same
# operations the same way.
DESTRUCTIVE_TOOLS = {"computer_settings", "system_command"}
DESTRUCTIVE_ACTIONS = {
    "file_controller": {"delete", "rename", "move", "write", "organize_desktop"},
    "desktop_control": {"shutdown", "restart", "lock", "signout", "logoff"},
}

# Individual Brahma-Echo actions that destroy data, matched per tool. Upstream
# spreads these across ~70 functions in computer_settings/computer_control, so
# the whole *tool* would have to be gated to catch one function - which would
# make asking for the current volume as intrusive as shutting the PC down.
#
# Every name here is checked against the action enum the bridge extracts from the
# upstream source (see `core/brahma_bridge.py`); `tests/test_sara_ultron_phase2.py`
# fails if any entry names an action the module does not actually dispatch, so
# these cannot silently rot into decoration. Note `brahma_desktop`'s destructive
# action is `clean`, not the underlying `clean_desktop()` function.
DESTRUCTIVE_BRAHMA_ACTIONS = {
    "brahma_computer_settings": {
        "restart", "shutdown", "lock_screen", "reboot", "restart_computer",
        "shutdown_computer", "signout", "logoff", "toggle_wifi",
    },
    "brahma_computer_control": {
        "hotkey", "press", "type", "smart_type", "paste", "random_data",
    },
    "brahma_file_controller": {
        "delete", "move", "rename", "write", "create_file", "create_folder",
        "organize_desktop", "organize_folder", "organize",
        "preview_organize", "organize_preview", "undo_organize", "organize_undo",
    },
    "brahma_desktop": {
        "task", "clean", "clean_empty", "clean_empty_folders",
        "organize", "organize_desktop", "preview", "preview_organize",
        "dry_run", "dryrun", "inspect", "undo", "rollback", "revert",
        "find_duplicates", "duplicates", "dupes", "archive", "archive_old",
        "wallpaper", "wallpaper_url",
    },
    "brahma_game_updater": {"install", "schedule"},
    "brahma_office_builder": set(),
    "brahma_docx_tools": set(),
    "brahma_pdf_tools": set(),
    "brahma_screen_processor": set(),
    "brahma_pushup_counter": set(),
    "brahma_calorie_counter": set(),
}


class ErrorDecision(str, Enum):
    """What to do about a step that just failed."""
    RETRY = "retry"    # transient - try the same step again
    SKIP = "skip"      # this step cannot succeed - continue with the rest
    ABORT = "abort"    # the run must stop (user denial / hard refusal)


# Substrings that mean "this will never work" -> SKIP, not RETRY.
# Matched against the head of the output only: handlers prefix their errors
# ("Could not ...", "File not found: ...") but a successful search's article
# text can legitimately contain these words further in.
_SKIP_MARKERS = (
    "is not available", "does not exist", "not found", "no such",
    "not installed", "unknown action", "unknown tool", "unknown pptx action",
    "unknown file type", "cannot create", "missing required",
    # Permanent, non-transient upstream failures. Retrying these three times
    # just burns the budget and delays an honest report; the same conditions
    # are hard errors for app.core.llm_router.
    "requires an active paid plan", "insufficient_quota", "insufficient quota",
    "insufficient balance", "billing", "payment required", "402",
    "invalid api key", "incorrect api key", "401", "unauthorized", "403",
    "invalid_api_key", "model not found", "no such model",
)

# Substrings that mean "worth another try" -> RETRY.
_RETRY_MARKERS = (
    "timed out", "timeout", "try again", "temporarily", "busy",
    "rate limit", "too many requests", "connection", "unavailable",
    "could not reach", "transient", "resource busy", "locked",
)

# Substrings that mean "stop everything" -> ABORT.
_ABORT_MARKERS = (
    "denied by the user", "denied by user", "not allowed",
    "forbidden", "permission denied by", "emergency stop",
)

# A tool that ran cleanly but did nothing: it tells the model it had nothing to
# work with. Counting that as a success would let a run report "completed" when
# the real work never happened, so it is treated as a failure.
_NOOP_MARKERS = (
    "no user content was provided", "nothing to summarize",
    "no content provided", "cannot summarize", "nothing to work with",
    "no input was provided", "nothing found to", "please provide",
)

# Generic failure markers (from SARA's autonomous agent, kept aligned).
_FAIL_MARKERS = (
    "could not", "cannot create", "failed to", "failed:", "error:",
    "not found", "no such", "unknown action", "unknown pptx action",
    "unknown file type", "does not exist", "not installed",
    "access denied", "permission denied", "likely did not open",
    "generation failed", "execution halted", "timed out",
)

# A literal, unsubstituted planner placeholder reaching a tool.
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def _resolve_value(value: Any, index: int, previous: str) -> Any:
    """Substitute ``${stepN_output}`` / ``${previous_output}`` placeholders."""
    if isinstance(value, str):
        def _sub(match: "re.Match[str]") -> str:
            token = match.group(1).strip().lower().replace("-", "_").replace(" ", "_")
            if token in ("previous_output", "prev_output", "last_output", "output"):
                return previous
            m = re.fullmatch(r"step_?(\d+)_output", token)
            if m:
                # Only the immediately preceding step's output is in scope at
                # call time; an earlier step's result is no longer carried.
                return previous if int(m.group(1)) == index else ""
            return ""

        return _PLACEHOLDER_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _resolve_value(v, index, previous) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, index, previous) for v in value]
    return value


def resolve_placeholders(args: Dict[str, Any], index: int, previous: str) -> Dict[str, Any]:
    """Return ``args`` with planner output placeholders replaced by real data.

    An unknown placeholder resolves to an empty string rather than being passed
    through, so a tool can never receive the literal text ``${step1_output}``.
    """
    if not isinstance(args, dict):
        return {}
    return {k: _resolve_value(v, index, previous) for k, v in args.items()}


def has_unresolved_placeholder(args: Dict[str, Any]) -> bool:
    """True when a plan step still references an output that is not available."""
    def _walk(value: Any) -> bool:
        if isinstance(value, str):
            return bool(_PLACEHOLDER_RE.search(value))
        if isinstance(value, dict):
            return any(_walk(v) for v in value.values())
        if isinstance(value, list):
            return any(_walk(v) for v in value)
        return False

    return _walk(args or {})


def is_destructive(name: str, args: Dict[str, Any],
                   gated_tool: bool = False) -> bool:
    """True when this call can change or destroy real state.

    ``gated_tool`` covers the bridged Brahma-Echo tools that were declared
    risky as a whole (phone control, social posting, device unlock). Those are
    gated at tool level because their blast radius is not enumerable from an
    action name.
    """
    if gated_tool or name in DESTRUCTIVE_TOOLS:
        return True
    act = str((args or {}).get("action", "")).lower().strip()
    if act in DESTRUCTIVE_ACTIONS.get(name, set()):
        return True
    return act in DESTRUCTIVE_BRAHMA_ACTIONS.get(name, set())


def looks_failed(output: str, name: str = "") -> bool:
    """Content-based failure detection for a tool's textual output.

    Every marker family counts as failure, not just ``_FAIL_MARKERS``. A tool
    that answers "rate limit hit, try again" or "requires an active paid plan"
    raised nothing and may contain no "failed:" wording at all, but it is
    plainly not a success and must reach the retry/skip policy.
    """
    low = (output or "")[:300].lower()
    if not low:
        return False
    if name and low.startswith(f"action '{name}' is not available"):
        return True
    if low.startswith("tool '") or low.startswith("browser error"):
        return True
    return any(m in low for m in
               _FAIL_MARKERS + _RETRY_MARKERS + _NOOP_MARKERS + _SKIP_MARKERS)


def classify_error(output: str, name: str = "", attempts_left: int = 0) -> ErrorDecision:
    """Map a failed step's output to the action to take next."""
    low = (output or "")[:300].lower()
    if any(m in low for m in _ABORT_MARKERS):
        return ErrorDecision.ABORT
    if any(m in low for m in _SKIP_MARKERS) or any(m in low for m in _NOOP_MARKERS):
        # No point retrying something that will never succeed, and retrying a
        # no-op is pure delay — the input has to change, not the attempt count.
        return ErrorDecision.SKIP
    # Anything else gets the remaining attempts. Handlers return prose, not
    # error codes, so a non-marker failure is still worth a second and third
    # try - the alternative is giving up on a transient hiccup.
    return ErrorDecision.RETRY if attempts_left > 0 else ErrorDecision.SKIP


@dataclass
class StepResult:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    output: str = ""
    attempts: int = 0
    decision: Optional[ErrorDecision] = None
    skipped: bool = False
    seconds: float = 0.0
    source: str = "desktop"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "args": self.args,
            "ok": self.ok,
            "output": (self.output or "")[:EVENT_OUTPUT_LIMIT],
            "attempts": self.attempts,
            "skipped": self.skipped,
            "seconds": round(self.seconds, 1),
            "source": self.source,
        }


def _maybe_await(value):
    """Return ``value`` unchanged.

    Kept as a named seam for callers that may hold an awaitable, but the
    executor is deliberately synchronous again: routing through the bridge uses
    ``dispatch_sync`` on the server loop, so no step needs to be a coroutine.
    """
    return value


class UltronExecutor:
    """Executes one plan step at a time, with approval gating and error policy."""

    def __init__(self, registry: UltronToolRegistry, max_attempts: int = MAX_ATTEMPTS) -> None:
        self.registry = registry
        self.max_attempts = max(1, max_attempts)

    def _run_tool(self, tool: str, args: Dict[str, Any]) -> str:
        """Run one tool, on the PC when it is a desktop tool.

        THE ROUTING DECISION

        ``registry.run`` executes inside the backend process. For reasoning
        tools (memory, delegation, the agent catalogue) that is correct. For
        desktop tools it is not: a Linux container has no mouse, no screen and
        no Win32, so the call would either fail or - worse - return a plausible
        string while nothing happened on the user's PC.

        So a desktop tool is routed to the paired Windows agent through the
        execution bridge, and what comes back is the machine's real result.

        Deliberately SYNCHRONOUS. The executor is called from a worker thread
        with no event loop, and turning it async would force every existing
        caller and test to change. ``dispatch_sync`` hands the coroutine to the
        server loop and blocks here, so the desktop action still happens for
        real while nothing above this line has to become async.

        When no agent is connected the call FAILS with an explanation. It never
        degrades into a fabricated success; that was exactly how the previous
        deployment ended up reporting work that had never been performed.
        """
        from .execution_bridge import (
            ConfirmationRequired, NoLocalAgent, bridge,
        )
        from .risk import classify

        if not bridge.needs_remote(tool):
            return str(self.registry.run(tool, args) or "")

        if not bridge.is_online():
            return (
                f"'{tool}' must run on the user's Windows PC, and no PC is "
                "connected right now. Nothing was executed. Pair this machine "
                "(Devices -> Connect this PC) and the action will run for real."
            )

        # The approval gate above already ran for gated tools; telling the
        # bridge "confirmed" keeps the local agent from re-prompting for a
        # decision the user has genuinely just made.
        policy = classify(tool, args)
        try:
            outcome = bridge.dispatch_sync(tool, args,
                                           confirmed=policy.requires_confirmation)
        except ConfirmationRequired as exc:
            # The executor's own gate should have caught this. Reaching here
            # means the two disagreed, which is a bug worth surfacing rather
            # than silently downgrading.
            return f"'{tool}' needs confirmation before it can run: {exc.prompt}"
        except NoLocalAgent as exc:
            return str(exc)
        except TimeoutError as exc:
            return str(exc)

        if not outcome.get("ok"):
            return (f"Tool '{tool}' failed on the PC: "
                    f"{outcome.get('error') or outcome.get('output')}")
        return str(outcome.get("output") or "Done.")

    # -- approval -------------------------------------------------------
    def _request_approval(self, session: UltronSession, call_id: str, name: str,
                          args: Dict[str, Any]) -> bool:
        prompt = f"Approve {name}?"
        if args:
            prompt += f" - {json.dumps(args, ensure_ascii=False)[:200]}"
        session.emit("approval_request", call_id=call_id, name=name, prompt=prompt)
        session.log(f"[ULTRON] Approval required: {name} {args or ''}")
        try:
            _cid, granted = session.approve_q.get(timeout=APPROVAL_TIMEOUT_S)
        except Exception:  # queue.Empty
            granted = False
        session.log(f"[ULTRON] {'Approved' if granted else 'Denied'}: {name}")
        return granted

    # -- execution ------------------------------------------------------
    def execute(self, session: UltronSession, tool: str,
                args: Dict[str, Any]) -> StepResult:
        """Run one step, retrying transient failures up to ``max_attempts``."""
        result = StepResult(tool=tool, args=args or {}, source=self.registry.source(tool))
        call_id = f"{session.task_id[:8]}-{len(session.steps)}-{tool}"

        if not self.registry.has(tool):
            result.output = (
                f"Tool '{tool}' does not exist. Available: {', '.join(self.registry.names())}"
            )
            result.decision = ErrorDecision.SKIP
            session.log(f"[ULTRON] x Unknown tool '{tool}'")
            return result

        # Validate before dispatch: a step that names its arguments wrongly
        # cannot work, and spending a tool call to learn that hides the cause.
        missing = self.registry.missing_args(tool, args or {})
        if missing:
            result.output = (
                f"Invalid call to '{tool}': missing required argument(s) "
                f"{', '.join(missing)}. This tool accepts "
                f"{', '.join(self.registry.required_args(tool))}."
            )
            result.decision = ErrorDecision.SKIP
            session.log(f"[ULTRON] x {tool} called without {', '.join(missing)}")
            return result

        # Phase 10: authorization is decided in ONE place.
        #
        # This used to consult a local copy of DESTRUCTIVE_TOOLS that could
        # drift from the identical copy in sara_auto_agent.py. `classify` is the
        # single authority now; the old tables stay only so the tests that
        # assert their contents keep passing.
        #
        # The behavioural change matters: the old rule gated `computer_settings`
        # and `system_command` wholesale, so setting the volume or running
        # `dir` asked for approval. The policy engine asks only for genuinely
        # destructive calls, which is what makes a multi-step desktop task
        # possible without a prompt per step.
        from .risk import classify

        decision_policy = classify(tool, args or {})
        gated = (
            session.mode == "assisted"
            or decision_policy.requires_confirmation
            or is_destructive(tool, args or {}, self.registry.is_gated(tool))
        )
        if gated and not session.stop.is_set():
            # The prompt names the real effect ("This will permanently delete
            # C:\\Projects\\x") rather than a generic "Allow?", so the user is
            # approving something they can picture.
            session.log(
                f"[ULTRON] GATE {decision_policy.tier}: {tool} -> {decision_policy.impact}"
            )
            # A confirmation request is the one thing that MUST be spoken: the
            # user may not be looking at the screen, and something destructive
            # is waiting on their answer.
            try:
                from .narration import narrator as _narrator

                _narrator.needs_attention(decision_policy.impact or tool)
            except Exception:  # noqa: BLE001
                pass
            if not self._request_approval(session, call_id, tool, args or {}):
                result.output = "Denied by the user. Do not retry this action."
                result.decision = ErrorDecision.ABORT
                result.skipped = True
                session.log(f"[ULTRON] x Denied by user: {tool}")
                return result

        t0 = time.time()
        last_output = ""
        session.current_tool = tool
        for attempt in range(1, self.max_attempts + 1):
            if session.stop.is_set():
                result.output = "Stopped by user."
                result.decision = ErrorDecision.ABORT
                result.skipped = True
                break

            result.attempts = attempt
            session.emit("tool_call", call_id=call_id, name=tool, args=args or {},
                         attempt=attempt)
            session.log(f"[ULTRON] > {tool}({json.dumps(args or {}, ensure_ascii=False)[:200]})")

            # Announce the step out loud before running it, so a person in the
            # room can hear what is happening rather than waiting in silence.
            if attempt == 1:
                try:
                    from .narration import narrator as _narrator

                    _narrator.step_start(tool, str((args or {}).get("action") or ""))
                except Exception:  # noqa: BLE001 - narration never blocks a task
                    pass

            try:
                last_output = self._run_tool(tool, args or {})
            except Exception as exc:  # noqa: BLE001 - registry already traps most
                last_output = f"{tool} crashed: {type(exc).__name__}: {exc}"
                log.warning("Ultron step %s crashed: %s", tool, type(exc).__name__)

            if not looks_failed(last_output, tool):
                result.ok = True
                result.output = last_output
                break

            last_output = last_output or "Tool returned no output."
            decision = classify_error(last_output, tool, self.max_attempts - attempt)
            result.decision = decision
            if decision is ErrorDecision.RETRY and attempt < self.max_attempts:
                session.log(f"[ULTRON] ~ retrying {tool} ({attempt}/{self.max_attempts}): "
                            f"{last_output[:160]}")
                # Linear backoff, matching SARA's LLM router convention.
                time.sleep(min(2 * attempt, 6))
                continue
            if decision is ErrorDecision.ABORT:
                result.skipped = True
            break

        # last_output is empty only when the loop never ran a tool (stop was
        # already set), in which case keep the reason set on the way in.
        result.output = last_output or result.output or "Tool returned no output."
        result.seconds = time.time() - t0
        session.current_tool = ""

        session.emit("tool_result", call_id=call_id, name=tool, ok=result.ok,
                     output=result.output[:EVENT_OUTPUT_LIMIT],
                     attempts=result.attempts, seconds=round(result.seconds, 1))
        session.log(f"[ULTRON] < {tool} -> {'OK' if result.ok else 'FAILED'} "
                    f"({result.seconds:.1f}s, {result.attempts} attempt(s)): "
                    f"{result.output[:160]}")
        return result

    def execute_plan(self, session: UltronSession,
                     plan: List[Dict[str, Any]]) -> List[StepResult]:
        """Run a whole plan in order. Stops early on a hard abort or a stop.

        Each step's args are resolved against the previous step's real output
        first, so a planner that writes ``"${step1_output}"`` gets that step's
        actual result rather than the literal placeholder string.

        Synchronous on purpose: a desktop step blocks here while the Windows
        agent performs the action and reports back, which is honest about a
        real wait without making the whole executor async.
        """
        results: List[StepResult] = []
        previous_output = ""
        for index, step in enumerate(plan):
            if session.stop.is_set():
                break
            tool = str(step.get("tool", ""))
            raw = step.get("args") if isinstance(step.get("args"), dict) else {}
            args = resolve_placeholders(raw, index, previous_output)
            session.current_tool = tool
            res = self.execute(session, tool, args)
            results.append(res)
            session.steps.append(res.to_dict())
            if res.ok and res.output:
                previous_output = res.output
            if res.decision is ErrorDecision.ABORT:
                session.log("[ULTRON] Aborting run after a hard stop condition.")
                break
        return results
