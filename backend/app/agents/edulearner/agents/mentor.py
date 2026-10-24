"""Conversational mentor agent with a deterministic local answer engine.

Uses plan/gap/readiness state from the context so guidance is always tied to
evidence. When the shared SARA LLM brain is configured it *enhances* the
grounded answer instead of replacing it; without it the deterministic answer
is returned, so the mentor never fabricates scores or links.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..core.models import ChatResponse
from ..core.primitives import confidence
from .base import AgentContext, BaseAgent

logger = logging.getLogger("sara.edulearner")

SYSTEM_PROMPT = (
    "You are EduLearner AI, an evidence-driven learning mentor inside the SARA "
    "AI Nexus Hub. Ground every answer in the learner's profile, skill gaps, "
    "progress data and memory provided in the context. Be concrete, never "
    "invent scores, and always end with one actionable next step."
)


def _context_summary(ctx: AgentContext) -> str:
    profile = ctx.profile
    return (
        f"target_role={ctx.target_role.name if ctx.target_role else 'unset'}, "
        f"skills={[(s.name, s.score) for s in ctx.skills][:10]}, "
        f"top_gaps={[(g.skill, round(g.gap)) for g in ctx.gaps][:5]}, "
        f"recent_scores={ctx.recent_scores()}, "
        f"memory={ctx.memory}, "
        f"available_hours={profile.available_hours}, "
        f"learning_style={profile.learning_style or 'mixed'}"
    )


class ConversationalMentorAgent(BaseAgent):
    """Intent-routed mentor whose answers are always grounded in evidence."""

    name = "ConversationalMentorAgent"

    async def execute(self, ctx: AgentContext, message: str = "",
                      **kwargs) -> Dict[str, Any]:
        question = (message or "").strip()
        intent = self._intent(question)
        answer, actions, evidence = self._local_answer(ctx, intent)

        llm_used = False
        llm_model = ""
        llm = kwargs.get("llm")
        if llm is None:
            from ..llm import get_llm
            llm = get_llm()
        if llm is not None and llm.available:
            try:
                enhanced = llm.chat([
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "system", "content": "Learner context: " + _context_summary(ctx)[:1500]},
                    {"role": "user", "content": question or "Help me plan my next study session."},
                ])
                if enhanced and enhanced.strip():
                    answer = enhanced.strip()
                    llm_used = True
                    llm_model = getattr(llm, "active_model", "") or llm.model
            except Exception as exc:  # noqa: BLE001 - chat must never fail
                logger.info("LLM mentor unavailable (%s); using grounded local answer",
                            str(exc)[:180])

        response = ChatResponse(
            answer=answer,
            actions=actions,
            evidence=evidence,
            confidence=confidence(0.72 if llm_used else 0.6),
        )
        payload = response.model_dump()
        payload["llm_used"] = llm_used
        payload["llm_model"] = llm_model
        return payload

    @staticmethod
    def _intent(question: str) -> str:
        text = question.lower()
        if any(w in text for w in ("today", "next", "plan", "what should i", "study")):
            return "plan"
        if any(w in text for w in ("ready", "interview", "job", "hire")):
            return "readiness"
        if any(w in text for w in ("weak", "gap", "struggl", "improve", "bad at", "hard")):
            return "weakness"
        if any(w in text for w in ("strong", "good at", "strength", "skilled")):
            return "strength"
        if any(w in text for w in ("resource", "book", "course", "tutorial", "learn", "video")):
            return "resources"
        if any(w in text for w in ("progress", "streak", "how am i", "report")):
            return "progress"
        if any(w in text for w in ("explain", "what is", "how does", "why")):
            return "explain"
        return "general"

    def _local_answer(self, ctx: AgentContext, intent: str):
        plan_title = ctx.plan[0].title if ctx.plan else None
        top_gap = ctx.gaps[0].skill if ctx.gaps else None
        weaknesses = (ctx.memory or {}).get("weaknesses", [])
        strengths = (ctx.memory or {}).get("strengths", [])
        evidence: List[str] = []
        if top_gap:
            evidence.append(f"Top gap: {top_gap}")
        if ctx.readiness:
            evidence.append(f"Readiness: {ctx.readiness.overall:.0f}/100")
        if ctx.plan:
            evidence.append(f"{len(ctx.plan)} tasks scheduled today")

        if intent == "plan":
            focus = plan_title or (top_gap or "a baseline assessment")
            answer = (f"Today, focus on {focus}. It is the highest-value work right now "
                      f"because it targets your largest measurable gap.")
            actions = [task.title for task in ctx.plan[:3]] or ["Generate today's plan"]
        elif intent == "readiness":
            if ctx.readiness:
                weakest = sorted(ctx.readiness.category_scores,
                                 key=lambda k: ctx.readiness.category_scores[k])[:2]
                answer = (f"Your interview readiness is {ctx.readiness.overall:.0f}/100. "
                          f"Weakest dimensions: {', '.join(weakest)}. "
                          "Readiness is evidence-based, not a guarantee.")
            else:
                answer = ("Run a readiness analysis first so I can score you on role "
                          "skills, DSA, projects and system design.")
            actions = ["Complete a mock interview", "Review the highest priority gaps"]
        elif intent == "weakness":
            focus = ", ".join((weaknesses or ([top_gap] if top_gap else []))[:4]) \
                    or "no recorded weaknesses yet"
            answer = (f"The most important weaknesses are {focus}. We will work on them "
                      f"in order of priority, prerequisites first.")
            actions = ["Use simpler practice on the weakest skill", "Reassess after revision"]
        elif intent == "strength":
            focus = ", ".join(strengths[:5]) or "no strengths recorded yet"
            answer = (f"Your strongest evidenced skills are {focus}. Lean on these in "
                      f"interviews and use them to anchor related skills.")
            actions = ["Build a portfolio piece around a strength",
                       "Mentor a peer on a strength"]
        elif intent == "resources":
            focus = top_gap or "your top gap"
            answer = (f"For {focus}, I recommend structured practice over passive "
                      f"reading: one concept, one guided example, one independent "
                      f"problem, then an assessment.")
            actions = ["Generate a practice set", "Open the roadmap"]
        elif intent == "progress":
            records = len(ctx.progress)
            completed = sum(1 for item in ctx.progress if item.completed)
            answer = (f"You have {records} progress records and {completed} completed "
                      f"tasks. {'Keep the streak going.' if completed else 'Log a session to start your streak.'}")
            actions = ["Open the progress report", "Generate next week's plan"]
        elif intent == "explain":
            answer = ("Start with the simplest invariant, trace one concrete example, "
                      "then test an edge case. I can personalise this after an assessment.")
            actions = ["Generate a beginner practice set"]
        else:
            focus = top_gap or "a skill assessment"
            answer = (f"I am using your profile, gaps, progress and memory. Your next "
                      f"evidence-backed focus is {focus}.")
            actions = ["Generate today's plan"]

        return answer, actions, evidence

