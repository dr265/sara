"""Pydantic models: input/output schemas for the EduLearner AI engine."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .enums import Difficulty, EvidenceSource, SkillState
from .primitives import now


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class UserProfile(BaseModel):
    user_id: str
    name: str = ""
    education: str = ""
    experience: str = ""
    resume_text: str = ""
    skills: List[str] = Field(default_factory=list)
    projects: List[Dict[str, Any]] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)
    goal: str = ""
    target_role: str = ""
    learning_style: str = "mixed"
    available_hours: float = 7.0


class RoleProfile(BaseModel):
    name: str
    required_skills: List[str] = Field(default_factory=list)
    preferred_skills: List[str] = Field(default_factory=list)
    target_proficiency: Dict[str, float] = Field(default_factory=dict)
    weights: Dict[str, float] = Field(default_factory=dict)
    interview_importance: Dict[str, float] = Field(default_factory=dict)
    dependencies: Dict[str, List[str]] = Field(default_factory=dict)


class SkillEvidence(BaseModel):
    source: EvidenceSource = EvidenceSource.SELF_REPORTED
    skill: str
    description: str = ""
    confidence: float = 0.5
    score: float = 0.0
    at: str = Field(default_factory=now)


class Skill(BaseModel):
    name: str
    score: float = 0.0
    level: SkillState = SkillState.MISSING
    confidence: float = 0.0
    evidence: List[SkillEvidence] = Field(default_factory=list)
    updated_at: str = Field(default_factory=now)


class SkillGap(BaseModel):
    skill: str
    current_level: SkillState = SkillState.MISSING
    target_level: SkillState = SkillState.INTERMEDIATE
    current_score: float = 0.0
    target_score: float = 80.0
    gap: float = 0.0
    importance: float = 1.0
    career_relevance: float = 1.0
    interview_relevance: float = 0.8
    dependency_factor: float = 1.0
    estimated_effort_hours: float = 2.0
    priority: float = 0.0
    classification: str = "MISSING"
    confidence: float = 0.3
    why: List[str] = Field(default_factory=list)


class DailyTask(BaseModel):
    task_id: str = Field(default_factory=lambda: _new_id("task"))
    title: str
    skill: str = ""
    objective: str = ""
    task_type: str = "practice"
    difficulty: Difficulty = Difficulty.MEDIUM
    estimated_minutes: int = 30
    rationale: str = ""
    status: str = "pending"
    created_at: str = Field(default_factory=now)


class LearningObjective(BaseModel):
    title: str
    skill: str
    description: str = ""
    expected_proficiency: SkillState = SkillState.INTERMEDIATE
    estimated_minutes: int = 60
    prerequisites: List[str] = Field(default_factory=list)


class RoadmapWeek(BaseModel):
    week: int
    theme: str = ""
    objectives: List[LearningObjective] = Field(default_factory=list)
    tasks: List[DailyTask] = Field(default_factory=list)
    assessment: str = ""
    project: str = ""


class RoadmapPhase(BaseModel):
    phase: int
    title: str
    weeks: List[RoadmapWeek] = Field(default_factory=list)
    expected_outcome: str = ""


class ProgressRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: _new_id("prog"))
    task_id: Optional[str] = None
    skill: str = ""
    score: Optional[float] = None
    completed: bool = False
    time_spent_minutes: float = 0.0
    attempts: int = 1
    note: str = ""
    created_at: str = Field(default_factory=now)


class AssessmentResult(BaseModel):
    result_id: str = Field(default_factory=lambda: _new_id("assess"))
    assessment_id: str = ""
    score: float = 0.0
    max_score: float = 100.0
    details: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)


class PracticeQuestion(BaseModel):
    practice_id: str = Field(default_factory=lambda: _new_id("pq"))
    skill: str
    topic: str = ""
    objective: str = ""
    question: str
    difficulty: Difficulty = Difficulty.MEDIUM
    estimated_minutes: int = 10
    hints: List[str] = Field(default_factory=list)
    evaluation_criteria: List[str] = Field(default_factory=list)
    explanation: str = ""
    question_type: str = "mcq"
    options: List[str] = Field(default_factory=list)
    correct_index: Optional[int] = None
    answer: Optional[str] = None
    misconceptions: List[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    answer: str
    actions: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)
    confidence: float = 0.6
    agent: str = "ConversationalMentorAgent"


class InterviewReadiness(BaseModel):
    overall: float = 0.0
    category_scores: Dict[str, float] = Field(default_factory=dict)
    evidence: List[str] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)
    confidence: float = 0.0


class ProgressReport(BaseModel):
    period: str = "weekly"
    current_status: str = "active"
    acquired_skills: List[str] = Field(default_factory=list)
    improving_skills: List[str] = Field(default_factory=list)
    persistent_weaknesses: List[str] = Field(default_factory=list)
    remaining_gaps: List[str] = Field(default_factory=list)
    assessment_trends: Dict[str, Any] = Field(default_factory=dict)
    learning_hours: float = 0.0
    consistency: float = 0.0
    roadmap_changes: List[str] = Field(default_factory=list)
    next_steps: List[str] = Field(default_factory=list)
    interview_readiness: float = 0.0
