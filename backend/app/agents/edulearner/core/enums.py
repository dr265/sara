"""Enumerations shared by every EduLearner AI agent."""

from __future__ import annotations

from enum import Enum


class Difficulty(str, Enum):
    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


class SkillState(str, Enum):
    MISSING = "MISSING"
    BEGINNER = "BEGINNER"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"
    EXPERT = "EXPERT"


class LearnerState(str, Enum):
    UNKNOWN = "UNKNOWN"
    NORMAL_PROGRESS = "NORMAL_PROGRESS"
    FAST_LEARNER = "FAST_LEARNER"
    STRUGGLING = "STRUGGLING"
    REPEATED_FAILURE = "REPEATED_FAILURE"
    MASTERING = "MASTERING"
    INCONSISTENT = "INCONSISTENT"
    INACTIVE = "INACTIVE"


class EvidenceSource(str, Enum):
    SELF_REPORTED = "SELF_REPORTED"
    RESUME_EVIDENCE = "RESUME_EVIDENCE"
    PROJECT_EVIDENCE = "PROJECT_EVIDENCE"
    CONVERSATION_EVIDENCE = "CONVERSATION_EVIDENCE"
    GITHUB_EVIDENCE = "GITHUB_EVIDENCE"
