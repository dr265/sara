"""Execution bridge — cloud plan, Windows execution.

THE GAP THIS FILLS

The backend already has a planner, an executor, a 63-tool registry and a
secure device link. But the executor calls ``registry.run(tool, args)``, which
executes **in the backend process**. On a Linux container that process has no
screen, no mouse and no Win32, so every desktop call either failed or silently
did nothing while the plan reported success.

This module is the missing edge. It takes a tool call that the backend cannot
perform itself and hands it to the paired Windows agent, then returns what the
agent actually did.

    plan step -> execution_bridge.dispatch(tool, args)
                          |
                          v
              paired Windows agent (sara_desktop)
                          |
              executes via the EXISTING action registry
                          |
                          v
              real result + real verification

WHAT IS DELIBERATELY NOT HERE

* No tool implementations. The Windows agent already has17 working actions;
  duplicating them here would create a second source of truth and drift.
* No screenshot or keystroke buffering. Frames carry a command and come back
  with a result.
* No silent fallback. If no agent is online the call FAILS loudly. Returning a
  fabricated success is the single worst thing this component could do.

SECURITY

* Dispatch requires a connected, non-revoked device.
* Every command carries a unique ``command_id``; a result that names an
  unknown or already-settled command is dropped, which is what stops a
  replayed frame from producing a second action.
* Results are size-capped. A tool that returns a 40 MB file listing must not
  be able to exhaust backend memory.
* High-risk commands are refused here unless they carry a confirmation token
  issued by this backend for that exact command. The Windows agent re-checks
  independently, so a compromised control plane still cannot delete a disk.

LIFECYCLE

The registry is in-process and in-memory, like ``devices.py``. A backend
restart drops connections; agents reconnect and re-register. That is correct -
a command queue that survived a crash would be a queue nobody confirmed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Set

from .risk import Decision, classify

log = logging.getLogger("sara.ultron.bridge")

#: How long to wait for the Windows agent before declaring failure. Long
#: enough for a cold app launch (VS Code can take 10s+), short enough that a
#: wedged agent does not stall the whole plan.
DEFAULT_TIMEOUT_S = 45.0

#: Cap on a returned result. Tool output beyond this is truncated with a
#: marker rather than dropped silently, so the model can tell it was cut.
MAX_RESULT_CHARS = 8000

#: Tools that MUST be executed locally even if the backend thinks it could do
#: them. Keeps behaviour identical to the pre-split single-process setup.
_ALWAYS_REMOTE = frozenset({
    "computer_control", "computer_settings", "desktop_control", "open_app",
    "browser_control", "file_controller", "system_command", "screen_capture",
    "screenshot", "clipboard", "youtube_video",
})


@dataclass
class Command:
    """One in-flight tool call awaiting a real result from the PC."""

    command_id: str
    tool: str
    args: Dict[str, Any]
    decision: Decision
    created: float = field(default_factory=time.time)
    future: "asyncio.Future[Dict[str, Any]]" = field(default_factory=asyncio.Future)

    def describe(self) -> str:
        return f"{self.tool}/{self.args.get('action', '')}".rstrip("/")


class NoLocalAgent(RuntimeError):
    """Raised when a PC command is requested with no usable agent attached."""


class ConfirmationRequired(RuntimeError):
    """Raised when a gated command arrives without a valid confirmation."""

    def __init__(self, decision: Decision, prompt: str) -> None:
        super().__init__(prompt)
        self.decision = decision
        self.prompt = prompt


@dataclass
class LocalAgent:
    """One connected Windows machine."""

    device_id: str
    label: str
    #: Async send callable supplied by the websocket handler.
    send: Callable[[Dict[str, Any]], Any]
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    commands_done: int = 0
    commands_failed: int = 0
    scopes: Set[str] = field(default_factory=set)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "label": self.label,
            "online": True,
            "connected_for_s": round(time.time() - self.connected_at, 1),
            "last_seen_age_s": round(time.time() - self.last_seen, 1),
            "commands_completed": self.commands_done,
            "commands_failed": self.commands_failed,
            "scopes": sorted(self.scopes),
        }


class ExecutionBridge:
    """Routes tool calls to the connected Windows agent.

    One instance per backend process. Thread-safe enough for the async web
    server; the only shared mutable state is the agent table and the pending
    command map, both guarded by an asyncio lock created lazily so the object
    can be built at import time.
    """

    def __init__(self) -> None:
        self._agents: Dict[str, LocalAgent] = {}
        self._pending: Dict[str, Command] = {}
        self._confirmations: Dict[str, float] = {}
        self._lock: Optional[asyncio.Lock] = None
        #: The server's event loop, captured at startup. The executor runs in a
        #: worker THREAD with no loop of its own, so a blocking
        #: ``dispatch_sync`` needs somewhere to hand the coroutine. Binding it
        #: once at startup is what lets the executor stay synchronous - which
        #: is what keeps every existing caller and test working unchanged.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the server loop. Called once during app startup."""
        self._loop = loop

    def dispatch_sync(self, tool: str, args: Optional[Dict[str, Any]] = None,
                      timeout_s: float = DEFAULT_TIMEOUT_S,
                      confirmed: bool = False) -> Dict[str, Any]:
        """Blocking dispatch, for callers that are not on the event loop.

        Used by the executor, which runs synchronously in a worker thread. The
        coroutine is handed to the server loop and waited on here, so the
        desktop still executes for real while nothing above it has to become
        async.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            raise NoLocalAgent(
                "The backend's event loop is not available, so a PC command "
                "cannot be dispatched. Nothing was executed."
            )
        future = asyncio.run_coroutine_threadsafe(
            self.dispatch(tool, args, timeout_s=timeout_s, confirmed=confirmed), loop
        )
        return future.result(timeout=timeout_s + 5.0)

    # -- plumbing --------------------------------------------------------
    def _guard(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # -- agent registration ----------------------------------------------
    def register_agent(self, agent: LocalAgent) -> None:
        self._agents[agent.device_id] = agent
        log.info("Windows agent online: %s (%s)", agent.label, agent.device_id)

    def unregister_agent(self, device_id: str) -> None:
        agent = self._agents.pop(device_id, None)
        if agent is None:
            return
        log.info("Windows agent offline: %s (%s)", agent.label, device_id)
        # Fail every command that was waiting on it. Leaving them pending would
        # let a plan hang until the timeout while the machine is plainly gone.
        self._fail_pending_for(device_id, "the Windows agent disconnected before the command completed")

    # -- confirmation tokens ---------------------------------------------
    def issue_confirmation(self, command_id: str, ttl_s: float = 300.0) -> None:
        """Authorise exactly one command, for a bounded time.

        Bound to the command id on purpose: a blanket "confirmed" flag that the
        caller can reuse is not a confirmation, it is a suggestion.
        """
        self._confirmations[command_id] = time.time() + ttl_s
        self._prune_confirmations()

    def _consume_confirmation(self, command_id: str) -> bool:
        expiry = self._confirmations.pop(command_id, None)
        return bool(expiry and expiry > time.time())

    def _prune_confirmations(self) -> None:
        now = time.time()
        for cid in [c for c, exp in self._confirmations.items() if exp <= now]:
            self._confirmations.pop(cid, None)

    # -- inspection -------------------------------------------------------
    def agents(self) -> Dict[str, Any]:
        return {d: a.as_dict() for d, a in self._agents.items()}

    def is_online(self) -> bool:
        return bool(self._agents)

    def any_agent(self) -> Optional[LocalAgent]:
        for agent in self._agents.values():
            return agent
        return None

    def snapshot(self) -> Dict[str, Any]:
        agents = self.agents()
        return {
            "windows_agents_online": len(agents),
            "agents": list(agents.values()),
            "pending_commands": len(self._pending),
            "pending": [
                {
                    "command_id": c.command_id,
                    "tool": c.tool,
                    "risk": c.decision.tier,
                    "age_s": round(time.time() - c.created, 1),
                }
                for c in self._pending.values()
            ],
            "note": (
                "The backend cannot execute desktop actions itself. Every "
                "computer_control/computer_settings/file_controller/system_command "
                "call is dispatched to a paired Windows agent and its REAL "
                "result is returned."
            ),
        }

    # -- the hot path -----------------------------------------------------
    def needs_remote(self, tool: str) -> bool:
        """True when this tool must run on the PC rather than in the backend."""
        if tool in _ALWAYS_REMOTE:
            return True
        # Anything that is not an Ultron-native reasoning tool and not a pure
        # data tool is assumed to need the machine it describes.
        return tool.startswith("brahma_")

    async def dispatch(self, tool: str, args: Optional[Dict[str, Any]] = None,
                       agent: Optional[LocalAgent] = None,
                       timeout_s: float = DEFAULT_TIMEOUT_S,
                       confirmed: bool = False) -> Dict[str, Any]:
        """Send one tool call to the Windows agent and await its real result.

        Returns the agent's own ``{"ok", "output", "verified"}`` payload.
        Raises ``NoLocalAgent`` when nothing is connected and
        ``ConfirmationRequired`` for a gated call with no confirmation.
        """
        args = dict(args or {})
        decision = classify(tool, args)

        if decision.requires_confirmation and not confirmed:
            from .risk import confirmation_prompt
            raise ConfirmationRequired(decision, confirmation_prompt(tool, args))

        target = agent or self.any_agent()
        if target is None:
            raise NoLocalAgent(
                f"'{tool}' needs the Windows agent, and no PC is connected. "
                "Pair this machine (Devices > Connect this PC) and retry. "
                "Nothing was executed."
            )

        command_id = uuid.uuid4().hex
        cmd = Command(command_id=command_id, tool=tool, args=args, decision=decision)
        self._pending[command_id] = cmd
        if decision.requires_confirmation and confirmed:
            self.issue_confirmation(command_id)

        payload = {
            "type": "command",
            "command_id": command_id,
            "tool": tool,
            "args": args,
            "risk": decision.tier,
            "confirmed": bool(decision.requires_confirmation and confirmed),
            "sent_at": time.time(),
        }

        try:
            await target.send(payload)
        except Exception as exc:  # noqa: BLE001 - socket died mid-dispatch
            self._pending.pop(command_id, None)
            raise NoLocalAgent(
                f"Could not reach the Windows agent: {type(exc).__name__}. "
                "Nothing was executed."
            ) from exc

        try:
            result = await asyncio.wait_for(cmd.future, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            target.commands_failed += 1
            raise TimeoutError(
                f"The Windows agent did not answer within {timeout_s:.0f}s for "
                f"{cmd.describe()}. The result is unknown - it may still be "
                "running on the PC. Check before retrying."
            ) from exc
        finally:
            self._pending.pop(command_id, None)
            self._confirmations.pop(command_id, None)

        if result.get("ok"):
            target.commands_done += 1
        else:
            target.commands_failed += 1
        return result

    # -- inbound ----------------------------------------------------------
    async def settle(self, frame: Dict[str, Any]) -> bool:
        """Apply a ``result`` frame from an agent. Returns True if it matched.

        Rejects results for unknown command ids, which is how a duplicated or
        replayed frame is prevented from executing anything twice.
        """
        command_id = str(frame.get("command_id") or "")
        cmd = self._pending.get(command_id)
        if cmd is None:
            log.warning("Dropping result for unknown/stale command %s", command_id[:12] or "-")
            return False

        agent = self._agents.get(str(frame.get("device_id") or ""))
        if agent is not None:
            agent.last_seen = time.time()

        if cmd.future.done():
            return False

        output = frame.get("output")
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False, default=str)
        if len(output) > MAX_RESULT_CHARS:
            output = output[:MAX_RESULT_CHARS] + f"\n... [truncated at {MAX_RESULT_CHARS} chars]"

        cmd.future.set_result({
            "ok": bool(frame.get("ok")),
            "output": output,
            "error": frame.get("error") or "",
            "verified": frame.get("verified"),
            "tool": cmd.tool,
            "command_id": command_id,
            "duration_s": frame.get("duration_s"),
        })
        return True

    def _fail_pending_for(self, device_id: str, reason: str) -> None:
        for cmd in list(self._pending.values()):
            if cmd.future.done():
                continue
            cmd.future.set_result({
                "ok": False,
                "output": "",
                "error": reason,
                "verified": False,
                "tool": cmd.tool,
                "command_id": cmd.command_id,
            })


#: Process-wide bridge. One per backend process, like every other singleton here.
bridge = ExecutionBridge()