# OpenMuse Integration into Sara Bots — Audit and Plan

## 1. Sources inspected

- OpenMuse (CopilotKit): `D:\openmuse-main\openmuse-main` (MIT). Stack: Node 24, pnpm 11, TypeScript, Hono server, React Native/Expo mobile, PGlite/PostgreSQL, CopilotKit runtime, AG-UI. Features: chat + delegated work, ideas/personal context, goals/tracking, browser (persistent Chromium), Linux computer (Docker container, bounded bash/Python/Node/git), Gmail/Calendar, PDF job, generated results, notifications, connectors catalogue, OpenBot (disabled adapter).
- Sara AI Platform: `C:\Users\HP\sara-ai-platform`. Stack: Python FastAPI + Next.js. Sara Bots subsystem: tools registry, runner adapters, engine (planner + executor), approvals, tasks, memory, schedules, custom bots, agent computer (browser via sara_desktop actions), email automation (Gmail OAuth + SMTP + attachments), agency (267 specialists).

## 2. Capability matrix (OpenMuse -> Sara)

| OpenMuse capability | Sara status | Notes |
|---|---|---|
| Chat + delegated work | Available (Sara Bots engine + tasks) | Durable tasks, approvals, pause/resume/cancel |
| Ideas / personal context | Partial (memory.note) | No proactive feed yet |
| Goals / Tracking (recurring watches) | Partial (schedules) | No change-detection watches yet |
| Browser (persistent Chromium, takeover) | Partial (sara_desktop actions) | No takeover console yet |
| Linux computer (Docker, bounded cmds) | Partial (action.file_processor, action.* code tools) | No isolated container; Windows host |
| Gmail / Calendar | Available (email-automation) | Calendar not yet wired |
| PDF job (import, fill, reply, receipt) | Available (email attachments + file_processor) | No AcroForm filling yet |
| Generated results (reports, scripts) | Available (llm.reason + file tools) | |
| Notifications (in-app inbox) | Partial (approvals inbox) | No general notification inbox |
| Connectors catalogue | Available (Fleet probe + tools registry) | |
| OpenBot adapter | Not implemented (disabled in OpenMuse too) | |
| Reusable skills (SKILL.md) | IMPLEMENTED (this slice) | skills.py + API + UI |
| Security gate (ALLOW/ASK/DENY) | Partial (approvals + autonomy modes) | Formal gate module pending |
| Capability registry with metadata | Partial (tools registry has risk/state) | Source/verification metadata pending |
| Durable task leases/recovery | Partial (tasks persist; recovery on startup) | SQL leases not yet |

## 3. What this slice implemented

Backend:
- `backend/app/services/sara_bots/skills.py`: owner-scoped skill store (JSON, atomic writes), validation against live tool ids, CRUD, skill_instructions() helper, owner isolation.
- `backend/app/api/sara_bots_custom.py`: `GET/POST/PATCH/DELETE /api/sara-bots/skills`, `GET /api/sara-bots/skills/{id}` (SkillBody). Re-added `/api/sara-bots/custom/attachments` (multipart, 15MB cap, returns path/filename/size).
- `backend/app/services/sara_bots/custom.py`: `validate_config` now accepts `skill_ids` and a `skills` field (validated against the owner's skills).
- `backend/app/services/sara_bots/engine.py`: injects enabled skill instructions into the bot context before planning.
- `backend/app/agents/sara_ultron_agent/core/fleet.py` + `backend/app/api/sara_bots.py`: probe forwards the caller's API key so agency-specialists shows available (fixes the 401 in the Fleet view).

Frontend:
- `frontend/src/app/hub/sara-bots/skills/page.tsx`: skills CRUD UI.
- `frontend/src/app/hub/page.tsx`: agency calls now use saraFetch (fixes Agency (0) and offline LLM badge).
- `frontend/src/app/hub/sara-bots/custom-bots.tsx`: Skills link + mic/attach icon buttons.

Tests:
- `backend/tests/test_sara_bots_skills.py`: 7 tests (create/list, unknown tool rejection, update/delete, owner isolation, skill_instructions filtering, validation, create error). All pass.
- Existing `test_sara_bots_custom.py` (56) and `test_email_automation.py` (13) still pass.

## 4. Phased plan (remaining)

- Phase 2: Capability registry metadata (source, verification status, deps) + security gate module (ALLOW/ASK/DENY) with scoped approvals.
- Phase 3: Bot factory wizard integration for skills + capability selection UI in bot editor.
- Phase 4: Agent computer improvements (browser takeover, bounded terminal, workspace) adapted from OpenMuse computer.
- Phase 5: Ideas feed (source-backed suggestions), goals/tracking with recurring watches.
- Phase 6: Notifications inbox, calendar wiring, durable task leases/recovery.
- Phase 7: Full verification + security tests.

## 5. Blockers / next actions

- Frontend bot editor does not yet expose skill attachment (backend accepts it). Next: add a skills multi-select to the wizard and to Edit configuration, sending `skills` in the payload.
- Security gate is still the existing approvals + autonomy modes; a formal ALLOW/ASK/DENY module is pending.
- Agent computer runs on the Windows host; Linux-container isolation from OpenMuse is not yet ported (would require Docker).
- Calendar connector not yet wired (email automation covers Gmail send only).
