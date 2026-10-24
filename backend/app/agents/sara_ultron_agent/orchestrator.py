"""Ultron run loop: plan -> execute -> replan -> report -> remember.

This is the part adapted from the uploaded Brahma-Echo agent that SARA did not
already have: a *supervisory* loop that plans up front, executes each step for
real, and re-plans around genuine failures instead of hammering the same call.
SARA's ``sara_auto_agent`` is a flat tool-calling loop; Ultron sits above that
level of behaviour while sharing the same tools, LLM router and memory store.

Every status the run reports is derived from what actually executed - the same
honesty rule SARA's autonomous agent follows. Nothing here can claim a step
succeeded unless the tool returned a success.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from app.core.llm_router import llm_chat_sync

from .core.catalog import AGENT_METADATA
from .core.executor import ErrorDecision, StepResult, UltronExecutor
from .core.memory import UltronMemory
from .core.planner import MAX_REPLAN_ATTEMPTS, UltronPlanner
from .core.session import TERMINAL_PHASES, UltronSession
from .core.tools import UltronToolRegistry, get_memory, get_registry

log = logging.getLogger("sara.ultron.orchestrator")

REPORT_TIMEOUT_S = 90.0

_REPORT_PROMPT = """You are SARA Ultron, reporting on a run you just performed \
inside the SARA AI Nexus Hub.

Every step below actually executed. Report ONLY what these results show.
Never invent an outcome, never imply a step succeeded when it did not.

GOAL
{goal}

PLAN THAT WAS RUN
{plan}

STEP RESULTS
{results}

MEMORY RECALLED BEFORE THE RUN
{memory}

