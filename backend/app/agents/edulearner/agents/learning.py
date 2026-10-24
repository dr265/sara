"""Roadmap, planning and practice agents."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.enums import Difficulty
from ..core.models import (
    DailyTask,
    LearningObjective,
    PracticeQuestion,
    RoadmapPhase,
    RoadmapWeek,
    SkillGap,
)
from ..core.primitives import clamp, now
from ..core.taxonomy import DEPENDENCIES, objectives_for
from .base import AgentContext, BaseAgent

SESSION_SEQUENCE = ["concept", "guided practice", "independent practice", "assessment"]


def _matches_remediation(skill: str, remediation_skills: List[str]) -> bool:
    """A gap skill counts as remediated when any of its observed topics are."""

    key = (skill or "").strip().lower()
    if not key:
        return False
    related = {
        "recursion": ["DSA"], "dynamic programming": ["DSA"],
        "arrays": ["DSA"], "strings": ["DSA"],
        "sql": ["SQL", "DBMS"], "oop": ["OOP"],
    }
    for other in remediation_skills:
        other_key = (other or "").strip().lower()
        if not other_key:
            continue
        if key == other_key or key in other_key or other_key in key:
            return True
        if key in [item.lower() for item in related.get(other_key, [])]:
            return True
    return False


class RoadmapGeneratorAgent(BaseAgent):
    """Turns prioritised gaps into phases, weeks and daily tasks.

    Weeks are numbered globally (1..N) and phases are sized by the learner's
    weekly capacity so the plan is achievable in the time they actually have.
    """

    name = "RoadmapGeneratorAgent"

    async def execute(self, ctx: AgentContext, weeks: Optional[int] = None,
                      **kwargs) -> Dict[str, Any]:
        active = [g for g in ctx.gaps if g.gap > 0]
        if not active:
            ctx.roadmap = {
                "generated_at": now(),
                "reason": "No measurable gaps remain for the target role",
                "phases": [], "total_weeks": 0,
            }
            return ctx.roadmap

        sessions_per_week = self._sessions_per_week(ctx)
        gaps_per_phase = max(1, min(3, sessions_per_week // 2))
        phases: List[RoadmapPhase] = []
        week_counter = 0

        horizon = weeks if (weeks is not None and weeks > 0) else len(active)
        horizon = max(1, min(horizon, len(active)))
        deferred = [gap.skill for gap in active[horizon:]]

        for index in range(0, horizon, gaps_per_phase):
            chunk = active[index:min(index + gaps_per_phase, horizon)]
            phase_weeks: List[RoadmapWeek] = []
            for gap in chunk:
                week_counter += 1
                phase_weeks.append(self._build_week(week_counter, gap, ctx))
            phases.append(RoadmapPhase(
                phase=len(phases) + 1,
                title=f"Phase {len(phases) + 1}: {chunk[0].skill} and related gaps",
                weeks=phase_weeks,
                expected_outcome=("Measured improvement in "
                                  + ", ".join(g.skill for g in chunk)),
            ))

        reason = "Prioritised by gap size, role weight, interview value and prerequisites"
        if deferred:
            reason += (f"; {len(deferred)} lower-priority gap(s) deferred beyond the "
                       f"{horizon}-week horizon: {', '.join(deferred)}")
        ctx.roadmap = {
            "generated_at": now(),
            "reason": reason,
            "phases": [p.model_dump() for p in phases],
            "total_weeks": week_counter,
            "total_objectives": sum(len(w.objectives) for p in phases for w in p.weeks),
            "requested_weeks": weeks,
            "deferred_gaps": deferred,
        }
        return ctx.roadmap

    def _sessions_per_week(self, ctx: AgentContext) -> int:
        try:
            hours = float(ctx.profile.available_hours) if ctx.profile else 7
        except (TypeError, ValueError):
            hours = 7
        return max(2, min(14, int(hours)))

    def _build_week(self, week: int, gap: SkillGap, ctx: AgentContext) -> RoadmapWeek:
        prereqs = DEPENDENCIES.get(gap.skill, [])
        remediation = (ctx.remediation or {}).get(gap.skill) if ctx.remediation else None
        in_remediation = bool(remediation and remediation.get("active"))
        difficulty = (Difficulty.EASY if in_remediation or gap.current_score < 40
                      else (Difficulty.MEDIUM if gap.current_score < 70
                            else Difficulty.HARD))

        topics = objectives_for(gap.skill, 5)
        if in_remediation:
            prereq_topics = [f"{prerequisite} refresher" for prerequisite in prereqs[:2]]
            topics = prereq_topics + topics[:3]

        tasks: List[DailyTask] = []
        objectives: List[LearningObjective] = []
        for index, topic in enumerate(topics):
            kind = SESSION_SEQUENCE[index % len(SESSION_SEQUENCE)]
            minutes = max(25, int(60 * gap.estimated_effort_hours / 4))
            tasks.append(DailyTask(
                title=f"{gap.skill}: {topic} ({kind})",
                skill=gap.skill,
                objective=f"Move {gap.skill} toward {gap.target_level.value}",
                task_type=kind,
                difficulty=difficulty,
                estimated_minutes=minutes,
                rationale="; ".join(gap.why) or "Highest priority gap for the target role",
            ))
            objectives.append(LearningObjective(
                title=f"{gap.skill}: {topic}",
                skill=gap.skill,
                description=(f"Practice {topic} through evidence-backed work; target "
                             f"{gap.target_level.value} ({gap.target_score:.0f})"),
                expected_proficiency=gap.target_level,
                estimated_minutes=minutes,
                prerequisites=prereqs if index == 0 else [],
            ))
        return RoadmapWeek(
            week=week,
            theme=f"{gap.skill} - {topics[0]}" if topics else gap.skill,
            objectives=objectives,
            tasks=tasks,
            assessment=f"Assess {gap.skill} after the {len(topics)} sessions",
            project=f"Apply {gap.skill} in a role-relevant mini project",
        )


class PlannerAgent(BaseAgent):
    """Allocates today's (or this week's) available time across top gaps."""

    name = "PlannerAgent"

    async def execute(self, ctx: AgentContext, period: str = "today",
                      **kwargs) -> Dict[str, Any]:
        days = 7 if period == "week" else 1
        try:
            hours = float(ctx.profile.available_hours) if ctx.profile else 7
        except (TypeError, ValueError):
            hours = 7
        # ``available_hours`` is weekly capacity: a day gets a seventh of it.
        daily_hours = hours / 7.0 if period == "today" else hours
        budget = max(15, int(daily_hours * 60 * days))
        tasks: List[DailyTask] = []
        remediation_skills = {key for key, value in (ctx.remediation or {}).items()
                              if isinstance(value, dict) and value.get("active")}

        for gap in ctx.gaps:
            if budget <= 0 or len(tasks) >= max(1, days * 4):
                break
            minutes = self._task_minutes(gap, budget, days)
            in_remediation = gap.skill in remediation_skills or _matches_remediation(
                gap.skill, list(remediation_skills))
            tasks.append(DailyTask(
                title=(f"{gap.skill} remediation: foundations" if in_remediation
                       else f"{gap.skill} focused practice"),
                skill=gap.skill,
                objective=f"Close the highest priority gap (priority {gap.priority:.1f})",
                task_type="practice",
                difficulty=(Difficulty.EASY if in_remediation or gap.current_score < 40
                            else (Difficulty.MEDIUM if gap.current_score < 70
                                  else Difficulty.HARD)),
                estimated_minutes=minutes,
                rationale=("Remediation active: accuracy triggered prerequisite revision "
                           "and a lower difficulty." if in_remediation else
                           "Priority combines gap size, role importance, interview value, "
                           "dependencies and estimated effort."),
            ))
            budget -= minutes
            if in_remediation and budget > 0:
                for extra in (
                    DailyTask(
                        title=f"{gap.skill}: revision of weak concepts",
                        skill=gap.skill,
                        objective="Rebuild the foundation that the current topic depends on",
                        task_type="revision", difficulty=Difficulty.EASY,
                        estimated_minutes=max(15, min(budget, 25)),
                        rationale="Revision added because recent scores indicated difficulty.",
                    ),
                    DailyTask(
                        title=f"{gap.skill}: reassessment",
                        skill=gap.skill,
                        objective="Confirm the remediation worked before progressing",
                        task_type="assessment", difficulty=Difficulty.EASY,
                        estimated_minutes=max(10, min(budget, 20)),
                        rationale="A reassessment is scheduled to clear the remediation flag.",
                    ),
                ):
                    if len(tasks) >= max(1, days * 4) or budget <= 0:
                        break
                    tasks.append(extra)
                    budget -= extra.estimated_minutes

        ctx.plan = tasks
        return {
            "period": period,
            "tasks": [task.model_dump() for task in tasks],
            "available_minutes": max(0, budget),
            "scheduled_minutes": sum(task.estimated_minutes for task in tasks),
        }

    @staticmethod
    def _task_minutes(gap: SkillGap, budget: int, days: int) -> int:
        raw = 30 + (gap.gap * 0.6)
        ceiling = 90 * max(1, days)
        return int(clamp(raw, 20, min(budget, ceiling, 90 * days)))


class PracticeGeneratorAgent(BaseAgent):
    """Generates real, marked practice questions for a skill or top gap.

    Questions come from the curated item bank (deterministic MCQs with a known
    correct answer) or, for skills without curated content, from generated
    conceptual tasks derived from that skill's learning objectives. Difficulty
    follows the adaptive state: remediation lowers it, mastery raises it.
    """

    name = "PracticeGeneratorAgent"

    async def execute(self, ctx: AgentContext, skill: Optional[str] = None,
                      difficulty: Optional[str] = None, count: int = 5,
                      **kwargs) -> List[Dict[str, Any]]:
        from ..questions import build as build_items

        gap = next((g for g in ctx.gaps
                    if not skill or g.skill.lower() == skill.lower()), None)
        topic = gap.skill if gap else (skill or (ctx.gaps[0].skill if ctx.gaps else "DSA"))
        difficulty_enum = self._difficulty(value=difficulty, gap=gap, ctx=ctx, skill=topic)
        total = max(1, min(20, int(count)))
        seed = ctx.user_id or topic
        items = build_items(topic, total, difficulty_enum.value, seed=seed)

        remediation = ctx.remediation.get(topic) if ctx.remediation else None
        if remediation and remediation.get("active"):
            mixed: List[Dict[str, Any]] = []
            remaining = total
            for prerequisite in (remediation.get("prerequisites") or [])[:2]:
                if remaining <= 0:
                    break
                take = max(1, remaining // 2)
                mixed.extend(build_items(prerequisite, take, "EASY", seed=seed))
                remaining -= take
            mixed.extend(build_items(topic, max(1, remaining),
                                     difficulty_enum.value, seed=seed))
            items = mixed or items
        return [self._to_question(item).model_dump() for item in items]

    @staticmethod
    def _to_question(item: Dict[str, Any]) -> PracticeQuestion:
        return PracticeQuestion(
            skill=item["skill"],
            topic=item.get("topic", item["skill"]),
            objective=item.get("objective", ""),
            question=item["question"],
            difficulty=Difficulty(item.get("difficulty", "MEDIUM")),
            estimated_minutes=int(item.get("estimated_minutes", 10)),
            hints=item.get("hints", []),
            evaluation_criteria=item.get("evaluation_criteria", []),
            explanation=item.get("explanation", ""),
            question_type=item.get("question_type", "mcq"),
            options=item.get("options", []),
            correct_index=item.get("correct_index"),
            answer=item.get("answer"),
            misconceptions=item.get("misconceptions", []),
        )

    @staticmethod
    def _difficulty(value: Optional[str], gap, ctx: AgentContext, skill: str) -> Difficulty:
        allowed = {item.value for item in Difficulty}
        if value in allowed:
            return Difficulty(value)
        remediation = (ctx.remediation or {}).get(skill or "")
        if remediation and remediation.get("active"):
            return Difficulty(remediation.get("difficulty") or "EASY")
        if gap is not None and gap.current_score >= 75:
            return Difficulty.HARD
        if gap is not None and gap.current_score >= 45:
            return Difficulty.MEDIUM
        stats = ctx.stats_for(skill) if skill else {}
        if stats.get("accuracy", 100) >= 85:
            return Difficulty.HARD
        if stats.get("accuracy", 100) < 45:
            return Difficulty.EASY
        return Difficulty.MEDIUM

