"""Profile analysis, career role analysis, gap analysis and prerequisites."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from ..core.enums import EvidenceSource, SkillState
from ..core.models import RoleProfile, Skill, SkillEvidence, SkillGap, UserProfile
from ..core.primitives import clamp, confidence, level_from_score, now
from ..core.scoring import ScoringEngine
from ..core.taxonomy import (
    DEFAULT_TARGET_PROFICIENCY,
    DEPENDENCIES,
    INTERVIEW_CRITICAL,
    ROLE_SKILLS,
    all_skills,
    find_skills,
    role_for,
)
from .base import AgentContext, BaseAgent


class ProfileAnalyzerAgent(BaseAgent):
    """Builds the skill inventory from the profile, resume and project evidence."""

    name = "ProfileAnalyzerAgent"

    async def execute(self, ctx: AgentContext,
                      profile: Optional[UserProfile] = None, **kwargs) -> Dict[str, Any]:
        p = profile or ctx.profile
        resume_tech = find_skills(p.resume_text or "", all_skills())
        names: List[str] = list(dict.fromkeys(list(p.skills) + list(resume_tech)))
        evidence: List[SkillEvidence] = []

        for name in names:
            self_reported = name in p.skills
            evidence.append(SkillEvidence(
                source=(EvidenceSource.SELF_REPORTED if self_reported
                        else EvidenceSource.RESUME_EVIDENCE),
                skill=name,
                description="Listed by learner" if self_reported else "Found in resume",
                confidence=0.45 if self_reported else 0.6,
                score=45 if self_reported else 55,
            ))
        project_blob = json.dumps(p.projects, default=str).lower() if p.projects else ""
        for name in names:
            if name.lower() in project_blob:
                evidence.append(SkillEvidence(
                    source=EvidenceSource.PROJECT_EVIDENCE, skill=name,
                    description="Applied in a learner project",
                    confidence=0.75, score=65,
                ))
        for name in (p.certifications or []):
            evidence.append(SkillEvidence(
                source=EvidenceSource.RESUME_EVIDENCE, skill=name,
                description="Supported by a certification", confidence=0.7, score=70,
            ))

        ctx.evidence.extend(evidence)
        grouped: Dict[str, List[SkillEvidence]] = defaultdict(list)
        for item in evidence:
            grouped[item.skill].append(item)

        skills: List[Skill] = []
        for name, items in grouped.items():
            score, conf = ScoringEngine.evidence_score(items)
            existing = ctx.skill_by_name(name)
            if existing is not None:
                existing.score, existing.confidence = score, conf
                existing.evidence, existing.updated_at = items, now()
                skills.append(existing)
            else:
                skills.append(Skill(
                    name=name, score=score,
                    level=SkillState(level_from_score(score)),
                    confidence=conf, evidence=items,
                ))
        ctx.skills = skills
        return {
            "skills": [s.model_dump() for s in skills],
            "overall_level": SkillState(level_from_score(
                ScoringEngine.mastery([s.score for s in skills]))).value,
            "overall_score": ScoringEngine.mastery([s.score for s in skills]),
            "evidence_count": len(evidence),
        }

    def confidence_for(self, ctx: AgentContext, data: Any) -> float:
        return confidence(min(1.0, 0.45 + len(ctx.evidence) / 30))


class CareerRoleAnalyzerAgent(BaseAgent):
    """Resolves the target role into a weighted competency model."""

    name = "CareerRoleAnalyzerAgent"

    async def execute(self, ctx: AgentContext, role: str = "", **kwargs) -> Dict[str, Any]:
        name = role_for(role or ctx.profile.target_role or ctx.profile.goal
                        or "Software Engineer")
        required = list(ROLE_SKILLS.get(name, ROLE_SKILLS["Software Engineer"]))
        critical = set(INTERVIEW_CRITICAL.get(name, []))
        model = RoleProfile(
            name=name,
            required_skills=required,
            preferred_skills=[],
            target_proficiency={s: DEFAULT_TARGET_PROFICIENCY for s in required},
            weights={s: (1.5 if s in critical else 1.0) for s in required},
            interview_importance={s: (0.95 if s in critical else 0.75) for s in required},
            dependencies={s: DEPENDENCIES.get(s, []) for s in required},
        )
        ctx.target_role = model
        return model.model_dump()

    def confidence_for(self, ctx: AgentContext, data: Any) -> float:
        known = isinstance(data, dict) and data.get("name") in ROLE_SKILLS
        return confidence(0.85 if known else 0.55)


class SkillGapAnalyzerAgent(BaseAgent):
    """Measures the distance between current skills and the target role."""

    name = "SkillGapAnalyzerAgent"

    async def execute(self, ctx: AgentContext, **kwargs) -> List[Dict[str, Any]]:
        if not ctx.target_role:
            await CareerRoleAnalyzerAgent().execute(ctx)
        current = {s.name.lower(): s for s in ctx.skills}
        gaps: List[SkillGap] = []
        for name in ctx.target_role.required_skills:
            found = current.get(name.lower())
            score = clamp(found.score) if found else 0.0
            target = float(ctx.target_role.target_proficiency.get(
                name, DEFAULT_TARGET_PROFICIENCY))
            gap = clamp(target - score)
            effort = max(2.0, round(gap / 5))
            deps = DEPENDENCIES.get(name, [])
            dep_factor = 1 + 0.1 * sum(
                1 for dep in deps
                if (current.get(dep.lower()) is None
                    or current[dep.lower()].score < 50)
            )
            importance = ctx.target_role.weights.get(name, 1.0)
            priority = ScoringEngine.priority(
                gap, importance, 1.0,
                ctx.target_role.interview_importance.get(name, 0.8),
                dep_factor, effort,
            )
            gaps.append(SkillGap(
                skill=name,
                current_level=(found.level if found else SkillState.MISSING),
                target_level=SkillState(level_from_score(target)),
                current_score=score, target_score=target, gap=gap,
                importance=importance, career_relevance=1.0,
                interview_relevance=ctx.target_role.interview_importance.get(name, 0.8),
                dependency_factor=dep_factor, estimated_effort_hours=effort,
                priority=priority,
                classification=self._classify(found, score, target, gap),
                confidence=found.confidence if found else 0.3,
                why=self._why(found, gap, target, deps),
            ))
        gaps.sort(key=lambda g: g.priority, reverse=True)
        ctx.gaps = gaps
        return [g.model_dump() for g in gaps]

    @staticmethod
    def _classify(found, score: float, target: float, gap: float) -> str:
        if found and score >= target:
            return "MASTERED"
        if not found or score <= 0:
            return "MISSING"
        if gap >= 40:
            return "WEAK"
        if gap > 0:
            return "DEVELOPING"
        return "STRONG"

    @staticmethod
    def _why(found, gap: float, target: float, deps: List[str]) -> List[str]:
        why: List[str] = []
        if not found:
            why.append("No reliable evidence exists for this skill yet")
        if gap > 0:
            why.append(f"{gap:.0f} points below the {target:.0f} target proficiency")
        if deps:
            why.append("Prerequisites to strengthen first: " + ", ".join(deps))
        return why

    def confidence_for(self, ctx: AgentContext, data: Any) -> float:
        return confidence(min(1.0, 0.5 + len(ctx.skills) / 25))


class PrerequisiteAgent(BaseAgent):
    """Computes a dependency-respecting learning order for the target role."""

    name = "PrerequisiteAgent"

    async def execute(self, ctx: AgentContext, **kwargs) -> Dict[str, Any]:
        required = list(ctx.target_role.required_skills) if ctx.target_role else []
        return {
            "dependencies": {s: DEPENDENCIES.get(s, []) for s in required},
            "order": self.order(ctx),
        }

    def order(self, ctx: AgentContext) -> List[str]:
        required: Set[str] = set(ctx.target_role.required_skills if ctx.target_role else [])
        result: List[str] = []
        visiting: Set[str] = set()

        def visit(node: str) -> None:
            if node in result or node in visiting:
                return
            visiting.add(node)
            for dep in DEPENDENCIES.get(node, []):
                if dep in required:
                    visit(dep)
            visiting.discard(node)
            if node not in result:
                result.append(node)

        for item in required:
            visit(item)
        for item in required:
            if item not in result:
                result.append(item)
        return result

