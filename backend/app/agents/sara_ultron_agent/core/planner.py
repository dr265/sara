"""Ultron planner - turns a goal into a validated, ordered list of tool steps.

Adapted from the uploaded Brahma-Echo planner (``agent/planner.py``), with two
deliberate changes:

* **Brahma-Echo called ``google.generativeai`` directly.** Ultron calls SARA's
  centralized LLM router (``app.core.llm_router.llm_chat_sync``) so it inherits
  provider failover, model fallback and health tracking like every other agent.
* **Brahma-Echo returned a plan but never checked it against the tools that
  actually exist.** Ultron validates every step against the live tool registry
  and reports what it dropped, so a hallucinated tool name cannot silently
  become a failed run.

The plan is intentionally a *list of tool calls*, not free-form prose: the
executor runs it for real and the final report is built from real outputs.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.llm_router import llm_chat_sync

log = logging.getLogger("sara.ultron.planner")

MAX_STEPS = 8               # ceiling on plan length
MAX_REPLAN_ATTEMPTS = 2     # ceiling on replans per run
LLM_TIMEOUT_S = 120.0

_PLANNER_PROMPT = """You are the planning stage of SARA Ultron, a supervisory agent \
inside the SARA AI Nexus Hub.

You turn a user goal into the SHORTEST ordered list of concrete tool calls that \
achieves it. Today is {today}.

AVAILABLE TOOLS
{tools}

SARA AGENTS AVAILABLE FOR DELEGATION
{agents}

PAST LEARNINGS
{memory}

RULES
1. Use ONLY tool names from the list above. Never invent a tool.
2. NEVER return an empty plan. An empty "steps" list is treated as a planning
   failure. If the user asked a question, answered a greeting, or wants
   information rather than an action, plan exactly one step calling
   ultron_answer with the user's message as "question". The ONLY valid reason to
   plan nothing is a goal that is already satisfied.
3. If another SARA agent is a better fit than anything you can do yourself, use
   ultron_delegate for that part of the goal.
4. Use EXACTLY the argument names listed after each tool. Never rename an
   argument (ultron_delegate wants "message", not "task") and never invent one.
   A call missing a required argument is rejected without being executed.
5. Steps run in order and each one's real output is fed back. Do not plan
   parallel work; chain steps instead.
