"""End-to-end smoke test for EduLearner AI through the real HTTP API.

Run from backend/:  python -m tests.test_edulearner
Exercises: health -> agents -> route -> onboard -> dashboard -> plan ->
task action -> practice -> answer (wrong, twice, to force remediation) ->
readiness -> adaptive -> report -> chat -> memory -> adaptations.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.deps import API_KEY  # noqa: E402

USER = "edulearner-smoke"
HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}


def main() -> None:
    client = TestClient(app)
    failures = []

    def step(label: str, response) -> dict:
        body = response.json()
        ok = response.status_code < 400 and body.get("success", True)
        print(f"  [{'OK ' if ok else 'FAIL'}] {label} -> {response.status_code}")
        if not ok:
            failures.append(label)
            print("        ", str(body)[:220])
        return body

    print("EduLearner AI end-to-end smoke test")
    print("-" * 60)

    step("health", client.get("/api/edulearner/health"))
    agents = step("agents registry", client.get("/api/edulearner/agents"))
    print(f"        {agents['data']['count']} agents, "
          f"{len(agents['data']['pipeline'])} pipeline steps, db={agents['data']['database']}")

    route = step("SARA route scoring",
                 client.post("/api/edulearner/route",
                             json={"prompt": "Help me learn Python and prepare for interviews"}))
    print(f"        relevant={route['data']['relevant']} "
          f"route={route['data']['suggested_route']}")

    onboard = step("onboard", client.post("/api/edulearner/onboard", headers=HEADERS, json={
        "user_id": USER,
        "name": "Aarav Sharma",
        "education": "B.Tech Computer Science, 2nd year",
        "skills": ["C++", "Python", "HTML", "CSS", "React", "MongoDB", "Git"],
        "goal": "Software Engineer",
        "target_role": "Software Engineer",
        "available_hours": 10,
    }))
    analysis = onboard["data"]["analysis"]
    print(f"        gaps={len(analysis['gaps'])} top={analysis['gaps'][0]['skill']}")

    dashboard = step("dashboard", client.get(f"/api/edulearner/dashboard/{USER}"))
    print(f"        readiness={dashboard['data']['readiness']['overall']:.0f}/100, "
          f"completion={dashboard['data']['profile_completion']}%")

    step("roadmap", client.get(f"/api/edulearner/roadmap/{USER}"))
    step("progress", client.get(f"/api/edulearner/progress/{USER}"))

    plan = step("plan today",
                client.post(f"/api/edulearner/plan/today?user_id={USER}", headers=HEADERS))
    tasks = plan["data"]["tasks"]
    print(f"        {len(tasks)} tasks, "
          f"{sum(t['estimated_minutes'] for t in tasks)} minutes")
    if tasks:
        step("task complete", client.post("/api/edulearner/plan/task", headers=HEADERS, json={
            "user_id": USER, "task_id": tasks[0]["task_id"],
            "action": "COMPLETE", "time_spent_minutes": 30,
        }))

    practice = step("practice generate",
                    client.post(f"/api/edulearner/practice/generate?user_id={USER}&skill=DSA&count=3",
                                headers=HEADERS))
    questions = practice["data"]["questions"]
    print(f"        {practice['data']['count']} questions, first topic: {questions[0]['topic']}")

    # Answer the first two questions WRONG on purpose to force remediation.
    for index, question in enumerate(questions[:2]):
        correct = question.get("correct_index")
        wrong = 0 if correct != 0 else 1
        answer = step(f"practice answer {index + 1} (wrong)",
                      client.post("/api/edulearner/practice/answer", headers=HEADERS, json={
                          "user_id": USER, "practice_id": question["practice_id"],
                          "selected_index": wrong, "time_spent_minutes": 4,
                      }))
        print(f"        correct={answer['data']['correct']} score={answer['data']['score']}")

    adaptive = step("adaptive analyze",
                    client.post(f"/api/edulearner/adaptive/analyze?user_id={USER}", headers=HEADERS))
    adaptation = adaptive["data"]["adaptation"]
    print(f"        decision={adaptation['decision']} state={adaptation['state']} "
          f"on {adaptation.get('skill')}")
    print(f"        remediation active: {sorted(adaptive['data']['adaptation'].get('remediation', {}))}")

    step("readiness", client.post(f"/api/edulearner/interview/readiness?user_id={USER}",
                                  headers=HEADERS))
    report = step("report",
                  client.post(f"/api/edulearner/report/generate?user_id={USER}", headers=HEADERS))
    print(f"        roadmap changes: {report['data']['roadmap_changes'][:2]}")

    chat = step("mentor chat",
                client.post("/api/edulearner/chat", headers=HEADERS,
                            json={"user_id": USER, "message": "What should I study today?"}))
    print(f"        answer: {chat['data']['answer'][:90]}...")
    print(f"        llm_used={chat['data'].get('llm_used')}")

    memory = step("memory", client.get(f"/api/edulearner/memory/{USER}"))
    print(f"        bottleneck: {memory['data'].get('current_bottleneck')}")
    adaptations = step("adaptations log",
                       client.get(f"/api/edulearner/adaptations/{USER}"))
    print(f"        {adaptations['data']['count']} recorded executions")

    print("-" * 60)
    if failures:
        print(f"FAILED steps: {failures}")
        raise SystemExit(1)
    print("All steps passed.")


if __name__ == "__main__":
    main()
