"""Agent base classes and the shared per-request execution context.

Agents never call each other directly: they read and write through the
AgentContext, which keeps implementations stateless, testable in isolation,
and gives the orchestrator full execution traceability.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..core.models import (
    AssessmentResult,
    DailyTask,
    ProgressRecord,
    RoleProfile,
    Skill,
    SkillEvidence,
    SkillGap,
    UserProfile,
)
from ..core.primitives import now, rolling_stats

logger = logging.getLogger("sara.edulearner")


class AgentResult(BaseModel):
    """Uniform envelope returned by every agent."""

    agent_name: str
    success: bool = True
    data: Any = Field(default_factory=dict)
    confidence: float = 0.0
    warnings: List[str] = Field(default_factory=list)
    next_actions: List[str] = Field(default_factory=list)
    execution_metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentContext:
    """Mutable, per-request working state shared by the agents in a workflow."""

    def __init__(self, user_id: str, profile: Optional[UserProfile] = None) -> None:
        self.user_id = user_id
        self.profile = profile or UserProfile(user_id=user_id)
        self.target_role: Optional[RoleProfile] = None
        self.evidence: List[SkillEvidence] = []
        self.skills: List[Skill] = []
        self.gaps: List[SkillGap] = []
        self.roadmap: Dict[str, Any] = {}
        self.plan: List[DailyTask] = []
        self.progress: List[ProgressRecord] = []
        self.assessments: List[AssessmentResult] = []
        self.memory: Dict[str, Any] = {}
        self.readiness = None
        self.outputs: Dict[str, Any] = {}
        self.practice_history: Dict[str, List[Dict[str, Any]]] = {}
        self.remediation: Dict[str, Dict[str, Any]] = {}

    def skill_by_name(self, name: str) -> Optional[Skill]:
        key = (name or "").strip().lower()
        return next((s for s in self.skills if s.name.strip().lower() == key), None)

    def gap_by_name(self, name: str) -> Optional[SkillGap]:
        key = (name or "").strip().lower()
        return next((g for g in self.gaps if g.skill.strip().lower() == key), None)

    def record_practice(self, skill: str, score: float, difficulty: str = "",
                        task_id: Optional[str] = None) -> None:
        key = (skill or "").strip()
        if not key:
            return
        self.practice_history.setdefault(key, []).append(
            {"score": float(score), "difficulty": difficulty or "",
             "task_id": task_id, "at": now()}
        )
        self.practice_history[key] = self.practice_history[key][-25:]

    def scores_for(self, skill: Optional[str] = None, limit: int = 5) -> List[float]:
        if skill:
            return [item["score"] for item in
                    self.practice_history.get(skill.strip(), [])]
        return [r.score for r in self.progress[-limit:] if r.score is not None] or [
            item["score"] for items in self.practice_history.values() for item in items
        ]

    def stats_for(self, skill: Optional[str] = None) -> Dict[str, Any]:
        if skill:
            return rolling_stats(self.scores_for(skill))
        merged = [item["score"] for items in self.practice_history.values()
                  for item in items]
        return rolling_stats(merged if merged else self.recent_scores())

    def recent_scores(self, limit: int = 5) -> List[float]:
        return [r.score for r in self.progress[-limit:] if r.score is not None]

    def snapshot(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "profile": self.profile.model_dump(),
            "target_role": self.target_role.model_dump() if self.target_role else None,
            "skills": [s.model_dump() for s in self.skills],
            "gaps": [g.model_dump() for g in self.gaps],
            "roadmap": self.roadmap,
            "plan": [t.model_dump() for t in self.plan],
            "progress": [r.model_dump() for r in self.progress],
            "practice_history": self.practice_history,
            "remediation": self.remediation,
            "memory": self.memory,
        }


class BaseAgent:
    """Base class: instruments execution and converts failures into a result."""

    name = "BaseAgent"

    async def run(self, ctx: AgentContext, **kwargs) -> AgentResult:
        started = time.monotonic()
        try:
            data = await self.execute(ctx, **kwargs)
            result = AgentResult(
                agent_name=self.name,
                data=data,
                confidence=self.confidence_for(ctx, data),
                next_actions=self.next_actions_for(ctx, data),
                execution_metadata={
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                    "agent": self.name,
                },
            )
            ctx.outputs[self.name] = data
            return result
        except Exception as exc:  # noqa: BLE001 - a failing agent must never kill a workflow
            logger.exception("edulearner agent=%s failed", self.name)
            return AgentResult(
                agent_name=self.name,
                success=False,
                warnings=[f"{type(exc).__name__}: {exc}"],
                execution_metadata={
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                    "agent": self.name,
                },
            )

    async def execute(self, ctx: AgentContext, **kwargs) -> Any:
        raise NotImplementedError

    def confidence_for(self, ctx: AgentContext, data: Any) -> float:
        return 0.8

    def next_actions_for(self, ctx: AgentContext, data: Any) -> List[str]:
        return []

