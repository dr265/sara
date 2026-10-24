# SARA-AI-AGENCY
Sara AI Nexus AGENCY is an autonomous multi-agent AI ecosystem that unifies social media, voice, messaging, documents, Gmail, financial analysis, and desktop automation—turning business intent into coordinated AI actions through one intelligent command center.

## Local development

From the project root, install the frontend dependencies and start its development server:

```sh
npm --prefix frontend install
npm run dev
```

Open the local URL printed by the server (normally http://localhost:3000).
The root `dev` script forwards to `frontend`; you can also run `npm run dev` directly inside that folder.
To choose another port, use `npm run dev -- --port 3001`.

This command starts only the frontend. On Windows, use `start_all.bat` to launch both the Python backend and frontend.

## Backend (unified FastAPI server)

All agents are served by one backend that boots on port 8000 (`SARA_PORT` to override).
There is no requirements file for the backend yet, so install the verified core set
(actually imported by the `app.main` import chain) into a local venv:

```sh
cd backend
python -m venv venv
venv\Scripts\pip install fastapi uvicorn sqlalchemy pydantic-settings python-dotenv httpx requests openai
venv\Scripts\python run_backend.py
```

Check `http://127.0.0.1:8000/api/health` for boot confirmation. Per-agent health
endpoints exist under each agent's prefix (for example `/ultron/health`,
`/api/gameforge/health`, `/api/music/health`, `/agent/health`).
Configuration lives in `backend/.env` — copy root `.env.example` there and fill
in the values you need (each entry documents where it is used).

## Dynamic Election Campaign Voice Agent

A second, fully configurable voice agent built on the existing Voice Calling
Agent. It reuses the same Twilio provider, call state machine, webhooks,
Sarvam STT/TTS/translate stack, 13-language detector and failover LLM router —
nothing about the telephony pipeline is duplicated.

Configure it in the hub at **Voice Agent → 🗳️ Election Campaign**
(`/hub/voice-agent#campaign`). Everything on that screen is saved per user:
representative, organization, campaign, location/ward, position, language,
voice, greeting, identity disclosure, purpose, introduction, approved
message, FAQs, busy/decline/hand-off/do-not-call/closing messages, contact
details, hand-off number and the calling window.

* Backend service: `backend/app/services/voice_campaign.py`
* Engine wiring, tenant-scoped API and webhook routing:
  `backend/app/voice_agent_engine.py` (`/voice-agent/voice/campaign/*`)
* Frontend: `frontend/src/app/hub/voice-agent/election-campaign.tsx`

Placeholders such as `[representative_name]`, `[location]`, `[position]` and
their Telugu equivalents (`[ప్రతినిధి పేరు]`, `[ప్రాంతం]`, `[పదవి]`) are filled
from the saved profile, so changing a value changes what the agent says on
the next call — no source edit is needed.

Calls reuse the existing `voice_calls` table; a campaign call is one whose
row carries an `agent_profile_id`, and it keeps the profile snapshot it
started with. Campaign rows are scoped by the existing tenant key, so one
user's configuration, contacts and calls are invisible to another. Opt-outs
raised on the call are stored immediately and block later calls.

No new credentials are required — it uses the same `TWILIO_*`, `SARVAM_API_KEY`
and `LLM_API_KEY` values as the rest of the voice agent.

## docker-compose services

`docker-compose.yml` starts infrastructure only — none of the app servers run in
containers today:

- `db` — pgvector/pg16 on 5432 (with healthcheck)
- `redis` — on 6379 (with healthcheck)
- `n8n` — workflow automation on 5678 (with healthcheck)
- `ngrok` — tunnel to the host backend on 8000; token comes from root `.env` (`NGROK_AUTHTOKEN`)

## sara_desktop (standalone PyQt6 assistant)

`sara_desktop/` is a separate Windows desktop voice assistant — it does **not**
talk to the platform backend; it connects to Gemini Live / LLM gateways directly
using `sara_desktop/config/api_keys.json` (a separate config surface from the
backend `.env`). That file holds your **real** provider keys and is git-ignored —
copy the tracked template and fill it in once:

```sh
cp sara_desktop/config/api_keys.example.json sara_desktop/config/api_keys.json
```

Alternatively export `LLM_API_KEY` / `ANYMODEL_API_KEY` in the environment and
leave the key fields empty.

```sh
cd sara_desktop
pip install -r requirements.txt
python main.py
```

Its optional phone dashboard now defaults to port **8010** (was 8000, which
collided with the backend); override with `SARA_DESKTOP_DASH_PORT`.

## Other

- `tools/cloudflared.exe` — Cloudflare Tunnel client, an alternative public
  exposure path alongside the compose ngrok service (no script wraps it; run it
  manually if you prefer it over ngrok).
- `start_all.bat stop` stops only the processes listening on the configured
  backend/frontend ports — it no longer kills every node/python process.