Write a plain-text report (no markdown headers) that states:
1. what was achieved, 2. anything that failed or was skipped and why,
3. what the user should do next.
If nothing succeeded, say so plainly. Address the user politely."""


class UltronOrchestrator:
    """Drives one Ultron run end to end."""

    def __init__(self, registry: UltronToolRegistry, memory: UltronMemory) -> None:
        self.registry = registry
        self.memory = memory
        self.planner = UltronPlanner()
        self.executor = UltronExecutor(registry)

    # -- reporting ------------------------------------------------------
    def _fact_report(self, session: UltronSession,
                     results: List[StepResult]) -> str:
        """Deterministic, factual rendering of a run. Never invents anything.

        Used when the LLM report fails, and as the floor whenever the model's
        reply is too thin to be a report.
        """
        ok = sum(1 for r in results if r.ok)
        bad = [r for r in results if not r.ok]
        lines = [f"Goal: {session.goal}", f"Steps: {ok}/{len(results)} succeeded."]
        for r in results:
            lines.append(f"{'OK' if r.ok else 'FAILED'} {r.tool}: {r.output[:300]}")
        if not bad and results:
            lines.append("All planned steps completed.")
        return "\n".join(lines)

    def _report(self, session: UltronSession, results: List[StepResult]) -> str:
        rendered = "\n".join(
            f"- {r.tool} ({r.source}): {'OK' if r.ok else 'FAILED/SKIPPED'} "
            f"after {r.attempts} attempt(s) - {r.output[:400]}"
            for r in results
        ) or "(no steps were executed)"
        try:
            report = llm_chat_sync(
                [{"role": "system", "content": _REPORT_PROMPT.format(
                    goal=session.goal,
                    plan=json_lines(session.plan),
                    results=rendered,
                    memory=session.memory_snapshot or "No prior memory for this task.",
                )},
                 {"role": "user", "content": "Write the final report now."}],
                max_tokens=800,
                temperature=0.3,
                timeout=REPORT_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - never fail a finished run on its report
            log.warning("Ultron report generation failed: %s", type(exc).__name__)
            return self._fact_report(session, results)

        # Quality gate. Several cheap gateways (and "auto" routers in particular)
        # answer with a stub like "Report" or a restatement of the instruction.
        # Shipping that to the user makes a successful run look broken, so a
        # reply that carries no substance falls back to the factual rendering.
        text = (report or "").strip()
        if len(text) < 40:
            log.info("Ultron report too thin (%d chars) - using factual report",
                     len(text))
            return self._fact_report(session, results)
        return text

    # -- main loop ------------------------------------------------------
    def run(self, session: UltronSession) -> None:
        session.status = "running"
        results: List[StepResult] = []
        try:
            native = sorted(n for n in self.registry.names() if self.registry.source(n) == "ultron")
            desktop = sorted(n for n in self.registry.names() if self.registry.source(n) == "desktop")
            session.emit("meta", agent=AGENT_METADATA["name"],
                         version=AGENT_METADATA["version"],
                         tools=sorted(self.registry.names()),
                         native_tools=native, desktop_tools=desktop)
            session.log(f"[ULTRON] Goal: {session.goal}")
            session.log(f"[ULTRON] Engine online: {len(desktop)} desktop actions, "
                        f"{len(native)} native tools")

            session.set_phase("understanding")
            session.memory_snapshot = self.memory.context(session.goal)

            # 1. Plan
            session.set_phase("planning")
            plan = self.planner.create_plan(
                session.goal, self.registry.descriptions(), self.registry.has,
                session.memory_snapshot,
            )
            session.plan = plan.to_dict()
            session.emit("plan", steps=session.plan, source=plan.source, dropped=plan.dropped)
            session.log(f"[ULTRON] Plan ready ({plan.source}): "
                        f"{len(session.plan)} step(s)")
            for name in plan.dropped:
                session.log(f"[ULTRON] ! Planner referenced unknown tool '{name}' - dropped")

            # 2. Execute, replanning around genuine failures
            session.set_phase("executing", steps=len(session.plan))
            pending: List[Dict[str, Any]] = list(session.plan)
            done: List[StepResult] = []
            replans = 0

            while pending and session.active:
                batch = self.executor.execute_plan(session, pending)
                results.extend(batch)
                done.extend(r for r in batch if r.ok)

                aborted = any(r.decision is ErrorDecision.ABORT for r in batch)
                failed = [r for r in batch if not r.ok]
                if aborted or not failed:
                    break
                if replans >= MAX_REPLAN_ATTEMPTS:
                    session.log(f"[ULTRON] Replan budget exhausted "
                                f"({MAX_REPLAN_ATTEMPTS}) - stopping with failures outstanding")
                    break

                replans += 1
                session.replans = replans
                failure = "; ".join(f"{r.tool}: {r.output[:200]}" for r in failed)
                session.log(f"[ULTRON] Replanning ({replans}/{MAX_REPLAN_ATTEMPTS}) after failure")

                replanned = self.planner.replan(
                    session.goal,
                    [{"tool": r.tool, "output": r.output} for r in done],
                    failure, self.registry.descriptions(), self.registry.has,
                    session.memory_snapshot,
                )
                pending = replanned.to_dict()
                if not pending:
                    break
                session.plan.extend(pending)
                session.emit("replan", attempt=replans, steps=pending)

            # 3. Honest status
            session.set_phase("verifying")
            #
            # A run that fell back to the degraded plan cannot claim success:
            # the tool that ran was not the plan the goal called for, so the
            # goal itself is unverified even when the tool exited cleanly.
            degraded = plan.source != "llm"
            #
            # The one exception is a *conversational* goal answered by
            # ultron_answer: there is no tool action to verify, so a successful
            # answer IS the completion. Without this carve-out every ordinary
            # question ("who are you", "what can you do") was reported as
            # "completed with issues - goal unverified", which is wrong and made
            # the agent look broken in front of users.
            answered_directly = bool(results) and all(
                r.tool == "ultron_answer" for r in results
            )
            if answered_directly:
                degraded = False

            if session.stop.is_set():
                session.status = "stopped"
                summary = "Stopped by user."
            elif not results:
                session.status = "failed"
                summary = "No tool actions were executed - nothing was done."
                session.log("[ULTRON] Task ended with NO executed actions.")
            else:
                bad = [r for r in results if not r.ok]
                if bad:
                    session.status = "completed_with_issues"
                    session.log(f"[ULTRON] Task finished with issues: "
                                f"{len(results) - len(bad)} OK / {len(bad)} failed.")
                elif degraded:
                    session.status = "completed_with_issues"
                    summary = ("The planner could not build a plan for this goal, so only a "
                               "degraded fallback step ran. The goal is NOT verified as done.")
                    session.log("[ULTRON] Degraded fallback plan - goal unverified.")
                else:
                    session.status = "completed"
                    session.log("[ULTRON] Task complete.")

            # 4. Report from real results, then remember them.
            report = self._report(session, results)
            if session.status == "completed_with_issues":
                if degraded:
                    prefix = ("! Completed with issues - only a degraded fallback step ran, "
                              "so the goal is unverified.\n\n")
                else:
                    prefix = (f"! Completed with issues - "
                              f"{sum(1 for r in results if not r.ok)} of {len(results)} "
                              f"steps did not succeed.\n\n")
                report = prefix + report
            summary = report or summary

            errors = "; ".join(f"{r.tool}: {r.output[:160]}" for r in results if not r.ok)
            self.memory.remember(
                session.goal, session.plan,
                session.status == "completed", errors[:1000],
            )
            if errors:
                self.memory.auto_learn(session.goal, errors)

        except Exception as exc:  # noqa: BLE001 - a crash must still close the SSE stream
            log.exception("Ultron run %s failed", session.task_id)
            session.status = "failed"
            session.set_phase("failed", reason=f"{type(exc).__name__}: {exc}"[:200])
            session.log(f"[ULTRON] ERROR: {type(exc).__name__}: {exc}")
            session.emit("done", status="failed", summary=f"{type(exc).__name__}: {exc}",
                         steps=session.steps)
            return

        # Terminal phase, taken straight from the status the run actually
        # ended on. A run that finished with issues is NOT shown as completed.
        session.set_phase(
            session.status if session.status in TERMINAL_PHASES else "failed"
        )
        session.emit("done", status=session.status, summary=summary,
                     steps=session.steps, elapsed=round(time.time() - session.created, 1))


def _render_arg(value: Any) -> str:
    """Render one plan arg compactly, whatever type it is.

    Args can hold nested objects and lists (a planner is free to pass a dict to
    a tool that takes one), so this must never assume a string.
    """
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    text = " ".join(str(text).split())
    return text if len(text) <= 60 else text[:57] + "..."


def json_lines(items: List[Dict[str, Any]]) -> str:
    """Render plan steps compactly for the report prompt."""
    if not items:
        return "(no plan)"
    lines = []
    for i, step in enumerate(items):
        args = step.get("args") or {}
        rendered = ", ".join(f"{k}={_render_arg(v)}" for k, v in args.items())
        lines.append(f"{i + 1}. {step.get('tool')}({rendered})")
    return "\n".join(lines)


def run_session(session: UltronSession) -> None:
    """Entry point used by the API layer's worker thread."""
    UltronOrchestrator(get_registry(), get_memory()).run(session)
