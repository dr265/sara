"""Deterministic scoring engine shared by the EduLearner agents.

Every score the platform shows is derived here from recorded evidence; nothing
is random and nothing is hardcoded to one learner.
"""

from __future__ import annotations

import statistics
from typing import Iterable, List, Tuple

from .primitives import clamp


class ScoringEngine:
    """Evidence aggregation, mastery estimation and gap prioritisation."""

    @staticmethod
    def evidence_score(items: Iterable) -> Tuple[float, float]:
        """Confidence-weighted mean of evidence scores -> (score, confidence).

        More independent pieces of evidence raise confidence; conflicting
        evidence (high variance) lowers it.
        """

        items = list(items)
        if not items:
            return 0.0, 0.0
        total_weight = 0.0
        weighted = 0.0
        scores: List[float] = []
        for item in items:
            conf = clamp(getattr(item, "confidence", 0.5), 0.05, 1.0)
            score = clamp(getattr(item, "score", 0.0))
            weighted += score * conf
            total_weight += conf
            scores.append(score)
        score = clamp(weighted / max(total_weight, 1e-9))
        spread = statistics.pstdev(scores) if len(scores) > 1 else 0.0
        base = min(0.95, 0.35 + 0.12 * len(items))
        conf = clamp(base - spread / 200.0, 0.1, 0.95)
        return round(score, 2), round(conf, 3)

    @staticmethod
    def mastery(scores: Iterable[float]) -> float:
        values = [clamp(s) for s in scores if s is not None]
        return round(statistics.mean(values), 2) if values else 0.0

    @staticmethod
    def priority(gap: float, importance: float, career_relevance: float,
                 interview_relevance: float, dependency_factor: float,
                 effort_hours: float) -> float:
        """Gap priority: bigger gaps on interview-critical, low-effort skills win."""

        raw = (
            gap * 0.55
            + importance * 12.0
            + career_relevance * 8.0
            + interview_relevance * 14.0
            + (dependency_factor - 1.0) * 20.0
            - min(effort_hours, 20.0) * 0.6
        )
        return round(max(0.0, raw), 2)
