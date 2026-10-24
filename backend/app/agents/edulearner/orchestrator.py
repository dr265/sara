"""Agent registry and workflow orchestration for EduLearner AI.

The orchestrator owns the registry (discovery), the shared context built from
persisted state, the execution workflows (initial analysis, adaptive loop),
and the audit trail of every agent execution (monitoring).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any, Dict, List, Optional

from .agents.base import AgentContext, AgentResult, BaseAgent
from .agents.profile_career import (
    CareerRoleAnalyzerAgent,
    PrerequisiteAgent,
    ProfileAnalyzerAgent,
    SkillGapAnalyzerAgent,
)
from .agents.learning import (
    PlannerAgent,
    PracticeGeneratorAgent,
    RoadmapGeneratorAgent,
)
from .agents.adaptive import AdaptiveLearningAgent
from .agents.mentor import ConversationalMentorAgent
from .agents.progress import (
    InterviewReadinessAgent,
    MemoryAgent,
    ProgressTrackerAgent,
    ReportGeneratorAgent,
)
from .core.models import (
    AssessmentResult,
    DailyTask,
    ProgressRecord,
    Skill,
    SkillGap,
    UserProfile,
)
from .core.primitives import now
from .llm import get_llm
from .repository import Repository, get_repository

logger = logging.getLogger("sara.edulearner")

AGENT_CLASSES: List[type] = [
    ProfileAnalyzerAgent,
    CareerRoleAnalyzerAgent,
    SkillGapAnalyzerAgent,
    PrerequisiteAgent,
    RoadmapGeneratorAgent,
    PlannerAgent,
    PracticeGeneratorAgent,
    AdaptiveLearningAgent,
    ProgressTrackerAgent,
    InterviewReadinessAgent,
    MemoryAgent,
    ConversationalMentorAgent,
    ReportGeneratorAgent,
]

# Ordered pipeline shown by the front-end's analysis visualiser.
ANALYSIS_PIPELINE: List[Dict[str, str]] = [
    {"agent": "ProfileAnalyzerAgent", "label": "Profile"},
    {"agent": "CareerRoleAnalyzerAgent", "label": "Career Role"},
    {"agent": "SkillGapAnalyzerAgent", "label": "Skill Gap"},
    {"agent": "PrerequisiteAgent", "label": "Prerequisites"},
    {"agent": "RoadmapGeneratorAgent", "label": "Roadmap"},
    {"agent": "PlannerAgent", "label": "Planner"},
    {"agent": "InterviewReadinessAgent", "label": "Readiness"},
    {"agent": "MemoryAgent", "label": "Memory"},
]

AGENT_DESCRIPTIONS: Dict[str, str] = {
    "ProfileAnalyzerAgent": "Builds the skill inventory from profile, resume and project evidence.",
    "CareerRoleAnalyzerAgent": "Resolves the target role into a weighted competency model.",
    "SkillGapAnalyzerAgent": "Measures the distance between current skills and the target role.",
    "PrerequisiteAgent": "Computes a dependency-respecting learning order.",
    "RoadmapGeneratorAgent": "Turns prioritised gaps into phases, weeks and daily tasks.",
    "PlannerAgent": "Allocates available time across the top gaps for today or this week.",
    "PracticeGeneratorAgent": "Generates real, marked practice questions from the item bank.",
    "AdaptiveLearningAgent": "Observes scores and decides remediation, support or acceleration per skill.",
    "ProgressTrackerAgent": "Records progress and derives per-skill accuracy and streaks.",
    "InterviewReadinessAgent": "Scores interview readiness across role-critical dimensions.",
    "MemoryAgent": "Maintains long-term memory: strengths, weaknesses, mistakes, bottleneck.",
    "ConversationalMentorAgent": "Intent-routed mentor grounded in evidence, enhanced by the LLM brain.",
    "ReportGeneratorAgent": "Produces a structured progress report from recorded evidence.",
}


class OrchestratorAgent:
    """Owns the agent registry, shared context construction and persistence."""

    def __init__(self, repo: Optional[Repository] = None) -> None:
        self.repo = repo if repo is not None else get_repository()
        self.llm = get_llm()
        self.agents: Dict[str, BaseAgent] = {cls.name: cls() for cls in AGENT_CLASSES}
        logger.info("EduLearner AI orchestrator initialised with %d agents (llm=%s)",
                    len(self.agents),
                    "configured" if self.llm.available else "offline")

    # -- discovery / monitoring -------------------------------------------------
    def registry(self) -> Dict[str, Any]:
        return {
            "agent": "EduLearnerAI",
            "version": "1.0.0",
            "status": "online",
            "count": len(self.agents),
            "pipeline": ANALYSIS_PIPELINE,
            "agents": [
                {"name": name, "description": AGENT_DESCRIPTIONS.get(name, "")}
                for name in sorted(self.agents)
            ],
            "llm": "configured" if self.llm.available else "offline",
            "database": self.repo.health(),
        }

    # -- context ---------------------------------------------------------------
    def context(self, user_id: str) -> AgentContext:
        """Rebuild the working context from persisted state."""

        raw = self.repo.get("profile", user_id)
        profile = UserProfile.model_validate(raw) if raw else UserProfile(user_id=user_id)
        ctx = AgentContext(user_id, profile)

        saved_skills = self.repo.get("skills", user_id)
        if saved_skills:
            ctx.skills = [Skill.model_validate(item) for item in saved_skills]
        saved_gaps = self.repo.get("gaps", user_id)
        if saved_gaps:
            ctx.gaps = [SkillGap.model_validate(item) for item in saved_gaps]
        ctx.progress = [ProgressRecord.model_validate(item)
                        for item in (self.repo.get("progress", user_id) or [])]
        ctx.assessments = [AssessmentResult.model_validate(item)
                           for item in (self.repo.get("assessments", user_id) or [])]
        ctx.memory = self.repo.get("memory", user_id) or {}
        ctx.practice_history = self.repo.get("practice_history", user_id) or {}
        ctx.remediation = self.repo.get("remediation", user_id) or {}
        roadmap = self.repo.get("roadmap", user_id)
        if roadmap:
            ctx.roadmap = roadmap
        plan = self.repo.get("plan", user_id)
        if plan:
            ctx.plan = [DailyTask.model_validate(item) for item in plan]
        return ctx

    def call(self, ctx: AgentContext, name: str, **kwargs) -> AgentResult:
        """Invoke a single agent by name, recording the execution."""

        agent = self.agents.get(name)
        if agent is None:
            raise KeyError(f"Unknown agent: {name}")
        result = self._run(agent, ctx, **kwargs)
        self.repo.append("adaptations", ctx.user_id, {
            "agent": name, "success": result.success,
            "confidence": result.confidence,
            "warnings": result.warnings,
            "latency_ms": result.execution_metadata.get("latency_ms"),
            "at": now(),
        }, limit=200)
        return result

    @staticmethod
    def _run(agent: BaseAgent, ctx: AgentContext, **kwargs) -> AgentResult:
        """Run the async execute synchronously (agents are pure/deterministic)."""

        coro = agent.run(ctx, **kwargs)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # FastAPI context: schedule on a fresh loop in a worker thread.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        return asyncio.run(coro)


    # -- persistence -------------------------------------------------------------
    def persist(self, ctx: AgentContext) -> None:
        self.repo.put("profile", ctx.user_id, ctx.profile.model_dump())
        self.repo.put("skills", ctx.user_id, [s.model_dump() for s in ctx.skills])
        self.repo.put("gaps", ctx.user_id, [g.model_dump() for g in ctx.gaps])
        self.repo.put("practice_history", ctx.user_id, ctx.practice_history)
        self.repo.put("remediation", ctx.user_id, ctx.remediation)
        if ctx.roadmap:
            self.repo.put("roadmap", ctx.user_id, ctx.roadmap)
        if ctx.plan:
            self.repo.put("plan", ctx.user_id, [t.model_dump() for t in ctx.plan])
        self.repo.put("memory", ctx.user_id, ctx.memory)

    # -- workflows ---------------------------------------------------------------
    def initial(self, ctx: AgentContext) -> Dict[str, Any]:
        """Full onboarding analysis: profile -> role -> gaps -> roadmap -> plan."""

        self.call(ctx, "ProfileAnalyzerAgent")
        self.call(ctx, "CareerRoleAnalyzerAgent", role=ctx.profile.target_role)
        self.call(ctx, "SkillGapAnalyzerAgent")
        self.call(ctx, "PrerequisiteAgent")
        self.call(ctx, "RoadmapGeneratorAgent")
        plan = self.call(ctx, "PlannerAgent")
        self.call(ctx, "InterviewReadinessAgent")
        self.call(ctx, "MemoryAgent")
        self.persist(ctx)
        return {
            "profile": ctx.outputs.get("ProfileAnalyzerAgent", {}),
            "gaps": ctx.outputs.get("SkillGapAnalyzerAgent", []),
            "roadmap": ctx.roadmap,
            "plan": plan.data,
            "readiness": ctx.outputs.get("InterviewReadinessAgent", {}),
            "agents": sorted(self.agents),
        }

    def adaptive_loop(self, ctx: AgentContext,
                      record: Optional[ProgressRecord] = None) -> Dict[str, Any]:
        """The feedback loop: track -> adapt -> re-gap -> replan -> persist."""

        tracking = self.call(ctx, "ProgressTrackerAgent", record=record)
        if record is not None and record.skill:
            ctx.record_practice(record.skill,
                                record.score if record.score is not None else
                                (100.0 if record.completed else 0.0),
                                task_id=record.task_id)
        adaptation = self.call(
            ctx, "AdaptiveLearningAgent", skill=record.skill if record else None)
        self.repo.put("last_adaptation", ctx.user_id, adaptation.data)
        self.call(ctx, "SkillGapAnalyzerAgent")
        self.call(ctx, "RoadmapGeneratorAgent")
        plan = self.call(ctx, "PlannerAgent")
        self.call(ctx, "InterviewReadinessAgent")
        self.call(ctx, "MemoryAgent")
        self.persist(ctx)
        return {
            "progress": tracking.data,
            "adaptation": adaptation.data,
            "roadmap": ctx.roadmap,
            "plan": plan.data,
            "readiness": ctx.outputs.get("InterviewReadinessAgent", {}),
        }

    def ensure_analysis(self, ctx: AgentContext) -> None:
        """Make sure gaps, a plan and readiness exist before serving a dashboard."""

        if not ctx.target_role:
            self.call(ctx, "CareerRoleAnalyzerAgent")
        if not ctx.gaps:
            self.call(ctx, "SkillGapAnalyzerAgent")
        if not ctx.plan:
            self.call(ctx, "PlannerAgent")
        if ctx.readiness is None:
            self.call(ctx, "InterviewReadinessAgent")


_orchestrator: Optional[OrchestratorAgent] = None


def get_orchestrator() -> OrchestratorAgent:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = OrchestratorAgent()
    return _orchestrator

