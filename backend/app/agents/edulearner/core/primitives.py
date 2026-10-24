"""Shared primitives: timestamps, clamping, rolling statistics, confidence."""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Dict, Iterable


def now() -> str:
    """UTC ISO-8601 timestamp used for every recorded event."""

    return datetime.now(timezone.utc).isoformat()


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def confidence(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 3)


def level_from_score(score: float) -> str:
    score = clamp(score)
    if score >= 85:
        return "EXPERT"
    if score >= 70:
        return "ADVANCED"
    if score >= 45:
        return "INTERMEDIATE"
    if score >= 20:
        return "BEGINNER"
    return "MISSING"


def rolling_stats(scores: Iterable[float], window: int = 5) -> Dict[str, Any]:
    """Deterministic rolling statistics over the most recent scores."""

    values = [float(s) for s in scores][-max(1, int(window)):]
    if not values:
        return {"count": 0, "accuracy": 0.0, "failures": 0,
                "successes": 0, "consecutive_failures": 0}
    failures = sum(1 for v in values if v < 50)
    successes = sum(1 for v in values if v >= 80)
    run = 0
    for v in reversed(values):
        if v < 50:
            run += 1
        else:
            break
    return {
        "count": len(values),
        "accuracy": round(statistics.mean(values), 2),
        "failures": failures,
        "successes": successes,
        "consecutive_failures": run,
    }
