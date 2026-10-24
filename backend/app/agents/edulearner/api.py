"""FastAPI router for EduLearner AI — mounted at /api/edulearner on the
unified SARA backend (app.main), next to the other Nexus Hub agent engines.

Every route: validates input (pydantic), enforces the shared SARA API key
(mutation routes), runs the orchestrator agents, persists state, and returns a
uniform ``{success, data, message}`` envelope. Errors never leak a stack
trace; they surface as honest ``success: false`` responses.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.database import models as db_models
from app.database.database import get_db

from .core.models import ProgressRecord, UserProfile
from .orchestrator import get_orchestrator

logger = logging.getLogger("sara.edulearner")

router = APIRouter(prefix="/api/edulearner", tags=["edulearner-ai"])


def ok(data: Any, message: str = "") -> Dict[str, Any]:
    return {"success": True, "data": data, "message": message}


def require_success(result, label: str) -> Any:
    """Raise an honest 500 when an agent execution failed."""

    if not getattr(result, "success", True):
        raise HTTPException(
            status_code=500,
            detail=f"{label} failed: {'; '.join(result.warnings) or 'unknown error'}",
        )
    return result.data


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class OnboardRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=80)
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


class TaskActionRequest(BaseModel):
    user_id: str
    task_id: str
    action: str  # COMPLETE | SKIP | STUCK
    score: Optional[float] = None
    time_spent_minutes: float = 0.0


class AnswerRequest(BaseModel):
    user_id: str
    practice_id: str
    selected_index: Optional[int] = None
    time_spent_minutes: float = 4.0


class ChatRequest(BaseModel):
    user_id: str
    message: str = Field(min_length=1)


class RouteRequest(BaseModel):
    prompt: str = Field(min_length=1)


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = None
    education: Optional[str] = None
    experience: Optional[str] = None
    goal: Optional[str] = None
    target_role: Optional[str] = None
    learning_style: Optional[str] = None
    available_hours: Optional[float] = Field(default=None, ge=0, le=80)


class SkillAddRequest(BaseModel):
    skill: str = Field(min_length=1, max_length=80)


class ProjectAddRequest(BaseModel):
    title: str = Field(min_length=1, max_length=140)
    stack: List[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)


class CertificationAddRequest(BaseModel):
    certification: str = Field(min_length=1, max_length=140)


# In-memory practice cache: practice_id -> question (per process, per user).
# Marking needs the correct_index, which is never sent to the client.
_practice_cache: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Discovery / monitoring (public read)
# ---------------------------------------------------------------------------
@router.get("/health")
def health() -> Dict[str, Any]:
    orch = get_orchestrator()
    return ok({
        "agent": "EduLearnerAI",
        "status": "online",
        "version": "1.0.0",
        "database": orch.repo.health(),
        "llm": "configured" if orch.llm.available else "offline",
        "llm_model": getattr(orch.llm, "active_model", "") or orch.llm.model,
        "agents": len(orch.agents),
        "learners": len(orch.repo.users()),
    })


@router.get("/agents")
def agents() -> Dict[str, Any]:
    return ok(get_orchestrator().registry())


# ---------------------------------------------------------------------------
# Onboarding & analysis
# ---------------------------------------------------------------------------
@router.post("/onboard", dependencies=[Depends(require_api_key)])
def onboard(payload: OnboardRequest,
            db: Session = Depends(get_db)) -> Dict[str, Any]:
    orch = get_orchestrator()
    orch.repo.delete_user(payload.user_id)
    profile = UserProfile(**payload.model_dump())
    orch.repo.put("profile", payload.user_id, profile.model_dump())
    # Keep the real SQL row in the shared SARA database in sync.
    row = _get_profile_row(db, payload.user_id)
    _write_profile_fields(row, payload.model_dump())
    db.commit()
    ctx = orch.context(payload.user_id)
    analysis = orch.initial(ctx)
    return ok({"profile": profile.model_dump(), "analysis": analysis},
              "Onboarding complete; analysis generated from evidence")


@router.get("/dashboard/{user_id}")
def dashboard(user_id: str) -> Dict[str, Any]:
    orch = get_orchestrator()
    ctx = orch.context(user_id)
    orch.ensure_analysis(ctx)
    profile = ctx.profile
    checks = [profile.name, profile.education, profile.skills,
              profile.target_role or profile.goal, profile.resume_text]
    completion = int(100 * sum(1 for c in checks if c) / len(checks))
    return ok({
        "profile": profile.model_dump(),
        "profile_completion": completion,
        "skills": [s.model_dump() for s in ctx.skills],
        "gaps": [g.model_dump() for g in ctx.gaps],
        "roadmap": ctx.roadmap,
        "plan": [t.model_dump() for t in ctx.plan],
        "readiness": ctx.readiness.model_dump() if ctx.readiness else None,
        "remediation": ctx.remediation,
        "memory": ctx.memory,
        "progress": [r.model_dump() for r in ctx.progress],
        "practice_history": ctx.practice_history,
    })


@router.get("/roadmap/{user_id}")
def roadmap(user_id: str) -> Dict[str, Any]:
    ctx = get_orchestrator().context(user_id)
    if not ctx.roadmap:
        raise HTTPException(status_code=404,
                            detail="No roadmap yet — run onboarding for this learner")
    return ok(ctx.roadmap)


@router.get("/progress/{user_id}")
def progress(user_id: str) -> Dict[str, Any]:
    orch = get_orchestrator()
    ctx = orch.context(user_id)
    tracking = orch.call(ctx, "ProgressTrackerAgent")
    return ok(tracking.data)


@router.get("/adaptations/{user_id}")
def adaptations(user_id: str) -> Dict[str, Any]:
    entries = get_orchestrator().repo.get("adaptations", user_id) or []
    return ok({"entries": entries[-50:], "count": len(entries)})


@router.get("/memory/{user_id}")
def memory(user_id: str) -> Dict[str, Any]:
    ctx = get_orchestrator().context(user_id)
    return ok(ctx.memory)


# ---------------------------------------------------------------------------
# Planning & practice (mutations — API-key protected)
# ---------------------------------------------------------------------------
@router.post("/plan/today", dependencies=[Depends(require_api_key)])
def plan_today(user_id: str) -> Dict[str, Any]:
    orch = get_orchestrator()
    ctx = orch.context(user_id)
    orch.ensure_analysis(ctx)
    plan = orch.call(ctx, "PlannerAgent", period="today")
    orch.persist(ctx)
    return ok(plan.data)


@router.post("/plan/task", dependencies=[Depends(require_api_key)])
def task_action(payload: TaskActionRequest) -> Dict[str, Any]:
    action = payload.action.upper()
    if action not in {"COMPLETE", "SKIP", "STUCK"}:
        raise HTTPException(status_code=422,
                            detail="action must be COMPLETE, SKIP or STUCK")
    orch = get_orchestrator()
    ctx = orch.context(payload.user_id)
    task = next((t for t in ctx.plan if t.task_id == payload.task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found in current plan")

    completed = action == "COMPLETE"
    score = payload.score if payload.score is not None else (
        100.0 if completed else 0.0)
    record = ProgressRecord(
        task_id=task.task_id, skill=task.skill, score=score,
        completed=completed, time_spent_minutes=payload.time_spent_minutes,
        note=f"{action} on '{task.title}'",
    )
    result = orch.adaptive_loop(ctx, record)
    orch.repo.append("progress", payload.user_id, record.model_dump())
    return ok({"record": record.model_dump(), "adaptive": result},
              f"Task {action.lower()} recorded; adaptive loop re-ran")


@router.post("/practice/generate", dependencies=[Depends(require_api_key)])
def practice_generate(user_id: str, skill: Optional[str] = None,
                      count: int = 5) -> Dict[str, Any]:
    orch = get_orchestrator()
    ctx = orch.context(user_id)
    orch.ensure_analysis(ctx)
    questions = require_success(
        orch.call(ctx, "PracticeGeneratorAgent", skill=skill, count=count),
        "PracticeGeneratorAgent")
    for q in questions:
        _practice_cache[q["practice_id"]] = {**q, "user_id": user_id}
    return ok({"questions": questions, "count": len(questions)})


@router.post("/practice/answer", dependencies=[Depends(require_api_key)])
def practice_answer(payload: AnswerRequest) -> Dict[str, Any]:
    question = _practice_cache.get(payload.practice_id)
    if question is None or question.get("user_id") != payload.user_id:
        raise HTTPException(status_code=404, detail="Practice question not found")

    correct_index = question.get("correct_index")
    answered = payload.selected_index
    correct = (correct_index is not None and answered is not None
               and answered == correct_index)
    score = 100.0 if correct else 0.0
    if correct_index is None:
        # Open question: a self-reported attempt still counts as engagement.
        score = 50.0

    orch = get_orchestrator()
    ctx = orch.context(payload.user_id)
    record = ProgressRecord(
        skill=question.get("skill", ""),
        score=score, completed=score >= 50,
        time_spent_minutes=payload.time_spent_minutes,
        note=f"Answered '{question.get('topic', '')}' "
             f"({'correct' if correct else 'incorrect' if correct_index is not None else 'open'})",
    )
    result = orch.adaptive_loop(ctx, record)
    orch.repo.append("progress", payload.user_id, record.model_dump())
    return ok({
        "correct": correct,
        "score": score,
        "explanation": question.get("explanation", ""),
        "misconceptions": question.get("misconceptions", []),
        "adaptive": result.get("adaptation"),
        "remediation": result.get("adaptation", {}).get("remediation", {}),
    })


@router.post("/interview/readiness", dependencies=[Depends(require_api_key)])
def interview_readiness(user_id: str) -> Dict[str, Any]:
    orch = get_orchestrator()
    ctx = orch.context(user_id)
    orch.ensure_analysis(ctx)
    readiness = orch.call(ctx, "InterviewReadinessAgent")
    orch.persist(ctx)
    return ok(readiness.data)


@router.post("/adaptive/analyze", dependencies=[Depends(require_api_key)])
def adaptive_analyze(user_id: str, skill: Optional[str] = None) -> Dict[str, Any]:
    orch = get_orchestrator()
    ctx = orch.context(user_id)
    result = orch.adaptive_loop(ctx, None if skill is None else ProgressRecord(skill=skill))
    return ok(result)


@router.post("/report/generate", dependencies=[Depends(require_api_key)])
def report_generate(user_id: str, period: str = "weekly") -> Dict[str, Any]:
    orch = get_orchestrator()
    ctx = orch.context(user_id)
    orch.ensure_analysis(ctx)
    last_adaptation = orch.repo.get("last_adaptation", user_id) or None
    report = require_success(
        orch.call(ctx, "ReportGeneratorAgent", period=period,
                  adaptation=last_adaptation),
        "ReportGeneratorAgent")
    orch.repo.append("reports", user_id, report, limit=50)
    return ok(report)


# ---------------------------------------------------------------------------
# Conversational mentor (agent-to-agent / chat surface)
# ---------------------------------------------------------------------------
@router.post("/chat", dependencies=[Depends(require_api_key)])
def chat(payload: ChatRequest) -> Dict[str, Any]:
    orch = get_orchestrator()
    ctx = orch.context(payload.user_id)
    orch.ensure_analysis(ctx)
    response = orch.call(
        ctx, "ConversationalMentorAgent", message=payload.message).data
    orch.repo.append("chat_history", payload.user_id, {
        "role": "user", "content": payload.message,
    }, limit=200)
    orch.repo.append("chat_history", payload.user_id, {
        "role": "agent", "content": response.get("answer", ""),
        "actions": response.get("actions", []),
    }, limit=200)
    return ok(response)


@router.get("/chat/history/{user_id}")
def chat_history(user_id: str) -> Dict[str, Any]:
    entries = get_orchestrator().repo.get("chat_history", user_id) or []
    return ok({"entries": entries[-60:], "count": len(entries)})


# ---------------------------------------------------------------------------
# Profile system — real SQL rows in the shared SARA database (sara.db)
#
# The learner profile lives in the edulearner_profiles table (see
# app.database.models). Every mutation also mirrors the profile into the
# engine's JSON store and re-runs the analysis pipeline, so gaps, roadmap and
# plans stay honest with the evidence the learner just changed.
# ---------------------------------------------------------------------------
RESUME_DIR = Path(__file__).resolve().parents[4] / "sara_storage" / "edulearner" / "resumes"
MAX_RESUME_BYTES = 10 * 1024 * 1024


def _loads_list(raw: Optional[str]) -> List[Any]:
    try:
        parsed = json.loads(raw or "")
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _get_profile_row(db: Session, user_id: str) -> db_models.EduLearnerProfile:
    row = db.get(db_models.EduLearnerProfile, user_id)
    if row is None:
        row = db_models.EduLearnerProfile(user_id=user_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _write_profile_fields(row: db_models.EduLearnerProfile, data: Dict[str, Any]) -> None:
    """Write the JSON-store profile dict onto the SQL row (add or update)."""

    if "name" in data:
        row.name = data.get("name") or ""
    if "education" in data:
        row.education = data.get("education") or ""
    if "experience" in data:
        row.experience = data.get("experience") or ""
    if "resume_text" in data:
        row.resume_text = data.get("resume_text") or ""
    if "skills" in data:
        row.skills = json.dumps(data.get("skills") or [], ensure_ascii=False)
    if "projects" in data:
        row.projects = json.dumps(data.get("projects") or [], ensure_ascii=False, default=str)
    if "certifications" in data:
        row.certifications = json.dumps(data.get("certifications") or [], ensure_ascii=False)
    if "goal" in data:
        row.goal = data.get("goal") or ""
    if "target_role" in data:
        row.target_role = data.get("target_role") or ""
    if "learning_style" in data:
        row.learning_style = data.get("learning_style") or "mixed"
    if "available_hours" in data:
        try:
            row.available_hours = int(round(float(data.get("available_hours") or 7.0)))
        except (TypeError, ValueError):
            row.available_hours = 7


def _profile_payload(row: db_models.EduLearnerProfile) -> Dict[str, Any]:
    projects = _loads_list(row.projects)
    for project in projects:
        if isinstance(project, dict) and not project.get("id"):
            project["id"] = f"proj_{uuid.uuid4().hex[:12]}"
    return {
        "user_id": row.user_id,
        "name": row.name or "",
        "education": row.education or "",
        "experience": row.experience or "",
        "resume_text": row.resume_text or "",
        "skills": _loads_list(row.skills),
        "projects": projects,
        "certifications": _loads_list(row.certifications),
        "goal": row.goal or "",
        "target_role": row.target_role or "",
        "learning_style": row.learning_style or "mixed",
        "available_hours": float(row.available_hours or 7.0),
    }


def _mirror_to_engine(user_id: str, data: Dict[str, Any]) -> None:
    """Copy the SQL profile into the engine's JSON store so the adaptive
    agents read exactly what the Profile tab saved."""

    payload = dict(data)
    payload["user_id"] = user_id
    get_orchestrator().repo.put("profile", user_id, payload)


def _reanalyse(user_id: str) -> Dict[str, Any]:
    """Re-run the full analysis pipeline from the updated evidence."""

    orch = get_orchestrator()
    ctx = orch.context(user_id)
    analysis = orch.initial(ctx)
    return {
        "gaps": len(analysis.get("gaps") or []),
        "roadmap_weeks": (analysis.get("roadmap") or {}).get("total_weeks", 0),
    }


def _save_sync_reanalyse(user_id: str, db: Session) -> Dict[str, Any]:
    row = _get_profile_row(db, user_id)
    data = _profile_payload(row)
    # Persist the normalized payload (e.g. projects that just gained ids) so
    # later per-item deletes find them.
    _write_profile_fields(row, data)
    db.commit()
    _mirror_to_engine(user_id, data)
    summary = _reanalyse(user_id)
    return {"profile": data, "analysis_summary": summary}


def _extract_resume_text(filename: str, raw: bytes) -> str:
    """Best-effort text extraction: PDF, DOCX or plain text."""

    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception:  # noqa: BLE001 - surface as empty, validated by caller
            return ""
    if suffix == ".docx":
        try:
            import docx  # python-docx

            document = docx.Document(io.BytesIO(raw))
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        except Exception:  # noqa: BLE001
            return ""
    return raw.decode("utf-8", errors="ignore")


# -- profile CRUD -----------------------------------------------------------
@router.get("/profile/{user_id}")
def get_profile(user_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = _get_profile_row(db, user_id)
    payload = _profile_payload(row)
    resume = (
        db.query(db_models.EduLearnerResume)
        .filter(db_models.EduLearnerResume.user_id == user_id)
        .order_by(db_models.EduLearnerResume.uploaded_at.desc())
        .first()
    )
    payload["resume"] = (
        {"id": resume.id, "filename": resume.filename,
         "uploaded_at": str(resume.uploaded_at)}
        if resume else None
    )
    return ok(payload)


@router.put("/profile/{user_id}", dependencies=[Depends(require_api_key)])
def update_profile(user_id: str, payload: ProfileUpdateRequest,
                   db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = _get_profile_row(db, user_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No profile fields provided")
    _write_profile_fields(row, changes)
    result = _save_sync_reanalyse(user_id, db)
    return ok(result, "Profile saved; analysis re-run from the new evidence")


@router.delete("/profile/{user_id}", dependencies=[Depends(require_api_key)])
def delete_profile(user_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    get_orchestrator().repo.delete_user(user_id)
    db.query(db_models.EduLearnerResume).filter(
        db_models.EduLearnerResume.user_id == user_id).delete()
    row = db.get(db_models.EduLearnerProfile, user_id)
    if row is not None:
        db.delete(row)
    db.commit()
    return ok({"user_id": user_id}, "Learner profile and engine state deleted")


# -- skills -----------------------------------------------------------------
@router.post("/profile/{user_id}/skills", dependencies=[Depends(require_api_key)])
def add_skill(user_id: str, payload: SkillAddRequest,
             db: Session = Depends(get_db)) -> Dict[str, Any]:
    skill = payload.skill.strip()
    row = _get_profile_row(db, user_id)
    skills = _loads_list(row.skills)
    if any(str(s).lower() == skill.lower() for s in skills):
        raise HTTPException(status_code=409, detail=f"Skill '{skill}' is already listed")
    skills.append(skill)
    row.skills = json.dumps(skills, ensure_ascii=False)
    result = _save_sync_reanalyse(user_id, db)
    return ok(result, f"Skill '{skill}' added; analysis re-run")


@router.delete("/profile/{user_id}/skills/{skill}", dependencies=[Depends(require_api_key)])
def remove_skill(user_id: str, skill: str,
                 db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = _get_profile_row(db, user_id)
    skills = _loads_list(row.skills)
    remaining = [s for s in skills if str(s).lower() != skill.strip().lower()]
    if len(remaining) == len(skills):
        raise HTTPException(status_code=404, detail=f"Skill '{skill}' not found")
    row.skills = json.dumps(remaining, ensure_ascii=False)
    result = _save_sync_reanalyse(user_id, db)
    return ok(result, f"Skill '{skill}' removed; analysis re-run")


# -- projects ---------------------------------------------------------------
@router.post("/profile/{user_id}/projects", dependencies=[Depends(require_api_key)])
def add_project(user_id: str, payload: ProjectAddRequest,
                db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = _get_profile_row(db, user_id)
    projects = _loads_list(row.projects)
    projects.append({
        "id": f"proj_{uuid.uuid4().hex[:12]}",
        "title": payload.title.strip(),
        "stack": [s.strip() for s in payload.stack if str(s).strip()],
        "description": payload.description.strip(),
    })
    row.projects = json.dumps(projects, ensure_ascii=False, default=str)
    result = _save_sync_reanalyse(user_id, db)
    return ok(result, f"Project '{payload.title.strip()}' added; analysis re-run")


@router.delete("/profile/{user_id}/projects/{project_id}", dependencies=[Depends(require_api_key)])
def remove_project(user_id: str, project_id: str,
                   db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = _get_profile_row(db, user_id)
    projects = _loads_list(row.projects)
    remaining = [p for p in projects
                 if not (isinstance(p, dict) and p.get("id") == project_id)]
    if len(remaining) == len(projects):
        raise HTTPException(status_code=404, detail="Project not found")
    row.projects = json.dumps(remaining, ensure_ascii=False, default=str)
    result = _save_sync_reanalyse(user_id, db)
    return ok(result, "Project removed; analysis re-run")


# -- certifications -----------------------------------------------------------
@router.post("/profile/{user_id}/certifications", dependencies=[Depends(require_api_key)])
def add_certification(user_id: str, payload: CertificationAddRequest,
                      db: Session = Depends(get_db)) -> Dict[str, Any]:
    certification = payload.certification.strip()
    row = _get_profile_row(db, user_id)
    certs = _loads_list(row.certifications)
    if any(str(c).lower() == certification.lower() for c in certs):
        raise HTTPException(status_code=409,
                            detail=f"Certification '{certification}' is already listed")
    certs.append(certification)
    row.certifications = json.dumps(certs, ensure_ascii=False)
    result = _save_sync_reanalyse(user_id, db)
    return ok(result, f"Certification '{certification}' added; analysis re-run")


@router.delete("/profile/{user_id}/certifications/{certification}", dependencies=[Depends(require_api_key)])
def remove_certification(user_id: str, certification: str,
                         db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = _get_profile_row(db, user_id)
    certs = _loads_list(row.certifications)
    remaining = [c for c in certs if str(c).lower() != certification.strip().lower()]
    if len(remaining) == len(certs):
        raise HTTPException(status_code=404, detail="Certification not found")
    row.certifications = json.dumps(remaining, ensure_ascii=False)
    result = _save_sync_reanalyse(user_id, db)
    return ok(result, "Certification removed; analysis re-run")


# -- resume -----------------------------------------------------------------
@router.post("/profile/{user_id}/resume", dependencies=[Depends(require_api_key)])
async def upload_resume(user_id: str, file: UploadFile = File(...),
                        db: Session = Depends(get_db)) -> Dict[str, Any]:
    raw = await file.read()
    if len(raw) > MAX_RESUME_BYTES:
        raise HTTPException(status_code=413, detail="Resume file is larger than 10 MB")
    filename = Path(file.filename or "resume.txt").name or "resume.txt"
    text = _extract_resume_text(filename, raw)
    if not text.strip():
        raise HTTPException(
            status_code=422,
            detail="Could not extract text from this file — upload a PDF, DOCX or TXT resume",
        )

    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = f"{user_id}_{uuid.uuid4().hex[:8]}_{filename}"
    stored_path = RESUME_DIR / stored_name
    stored_path.write_bytes(raw)

    db.query(db_models.EduLearnerResume).filter(
        db_models.EduLearnerResume.user_id == user_id).delete()
    resume_row = db_models.EduLearnerResume(
        user_id=user_id, filename=filename,
        stored_path=str(stored_path), extracted_text=text,
    )
    db.add(resume_row)
    row = _get_profile_row(db, user_id)
    row.resume_text = text
    result = _save_sync_reanalyse(user_id, db)
    result["resume"] = {
        "id": resume_row.id, "filename": filename,
        "characters": len(text), "uploaded_at": str(resume_row.uploaded_at),
    }
    return ok(result,
              f"Resume parsed ({len(text)} characters extracted); analysis re-run")


@router.delete("/profile/{user_id}/resume", dependencies=[Depends(require_api_key)])
def delete_resume(user_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    rows = db.query(db_models.EduLearnerResume).filter(
        db_models.EduLearnerResume.user_id == user_id).all()
    for resume_row in rows:
        if resume_row.stored_path:
            try:
                Path(resume_row.stored_path).unlink(missing_ok=True)
            except OSError:
                pass
        db.delete(resume_row)
    row = _get_profile_row(db, user_id)
    row.resume_text = ""
    result = _save_sync_reanalyse(user_id, db)
    return ok(result, "Resume removed; analysis re-run")


# ---------------------------------------------------------------------------
# SARA routing — how the Nexus Hub decides EduLearner AI is the right agent
# ---------------------------------------------------------------------------
_EDULEARNER_KEYWORDS = (
    "learn", "learning", "study", "skill", "skills", "roadmap", "practice",
    "interview", "readiness", "gap", "career", "course", "mentor", "student",
    "upskill", "reskill", "education", "exam", "prepare",
)


@router.post("/route")
def route(payload: RouteRequest) -> Dict[str, Any]:
    """Score how relevant a free-text task is for EduLearner AI.

    The SARA Autonomous Agent (and the hub orchestrator) call this so routing
    is explicit and auditable instead of keyword guessing inside the UI.
    """

    text = payload.prompt.lower()
    hits = [word for word in _EDULEARNER_KEYWORDS if word in text]
    score = min(1.0, 0.15 * len(hits))
    matched = sorted(set(hits))
    return ok({
        "agent": "EduLearnerAI",
        "relevant": bool(matched),
        "confidence": round(score, 3),
        "matched_keywords": matched,
        "suggested_route": "/hub/edulearner-ai" if matched else None,
        "capabilities": [
            "Skill gap analysis", "Adaptive learning roadmaps", "Marked practice",
            "Daily planning", "Interview readiness", "Learning memory",
        ] if matched else [],
    })