6. To reuse an earlier step's output, write the literal placeholder
   ${{stepN_output}} (N = that step's 1-based number) or ${{previous_output}}.
   It is substituted with that step's actual result before the tool runs.
   Never write placeholder names that do not match this pattern.
7. Plan the minimum number of steps. If a single tool answers the goal, plan one step.
8. Output raw JSON only - no prose, no markdown fence, no explanation.

OUTPUT FORMAT
{{"steps": [{{"tool": "<tool name>", "args": {{...}}, "rationale": "<why, max 15 words>"}}]}}
If no tool is needed, output {{"steps": []}}."""


@dataclass
class PlanStep:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"tool": self.tool, "args": self.args, "rationale": self.rationale}


@dataclass
class Plan:
    steps: List[PlanStep] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)   # hallucinated tool names
    source: str = "fallback"                           # "llm" | "fallback"

    def to_dict(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.steps]


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> Optional[Any]:
    """Pull the first JSON object/array out of an LLM reply.

    Models wrap JSON in prose or fences often enough that a bare
    ``json.loads`` is not a safe parse. Handles fences, and falls back to
    brace/bracket scanning.
    """
    if not text:
        return None
    candidates: List[str] = []
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text.strip())

    for cand in candidates:
        try:
            return json.loads(cand)
        except ValueError:
            pass
        # Objects are tried before arrays: a plan is {"steps": [...]}, so a
        # bare array would be the wrong shape to return.
        for opener, closer in (("{", "}"), ("[", "]")):
            start = cand.find(opener)
            end = cand.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(cand[start:end + 1])
                except ValueError:
                    continue
    return None


def _coerce_steps(payload: Any) -> List[Dict[str, Any]]:
    """Normalize the LLM's plan payload into a list of raw step dicts."""
    if isinstance(payload, dict):
        raw = payload.get("steps")
    elif isinstance(payload, list):
        raw = payload
    else:
        return []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
class UltronPlanner:
    """LLM-backed planner with a deterministic fallback."""

    def __init__(self, max_steps: int = MAX_STEPS,
                 max_replans: int = MAX_REPLAN_ATTEMPTS) -> None:
        self.max_steps = max_steps
        self.max_replans = max_replans

    # -- helpers --------------------------------------------------------
    def _system(self, tools_desc: str, agents_desc: str, memory: str) -> str:
        from datetime import datetime

        return _PLANNER_PROMPT.format(
            today=datetime.now().strftime("%A, %d %B %Y"),
            tools=tools_desc or "(none)",
            agents=agents_desc or "(none)",
            memory=memory or "(no prior memory)",
        )

    @staticmethod
    def _agents_desc() -> str:
        from .catalog import AGENT_CATALOG

        return "\n".join(f"- {a['id']}: {a['short_description']}" for a in AGENT_CATALOG)

    def _validate(self, raw_steps: List[Dict[str, Any]], has_tool: Any) -> Plan:
        """Keep only steps whose tool actually exists; record the rest.

        Collapses repeated calls to the same tool with identical args - a
        common LLM failure mode that otherwise burns the whole step budget.
        """
        plan = Plan(source="llm")
        seen: set[tuple[str, str]] = set()
        for raw in raw_steps:
            if len(plan.steps) >= self.max_steps:
                break
            tool = str(raw.get("tool") or raw.get("name") or "").strip()
            if not tool:
                plan.dropped.append("(missing tool name)")
                continue
            if not has_tool(tool):
                plan.dropped.append(tool)
                log.warning("Planner produced unknown tool %r - dropped", tool)
                continue
            args = raw.get("args")
            if not isinstance(args, dict):
                args = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
            key = (tool, json.dumps(args, sort_keys=True, default=str))
            if key in seen:
                log.debug("Planner produced a duplicate step for %r - collapsed", tool)
                continue
            seen.add(key)
            rationale = str(raw.get("rationale") or "")[:200]
            plan.steps.append(PlanStep(tool=tool, args=args, rationale=rationale))
        return plan

    @staticmethod
    def fallback(goal: str) -> Plan:
        """Deterministic single-step plan used when the LLM cannot plan.

        ``ultron_answer`` is chosen rather than ``ultron_summarize`` because a
        failed planning call means we do not know what the user wants, and
        asking them is always safe and always useful. It also still routes
        through the LLM router, so it degrades gracefully with everything else.
        """
        return Plan(
            steps=[PlanStep(tool="ultron_answer",
                            args={"question": goal},
                            rationale="Planner unavailable; answered the goal directly.")],
            source="fallback",
        )

    def _conversational(self, goal: str, has_tool: Any) -> Plan:
        """Route a question to ``ultron_answer`` instead of degrading.

        The planner legitimately returns ``{"steps": []}`` when the user asked
        something informational. That is a *valid* plan meaning "no tool is
        needed", but the runner treated an empty step list as a planning
        failure, so every ordinary question ("who are you", "what can you do")
        came back as "degraded - goal unverified". This converts that empty plan
        into a real answer while keeping it clearly labelled as a fallback so
        the honesty rule in the orchestrator still holds.
        """
        if not has_tool("ultron_answer"):
            return self.fallback(goal)
        return Plan(
            steps=[PlanStep(tool="ultron_answer",
                            args={"question": goal},
                            rationale="Informational request; answered directly.")],
            source="fallback",
        )

    # -- public API -----------------------------------------------------
    def create_plan(self, goal: str, tools_desc: str, has_tool: Any,
                    memory: str = "") -> Plan:
        """Produce a validated plan for ``goal``."""
        try:
            reply = llm_chat_sync(
                [
                    {"role": "system", "content": self._system(tools_desc, self._agents_desc(), memory)},
                    {"role": "user", "content": goal},
                ],
                max_tokens=900,
                temperature=0.2,
                timeout=LLM_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - planning must never kill the run
            log.warning("Planner LLM call failed: %s", type(exc).__name__)
            return self.fallback(goal)

        payload = extract_json(reply or "")
        if payload is None:
            log.warning("Planner reply was not valid JSON - using fallback")
            return self.fallback(goal)

        plan = self._validate(_coerce_steps(payload), has_tool)
        if not plan.steps:
            # Empty plan == "no tool needed" (usually a question). Answer it
            # rather than reporting a failed run.
            log.info("Planner produced no usable steps - routing to ultron_answer")
            routed = self._conversational(goal, has_tool)
            # Carry the dropped-tool report across, otherwise the run loses the
            # only evidence that the planner hallucinated a tool name.
            routed.dropped = list(plan.dropped)
            return routed
        return plan

    def replan(self, goal: str, completed: List[Dict[str, Any]], failure: str,
               tools_desc: str, has_tool: Any, memory: str = "") -> Plan:
        """Produce a fresh plan for the remainder after a step failed.

        ``completed`` carries the steps that already ran, so the new plan does
        not repeat them. ``failure`` is the real error text, so the planner can
        route around the cause instead of retrying it blindly.
        """
        done = "\n".join(f"- {c.get('tool')}: {str(c.get('output', ''))[:200]}" for c in completed)
        brief = (
            f"ORIGINAL GOAL\n{goal}\n\n"
            f"ALREADY COMPLETED (do not repeat)\n{done or '(nothing succeeded yet)'}\n\n"
            f"FAILURE THAT MUST BE WORKED AROUND\n{failure[:800]}"
        )
        try:
            reply = llm_chat_sync(
                [
                    {"role": "system", "content": self._system(tools_desc, self._agents_desc(), memory)},
                    {"role": "user", "content":
                        brief + "\n\nProduce a plan for only what is still outstanding."},
                ],
                max_tokens=900,
                temperature=0.2,
                timeout=LLM_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Replan LLM call failed: %s", type(exc).__name__)
            return self.fallback(goal)

        payload = extract_json(reply or "")
        if payload is None:
            return self.fallback(goal)
        plan = self._validate(_coerce_steps(payload), has_tool)
        return plan if plan.steps else self.fallback(goal)
