# actions/edulearner_ai.py
"""EduLearner AI action - the adaptive tutor, by voice.

Calls the real backend tutor (POST /api/edulearner/chat, GET health) - the
same routes the Hub workspace uses. Answers come from the real
ConversationalMentor agent, grounded in the learner profile stored by the
backend; chat history is persisted exactly like the web UI's would be.
"""
from __future__ import annotations

from typing import Any, Dict

import requests

from core import hub_agents_client as hub

NAME = "EduLearner AI"
USER_ID = "sara-desktop"
CHAT_TIMEOUT = 180


def _answer_of(payload: Dict[str, Any]) -> str:
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    for key in ("answer", "reply", "message", "response", "content"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return hub.brief(val, 900)
    # Some agents return nested sections - fall back to a readable brief.
    nested = data.get("data")
    if isinstance(nested, dict):
        for key in ("answer", "reply", "message"):
            val = nested.get(key)
            if isinstance(val, str) and val.strip():
                return hub.brief(val, 900)
    return ""


def run_edulearner(parameters: dict) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    message = str(p.get("message") or p.get("question") or p.get("query") or "").strip()

    if not action:
        action = "ask" if message else "status"

    if action in ("status", "health"):
        health = hub.health("/api/edulearner/health")
        if not health:
            return f"{NAME} is not reachable right now. {hub.BACKEND_HINT}"
        data = health.get("data") if isinstance(health.get("data"), dict) else health
        return (f"{NAME} is {data.get('status', 'online')} - LLM: "
                f"{data.get('llm', 'unknown')}, {data.get('agents', '?')} tutor agents, "
                f"{data.get('learners', '?')} learner profile(s). Ask me a learning question any time.")

    if not message:
        return "Ask me a learning question - for example: 'explain gradient descent like I'm a beginner'."

    try:
        resp = hub.post("/api/edulearner/chat",
                        {"user_id": USER_ID, "message": message[:2000]},
                        timeout=CHAT_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    try:
        payload = resp.json()
    except Exception:
        return "EduLearner returned an unreadable response."

    answer = _answer_of(payload)
    if not answer:
        return f"EduLearner answered without readable text ({hub.brief(payload, 200)})."
    return answer


TOOL = {
    "name": "edulearner_ai",
    "description": (
        "Asks SARA's EduLearner AI tutor a learning question and returns its real, "
        "grounded answer (adaptive mentor agent). Use for study/learning questions "
        "like 'explain X', 'quiz me on Y', 'make a study plan for Z'. "
        "To OPEN the EduLearner workspace (not ask), use open_agency_workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Optional: 'ask' (default when a question is given) or 'status'.",
            },
            "message": {
                "type": "STRING",
                "description": "The learning question or request.",
            },
        },
        "required": [],
    },
    "handler": run_edulearner,
}
