"""Progress tracking, memory, interview readiness and reporting agents."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from ..core.enums import Difficulty
from ..core.models import InterviewReadiness, ProgressRecord, ProgressReport
from ..core.primitives import clamp, confidence, now
from ..core.scoring import ScoringEngine
from .base import AgentContext, BaseAgent


class ProgressTrackerAgent(BaseAgent):
    """Records progress and derives per-skill accuracy and streaks."""

    name = "ProgressTrackerAgent"

    async def execute(self, ctx: AgentContext, record: Optional[ProgressRecord] = None,
                      **kwargs) -> Dict[str, Any]:
        if record is not None:
            ctx.progress.append(record)

        by_skill: Dict[str, List[float]] = defaultdict(list)
        for item in ctx.progress:
            if not item.skill:
                continue
            value = item.score if item.score is not None else (100.0 if item.completed else 0.0)
            by_skill[item.skill].append(value)

        stats = {
            skill: {
                "attempts": len(values),
                "accuracy": round(statistics.mean(values), 2),
                "completed": sum(1 for v in values if v >= 50),
                "trend": _trend(values),
            }
            for skill, values in by_skill.items()
        }
        completed = sum(1 for item in ctx.progress if item.completed)
        return {
            "records": len(ctx.progress),
            "completed": completed,
            "skills": stats,
            "streak": self.streak(ctx.progress),
            "minutes_logged": round(sum(i.time_spent_minutes for i in ctx.progress), 1),
        }

    @staticmethod
    def streak(records: List[ProgressRecord]) -> int:
        """Consecutive days (ending today) with at least one completion."""

        days = {item.created_at[:10] for item in records if item.completed}
        streak = 0
        day = date.today()
        grace_used = False
        while day.isoformat() in days or (not grace_used and streak > 0):
            if day.isoformat() not in days:
                grace_used = True
            else:
                streak += 1
            day -= timedelta(days=1)
        return streak


def _trend(values: List[float]) -> str:
    if len(values) < 2:
        return "STABLE"
    first = statistics.mean(values[: max(1, len(values) // 2)])
    second = statistics.mean(values[max(1, len(values) // 2):])
    delta = second - first
    if delta > 5:
        return "IMPROVING"
    if delta < -5:
        return "DECLINING"
    return "STABLE"


class InterviewReadinessAgent(BaseAgent):
    """Scores interview readiness across role-critical dimensions."""

    name = "InterviewReadinessAgent"

    WEIGHTS = {
        "role_skills": 0.26,
        "dsa": 0.20,
        "coding_practice": 0.15,
        "projects": 0.12,
        "system_design": 0.12,
        "cs_fundamentals": 0.10,
        "behavioral": 0.05,
    }

    async def execute(self, ctx: AgentContext, **kwargs) -> Dict[str, Any]:
        role_skills = ScoringEngine.mastery([s.score for s in ctx.skills])
        dsa_skill = ctx.skill_by_name("DSA")
        dsa = dsa_skill.score if dsa_skill else role_skills * 0.5
        practice = ScoringEngine.mastery(
            [item["score"] for items in ctx.practice_history.values() for item in items]
            or ctx.recent_scores())
        projects = min(100.0, len(ctx.profile.projects) * 28 if ctx.profile else 0.0)
        sd_skill = ctx.skill_by_name("system design")
        system_design = sd_skill.score if sd_skill else role_skills * 0.45
        fundamentals = ScoringEngine.mastery([
            (ctx.skill_by_name(name).score if ctx.skill_by_name(name) else 0.0)
            for name in ("OS", "CN", "DBMS", "OOP", "Git")
        ])

        categories = {
            "role_skills": clamp(role_skills),
            "dsa": clamp(dsa),
            "coding_practice": clamp(practice),
            "projects": clamp(projects),
            "system_design": clamp(system_design),
            "cs_fundamentals": clamp(fundamentals),
            "behavioral": clamp(self._behavioral(ctx)),
        }
        overall = clamp(sum(categories[k] * w for k, w in self.WEIGHTS.items()))
        gaps = [g.skill for g in ctx.gaps if g.gap > 0][:6]
        blockers = self._blockers(ctx, categories, gaps)
        result = InterviewReadiness(
            overall=overall,
            category_scores=categories,
            evidence=[
                f"{len(ctx.skills)} skills carry evidence (confidence-weighted)",
                f"{len(ctx.progress)} progress records analysed",
                f"{len(ctx.practice_history)} topics practised",
                f"Target role: {ctx.target_role.name if ctx.target_role else 'unknown'}",
                f"Weakest dimension: {min(categories, key=categories.get).replace('_', ' ')}",
            ],
            gaps=gaps,
            recommendations=self._recommendations(categories, gaps, blockers),
            confidence=confidence(min(1.0, (len(ctx.skills) + len(ctx.progress)) / 22)),
            blockers=blockers,
        )
        ctx.readiness = result
        return result.model_dump()

    @staticmethod
    def _behavioral(ctx: AgentContext) -> float:
        records = len(ctx.progress)
        if not records:
            return 15.0
        consistency = sum(1 for item in ctx.progress if item.completed) / max(1, records)
        return clamp(20 + 60 * consistency)

    @staticmethod
    def _blockers(ctx: AgentContext, categories: Dict[str, float],
                  gaps: List[str]) -> List[str]:
        blockers: List[str] = []
        for key, value in sorted(categories.items(), key=lambda kv: kv[1]):
            if value < 45:
                blockers.append(
                    f"{key.replace('_', ' ')} is at {value:.0f}/100 -- below the "
                    "threshold most panels expect")
            if len(blockers) >= 3:
                break
        weak = [g for g in ctx.gaps if g.classification in {"MISSING", "WEAK"}][:2]
        for gap in weak:
            blockers.append(f"{gap.skill} has no reliable evidence yet "
                            f"({gap.classification.lower()})")
        if not blockers and gaps:
            blockers.append("Close the highest-priority gap: " + gaps[0])
        return blockers[:5]

    @staticmethod
    def _recommendations(categories: Dict[str, float], gaps: List[str],
                         blockers: List[str]) -> List[str]:
        recommendations: List[str] = []
        weakest = sorted(categories.items(), key=lambda kv: kv[1])[:2]
        for key, value in weakest:
            if value < 55:
                recommendations.append(f"Prioritise {key.replace('_', ' ')} "
                                       f"(currently {value:.0f}/100)")
        if gaps:
            recommendations.append("Close the highest-priority gap: " + gaps[0])
        recommendations.append("Complete a role-specific mock interview")
        if blockers:
            recommendations.append("Address the top blocker: " + blockers[0])
        return recommendations[:5]


class MemoryAgent(BaseAgent):
    """Maintains a compact long-term memory of the learner.

    Strengths, weaknesses, recurring mistakes, preferences and the current
    bottleneck are all derived from recorded behaviour rather than asked twice.
    """

    name = "MemoryAgent"

    async def execute(self, ctx: AgentContext, **kwargs) -> Dict[str, Any]:
        memory = dict(ctx.memory or {})
        memory.setdefault("goals", [])
        goal = ctx.profile.goal if ctx.profile else None
        if goal and goal not in memory["goals"]:
            memory["goals"].append(goal)
        memory["strengths"] = [s.name for s in ctx.skills if s.score >= 75]
        memory["weaknesses"] = [g.skill for g in ctx.gaps
                                if g.gap >= 30 and g.classification != "MASTERED"][:8]
        memory["completed_concepts"] = sorted({
            item.skill for item in ctx.progress if item.completed and item.skill
        })
        memory["preferred_difficulty"] = self._preferred_difficulty(ctx)
        memory["learning_style"] = (ctx.profile.learning_style
                                    if ctx.profile and ctx.profile.learning_style
                                    else "mixed")
        memory["recurring_mistakes"] = self._recurring_mistakes(ctx, memory)
        memory["current_bottleneck"] = self._bottleneck(ctx)
        memory["practice_by_skill"] = {
            skill: [item["score"] for item in items]
            for skill, items in ctx.practice_history.items()
        }
        memory["remediation"] = {
            key: {k: v for k, v in value.items() if k != "reason"}
            for key, value in (ctx.remediation or {}).items()
            if isinstance(value, dict)
        }
        history = list(memory.get("roadmap_history") or [])
        generated = ctx.roadmap.get("generated_at") if isinstance(ctx.roadmap, dict) else None
        if generated and (not history or history[-1] != generated):
            history.append(generated)
        memory["roadmap_history"] = history[-12:]
        memory["updated_at"] = now()
        ctx.memory = memory
        return memory

    @staticmethod
    def _recurring_mistakes(ctx: AgentContext, memory: Dict[str, Any]) -> List[str]:
        mistakes: List[str] = list(memory.get("recurring_mistakes") or [])
        for skill, items in ctx.practice_history.items():
            scores = [item["score"] for item in items]
            if len(scores) >= 2 and all(s < 50 for s in scores[-2:]):
                entry = (f"Repeated low scores on {skill} "
                         f"(last two: {scores[-2]:.0f}%, {scores[-1]:.0f}%)")
                if entry not in mistakes:
                    mistakes.append(entry)
        return mistakes[-10:]

    @staticmethod
    def _bottleneck(ctx: AgentContext) -> Optional[str]:
        remediation = ctx.remediation or {}
        for skill, value in remediation.items():
            if isinstance(value, dict) and value.get("active"):
                return f"{skill}: remediation in progress"
        for gap in ctx.gaps:
            if gap.classification == "MISSING" and gap.priority > 0:
                return f"{gap.skill}: no evidence yet, highest priority gap"
        if ctx.gaps:
            return f"{ctx.gaps[0].skill}: largest measurable gap"
        return None

    @staticmethod
    def _preferred_difficulty(ctx: AgentContext) -> str:
        stats = ctx.stats_for()
        if not stats.get("count"):
            return Difficulty.MEDIUM.value
        accuracy = stats.get("accuracy", 60)
        if accuracy < 45:
            return Difficulty.EASY.value
        if accuracy > 85:
            return Difficulty.HARD.value
        return Difficulty.MEDIUM.value


class ReportGeneratorAgent(BaseAgent):
    """Produces a structured progress report from recorded evidence."""

    name = "ReportGeneratorAgent"

    async def execute(self, ctx: AgentContext, period: str = "weekly",
                      adaptation: Optional[Dict[str, Any]] = None,
                      **kwargs) -> Dict[str, Any]:
        acquired = [s.name for s in ctx.skills if s.score >= 75]
        improving = [g.skill for g in ctx.gaps if 0 < g.gap < 30]
        weak = [g.skill for g in ctx.gaps if g.gap >= 30]
        hours = sum(item.time_spent_minutes for item in ctx.progress) / 60
        consistency = (sum(1 for item in ctx.progress if item.completed)
                       / max(1, len(ctx.progress)))
        if adaptation is None:
            adaptation = ctx.outputs.get("AdaptiveLearningAgent", {})
        roadmap_changes: List[str] = []
        if isinstance(adaptation, dict) and adaptation.get("decision"):
            roadmap_changes.append(
                f"{adaptation.get('decision')}"
                + (f" on {adaptation['skill']}" if adaptation.get("skill") else ""))
            roadmap_changes.extend(
                f"{entry.get('type', 'change')}: {entry.get('target', '')} "
                f"({entry.get('effect', '')})"
                for entry in adaptation.get("roadmap_changes", [])[:4]
            )
        if not roadmap_changes:
            roadmap_changes = ["initial roadmap generated from profile evidence"]

        report = ProgressReport(
            period=period,
            current_status="active",
            acquired_skills=acquired,
            improving_skills=improving,
            persistent_weaknesses=weak,
            remaining_gaps=[g.skill for g in ctx.gaps if g.gap > 0],
            assessment_trends={
                "records": len(ctx.assessments),
                "progress_records": len(ctx.progress),
                "learning_hours": round(hours, 2),
                "topics_practised": len(ctx.practice_history),
                "per_skill": {skill: ctx.stats_for(skill)
                              for skill in ctx.practice_history},
            },
            learning_hours=round(hours, 2),
            consistency=round(consistency, 2),
            roadmap_changes=roadmap_changes,
            next_steps=self._next_steps(ctx, weak, acquired),
            interview_readiness=clamp(ctx.readiness.overall if ctx.readiness else 0.0),
        )
        return report.model_dump()

    @staticmethod
    def _next_steps(ctx: AgentContext, weak: List[str], acquired: List[str]) -> List[str]:
        steps: List[str] = []
        if weak:
            steps.append(f"Practice {weak[0]} at reduced difficulty")
        for skill, value in (ctx.remediation or {}).items():
            if isinstance(value, dict) and value.get("active"):
                steps.append(f"Complete the remediation plan for {skill}")
                break
        if acquired:
            steps.append(f"Build a portfolio piece around {acquired[0]}")
        steps.append("Re-run the gap analysis after the next assessment")
        return steps[:5]

