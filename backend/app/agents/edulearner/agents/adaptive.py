"""The adaptive learning engine — the core differentiator of EduLearner AI.

It observes measurable learner behaviour, decides how the journey should
change, and records an auditable BEFORE -> OBSERVATION -> DECISION -> REASON
-> CHANGE trail that the UI renders as "How EduLearner adapted your journey".

All thresholds live in :data:`THRESHOLDS` and every decision is derived from
recorded scores — nothing is random and nothing is hardcoded to one learner.
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from ..core.enums import Difficulty, LearnerState
from ..core.primitives import confidence, now, rolling_stats
from ..core.taxonomy import DEPENDENCIES
from .base import AgentContext, BaseAgent

THRESHOLDS: Dict[str, float] = {
    "struggling_accuracy": 50.0,
    "repeated_failure_accuracy": 50.0,
    "repeated_failure_run": 2,
    "remediation_failures": 2,
    "fast_accuracy": 85.0,
    "fast_streak": 3,
    "mastering_accuracy": 88.0,
    "inconsistency_ratio": 0.6,
    "window": 5,
}

DIFFICULTY_ORDER: List[str] = [item.value for item in Difficulty]

_SEVERITY = {"REMEDIATION": 4, "REPEATED_FAILURE": 4, "STRUGGLING": 2,
             "INCONSISTENT": 1, "INACTIVE": 1, "MASTERING": 0,
             "FAST_LEARNER": 0, "NORMAL_PROGRESS": 0, "UNKNOWN": 0}


class AdaptiveLearningAgent(BaseAgent):
    """Reads observed performance and decides how the plan should change.

    Decides per skill (not just globally): a learner can be accelerating on
    Arrays while in remediation on Recursion. Both states are returned so the
    roadmap, planner and mentor can act on each independently.
    """

    name = "AdaptiveLearningAgent"

    async def execute(self, ctx: AgentContext, skill: Optional[str] = None,
                      **kwargs) -> Dict[str, Any]:
        skills_assessed = [skill] if skill else sorted(ctx.practice_history.keys())
        per_skill: Dict[str, Dict[str, Any]] = {
            name: self._decide_for_skill(ctx, name) for name in skills_assessed
        }
        global_state = self._decide_global(ctx)
        primary_skill = self._primary_skill(ctx, per_skill, global_state, skill)
        primary = per_skill.get(primary_skill, global_state)

        # Carry remediation state forward so the planner can act on it.
        ctx.remediation = {
            name: outcome["remediation"]
            for name, outcome in per_skill.items()
            if outcome.get("remediation", {}).get("active")
        }
        ctx.outputs[self.name] = primary
        return {
            "state": primary["state"],
            "decision": primary["decision"],
            "skill": primary.get("skill"),
            "reason": primary["reason"],
            "changes": primary["changes"],
            "roadmap_changes": primary.get("roadmap_changes", []),
            "confidence": primary["confidence"],
            "trigger": primary.get("trigger"),
            "per_skill": per_skill,
            "global_state": global_state,
            "remediation": ctx.remediation,
            "assessed_at": now(),
        }

    def _primary_skill(self, ctx: AgentContext, per_skill: Dict[str, Dict[str, Any]],
                       global_state: Dict[str, Any],
                       skill: Optional[str]) -> Optional[str]:
        if skill:
            return skill
        ranked = sorted(
            per_skill.items(),
            key=lambda kv: (_SEVERITY.get(kv[1]["state"], 0),
                            kv[1].get("trigger", {}).get("accuracy", 0)),
        )
        if ranked and _SEVERITY.get(ranked[-1][1]["state"], 0) > 0:
            return ranked[-1][0]
        top_gap = next((g.skill for g in ctx.gaps if g.gap > 0), None)
        if top_gap and top_gap in per_skill:
            return top_gap
        return next(iter(per_skill), None)

    def _decide_for_skill(self, ctx: AgentContext, skill: str) -> Dict[str, Any]:
        scores = ctx.scores_for(skill)
        stats = rolling_stats(scores, int(THRESHOLDS["window"]))
        if not scores:
            return self._outcome(skill, LearnerState.UNKNOWN, "MAINTAIN",
                                 "No practice recorded for this skill yet",
                                 [], confidence(0.25), stats)

        accuracy = stats["accuracy"]
        failures = stats["failures"]
        run = stats["consecutive_failures"]
        successes = stats["successes"]

        # 1. Repeated failure -> remediation mode (the headline behaviour).
        if run >= int(THRESHOLDS["repeated_failure_run"]) or (
                failures >= int(THRESHOLDS["remediation_failures"])
                and accuracy < THRESHOLDS["repeated_failure_accuracy"]):
            return self._remediation(ctx, skill, stats)

        # 2. Fast learner -> accelerate.
        if (accuracy >= THRESHOLDS["fast_accuracy"]
                and successes >= int(THRESHOLDS["fast_streak"])):
            return self._accelerate(ctx, skill, stats)

        # 3. Mastering -> compress the roadmap around the skill.
        if accuracy >= THRESHOLDS["mastering_accuracy"]:
            return self._outcome(
                skill, LearnerState.MASTERING, "COMPRESS_REPETITION",
                f"{skill} accuracy is {accuracy:.0f}% across {stats['count']} attempts",
                ["reduce repetition on " + skill,
                 "promote " + skill + " to an advanced objective"],
                confidence(min(0.95, 0.6 + stats["count"] * 0.06)), stats)

        # 4. Struggling but not yet repeated failure: ease without remediation.
        if accuracy < THRESHOLDS["struggling_accuracy"]:
            return self._support(ctx, skill, stats)

        return self._outcome(
            skill, LearnerState.NORMAL_PROGRESS, "MAINTAIN",
            f"{skill} accuracy is {accuracy:.0f}% (within normal range)",
            ["keep the current progression for " + skill],
            confidence(min(0.9, 0.55 + stats["count"] * 0.06)), stats)

    def _decide_global(self, ctx: AgentContext) -> Dict[str, Any]:
        records = ctx.progress
        completed = sum(1 for item in records if item.completed)
        skipped = len(records) - completed
        if records and skipped / max(1, len(records)) >= THRESHOLDS["inconsistency_ratio"] \
                and skipped >= 3:
            return self._outcome(
                None, LearnerState.INCONSISTENT, "REDUCE_LOAD",
                f"{skipped} of {len(records)} recorded tasks are incomplete",
                ["shorten today's plan", "schedule short revision sessions",
                 "front-load the highest priority task"],
                confidence(min(0.9, 0.5 + len(records) * 0.03)), None)

        recent_scores = [item["score"] for items in ctx.practice_history.values()
                         for item in items][-5:]
        if recent_scores:
            accuracy = statistics.mean(recent_scores)
            state = (LearnerState.FAST_LEARNER
                     if accuracy >= THRESHOLDS["fast_accuracy"]
                     else LearnerState.NORMAL_PROGRESS)
            return self._outcome(
                None, state, "MAINTAIN",
                f"Rolling accuracy across every skill is {accuracy:.0f}%",
                ["continue with the current plan"],
                confidence(min(0.9, 0.5 + len(recent_scores) * 0.05)), None)
        return self._outcome(None, LearnerState.UNKNOWN, "MAINTAIN",
                             "No progress signals recorded yet",
                             ["continue with the baseline plan"], confidence(0.3), None)

    # -- decisions --------------------------------------------------------------
    def _remediation(self, ctx: AgentContext, skill: str,
                     stats: Dict[str, Any]) -> Dict[str, Any]:
        prerequisites = DEPENDENCIES.get(skill, [])
        easier = self._easier(ctx, skill)
        changes = [
            (f"revisit prerequisites first: {', '.join(prerequisites)}"
             if prerequisites else "review the skill's fundamentals first"),
            f"reduce {skill} difficulty to {easier.value}",
            f"add 3 scaffolded practice tasks on {skill}",
            "schedule a reassessment once accuracy recovers",
        ]
        if "Dynamic programming" in ctx.practice_history or skill == "DSA":
            changes.append("delay advanced dynamic programming until recursion recovers")
        return {
            "state": LearnerState.REPEATED_FAILURE.value,
            "decision": "REMEDIATION_MODE",
            "skill": skill,
            "reason": [
                f"{skill} accuracy is {stats['accuracy']:.0f}% "
                f"across the last {stats['count']} attempts",
                f"{stats['consecutive_failures']} consecutive results below "
                f"{THRESHOLDS['repeated_failure_accuracy']:.0f}%",
            ],
            "changes": changes,
            "roadmap_changes": self._roadmap_changes(ctx, skill, "remediate"),
            "confidence": confidence(min(0.97, 0.72 + stats["count"] * 0.05)),
            "trigger": {
                "skill": skill,
                "accuracy": stats["accuracy"],
                "consecutive_failures": stats["consecutive_failures"],
                "attempts": stats["count"],
                "threshold_accuracy": THRESHOLDS["repeated_failure_accuracy"],
                "observed_at": now(),
            },
            "remediation": {
                "active": True,
                "skill": skill,
                "difficulty": easier.value,
                "prerequisites": prerequisites,
                "started_at": now(),
                "reason": (f"accuracy {stats['accuracy']:.0f}% with "
                           f"{stats['consecutive_failures']} consecutive failures"),
            },
        }

    def _support(self, ctx: AgentContext, skill: str,
                 stats: Dict[str, Any]) -> Dict[str, Any]:
        """One weak result (or a few non-consecutive ones): ease the path."""

        easier = self._easier(ctx, skill)
        prerequisites = DEPENDENCIES.get(skill, [])
        changes = [
            f"reduce {skill} difficulty to {easier.value}",
            "add a guided worked example before the next problem",
        ]
        if prerequisites:
            changes.append(f"add a prerequisite refresher on {prerequisites[0]}")
        return {
            "state": LearnerState.STRUGGLING.value,
            "decision": "DECREASE_DIFFICULTY",
            "skill": skill,
            "reason": [
                f"{skill} accuracy is {stats['accuracy']:.0f}% "
                f"across {stats['count']} attempt(s)",
                f"Below the {THRESHOLDS['struggling_accuracy']:.0f}% threshold; "
                "two consecutive failures would trigger remediation",
            ],
            "changes": changes,
            "roadmap_changes": [
                {"type": "modify", "target": skill,
                 "effect": f"difficulty reduced to {easier.value}",
                 "reason": "current difficulty is producing low accuracy"},
                {"type": "add", "target": f"{skill} worked example",
                 "effect": "guided practice inserted",
                 "reason": "learners who see a worked example first recover faster"},
            ],
            "confidence": confidence(min(0.9, 0.5 + stats["count"] * 0.07)),
            "trigger": {
                "skill": skill,
                "accuracy": stats["accuracy"],
                "consecutive_failures": stats["consecutive_failures"],
                "attempts": stats["count"],
                "threshold_accuracy": THRESHOLDS["struggling_accuracy"],
                "observed_at": now(),
            },
            "remediation": {"active": False, "skill": skill},
        }
    def _accelerate(self, ctx: AgentContext, skill: str,
                    stats: Dict[str, Any]) -> Dict[str, Any]:
        harder = self._harder(ctx, skill)
        return {
            "state": LearnerState.FAST_LEARNER.value,
            "decision": "INCREASE_DIFFICULTY",
            "skill": skill,
            "reason": [
                f"{skill} accuracy is {stats['accuracy']:.0f}%",
                f"{stats['successes']} consecutive strong results",
            ],
            "changes": [
                f"raise {skill} difficulty to {harder.value}",
                "remove redundant repetition on " + skill,
                "accelerate the roadmap around " + skill,
                "introduce an advanced project on " + skill,
            ],
            "roadmap_changes": self._roadmap_changes(ctx, skill, "accelerate"),
            "confidence": confidence(min(0.95, 0.7 + stats["count"] * 0.05)),
            "trigger": {
                "skill": skill,
                "accuracy": stats["accuracy"],
                "consecutive_successes": stats["successes"],
                "attempts": stats["count"],
                "threshold_accuracy": THRESHOLDS["fast_accuracy"],
                "observed_at": now(),
            },
            "remediation": {"active": False, "skill": skill},
        }

    def _outcome(self, skill: Optional[str], state: LearnerState, decision: str,
                 reason: str, changes: List[str], conf: float,
                 stats: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "state": state.value,
            "decision": decision,
            "skill": skill,
            "reason": [reason],
            "changes": changes,
            "roadmap_changes": [],
            "confidence": conf,
            "trigger": ({
                "skill": skill,
                "accuracy": stats.get("accuracy", 0),
                "attempts": stats.get("count", 0),
                "consecutive_failures": stats.get("consecutive_failures", 0),
                "observed_at": now(),
            } if stats else None),
            "remediation": {"active": False, "skill": skill} if skill else None,
        }

    def _roadmap_changes(self, ctx: AgentContext, skill: str,
                         mode: str) -> List[Dict[str, Any]]:
        """Concrete, explainable roadmap deltas the front-end can render."""

        if mode == "remediate":
            prerequisites = DEPENDENCIES.get(skill, [])
            entries = [
                {"type": "add", "target": f"prerequisite revision: {prerequisite}",
                 "effect": "inserted before " + skill,
                 "reason": f"{prerequisite} underpins {skill}"}
                for prerequisite in prerequisites[:2]
            ]
            entries.append({
                "type": "modify", "target": skill,
                "effect": "difficulty reduced",
                "reason": "repeated low scores on the current difficulty",
            })
            entries.append({
                "type": "add", "target": f"{skill} practice set",
                "effect": "3 scaffolded tasks inserted",
                "reason": "increase successful repetitions before progression",
            })
            return entries
        return [
            {"type": "modify", "target": skill,
             "effect": "difficulty increased",
             "reason": "three consecutive strong results"},
            {"type": "remove", "target": f"{skill} repetition",
             "effect": "redundant tasks removed",
             "reason": "skill is already at target accuracy"},
        ]

    # -- difficulty helpers -----------------------------------------------------
    def _easier(self, ctx: AgentContext, skill: str) -> Difficulty:
        current = self._current_difficulty(ctx, skill)
        index = max(0, DIFFICULTY_ORDER.index(current.value) - 1)
        return Difficulty(DIFFICULTY_ORDER[index])

    def _harder(self, ctx: AgentContext, skill: str) -> Difficulty:
        current = self._current_difficulty(ctx, skill)
        index = min(len(DIFFICULTY_ORDER) - 1, DIFFICULTY_ORDER.index(current.value) + 1)
        return Difficulty(DIFFICULTY_ORDER[index])

    @staticmethod
    def _current_difficulty(ctx: AgentContext, skill: str) -> Difficulty:
        history = ctx.practice_history.get(skill.strip(), [])
        for item in reversed(history):
            value = (item.get("difficulty") or "").strip()
            if value in DIFFICULTY_ORDER:
                return Difficulty(value)
        gap = ctx.gap_by_name(skill)
        if gap is not None:
            if gap.current_score >= 75:
                return Difficulty.HARD
            if gap.current_score >= 45:
                return Difficulty.MEDIUM
            return Difficulty.EASY
        return Difficulty.MEDIUM

    def next_actions_for(self, ctx: AgentContext, data: Any) -> List[str]:
        if isinstance(data, dict):
            return list(data.get("changes", []))[:3]
        return []


