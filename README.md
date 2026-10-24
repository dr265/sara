# SARA AI Nexus

**An autonomous multi-agent AI ecosystem that turns business intent into coordinated action.**

SARA unifies social media, voice calling, messaging, email, documents, research, financial analysis, and desktop automation behind one command center. A single FastAPI orchestration core routes each request to the right agent, with LLM failover across providers, tenant-scoped state, a Next.js hub for operators, and a PyQt6 desktop app for local machine control.

---

## Highlights

- **One backend for every agent** — API, WebSockets, background pollers, and agent orchestration run in a single process.
- **LLM failover router** — OpenAI-compatible providers are tried in order, so a quota error or outage degrades gracefully instead of breaking the request.
- **Voice stack** — Twilio telephony with Sarvam and ElevenLabs speech, multilingual detection, inbound booking, and outbound calling.
- **Omnichannel messaging** — WhatsApp Cloud API and transport providers, Telegram, Gmail/SMTP, and social publishing.
- **Desktop agent** — a standalone Windows assistant that executes local tools, screen capture, hotkeys, and system actions.
- **Secure defaults** — secrets live in git-ignored env files, API routes are key-gated, and paired-device tokens are stored hashed.

## Hub modules

| Module | Purpose |
| --- | --- |
| Sara Bots | Build and run custom worker bots with skills, schedules, memory, and task runs |
| Sara Muse | Creative assistant workspace |
| Sara Sales | Sales outreach and contact workflows |
| Sara Ultron | Device fleet, remote execution, and system actions |
| Voice Agent | Inbound booking and outbound calls with multilingual speech |
| WhatsApp Agent | WhatsApp conversations and channel management |
| Telegram Assistant | Telegram bot and booking workflows |
| Email Automation | Campaigns, templates, attachments, and Gmail/SMTP delivery |
| Social Media | Scheduled posts and social campaigns |
| Music Studio | Music generation and production tools |
| Game Forge | Game and project generation |
| Edulearner AI | Adaptive learning, mentoring, and progress tracking |
| Trading Agent | Market analysis workflows |
| AI Web Scraper | Research and structured data extraction |
| Frontend / Backend Architect | Generate application code from prompts |
| Sara Auto | Autonomous task automation |

## Architecture

```text
Browser / operator
   │  HTTPS + WebSocket
   ▼
Next.js hub (:3000) ── server-side proxy ──┐
                                           ▼
                           FastAPI unified backend (:8000 local, :8080 Docker)
                             ├─ agent orchestration + LLM failover router
                             ├─ voice: Twilio, Sarvam, ElevenLabs
                             ├─ messaging: WhatsApp, Telegram, Gmail/SMTP
                             ├─ documents, RAG, research, trading
                             └─ SQLite (default) or PostgreSQL 16 + pgvector
                                           ▲
                                           │  /ultron/* + hub-agent routes
                                           │  (and direct LLM access)
                           Windows desktop agent (PyQt6)
                             └─ local tools, screen, microphone, system control
```

## Tech stack

| Layer | Stack |
| --- | --- |
| Frontend | Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS 4, Three.js, MediaPipe, Clerk |
| Backend | Python 3.13, FastAPI, Uvicorn, SQLAlchemy 2, Pydantic 2, OpenAI-compatible clients |
| Data | SQLite by default; PostgreSQL 16 with pgvector as an optional profile |
| Voice and messaging | Twilio, Sarvam, ElevenLabs, WhatsApp Cloud API, Telegram, SMTP/Gmail |
| Desktop | Python, PyQt6, WebSockets |
| Infrastructure | Docker Compose, optional Cloudflare Tunnel (`tools/cloudflared.exe`) |

## Repository layout

```text
backend/          FastAPI app, agents, services, API routers, tests
frontend/         Next.js hub and workspace UI
sara_desktop/     Standalone Windows PyQt6 assistant
landing/          Static landing page
docs/             Integration and design notes
tools/            Optional external binaries
docker-compose.yml
start_all.bat     Windows launcher for the backend and frontend
```

## Getting started

### Prerequisites

- Python 3.13
- Node.js 20+ and npm
- Docker (optional, for the container stack)
- Windows (only for `sara_desktop` desktop control)

Provider keys are optional at boot. Features that need Twilio, Sarvam, Gmail, WhatsApp, or an LLM provider become available once the matching keys are configured.

### Local development

Start the backend:

```sh
python -m venv backend/venv
source backend/venv/bin/activate      # Windows: backend\venv\Scripts\activate
pip install -r backend/requirements.txt
cp .env.example backend/.env          # Windows: copy .env.example backend\.env
python backend/run_backend.py
```

Start the frontend in a second terminal:

```sh
npm --prefix frontend install
npm run dev
```

- Frontend: http://localhost:3000
- Backend: http://127.0.0.1:8000
- Health check: http://127.0.0.1:8000/api/health

On Windows, `start_all.bat` launches both processes. `start_all.bat stop` stops only the processes listening on the configured backend and frontend ports.

### Docker Compose

```sh
cp .env.example backend/.env
cp frontend/.env.docker.example frontend/.env.docker
docker compose --env-file frontend/.env.docker up -d --build
```

- Frontend: http://localhost:3000
- Backend: http://127.0.0.1:8080 (loopback only by default)
- Optional local PostgreSQL: `docker compose --profile localdb up -d`

Point `DATABASE_URL` at a hosted Postgres (for example Neon) to skip the local database profile. The `sara_state` volume stores the paired-device registry and must survive rebuilds, or every deployment unpairs connected devices.

### Desktop assistant

```sh
cp sara_desktop/config/api_keys.example.json sara_desktop/config/api_keys.json
cd sara_desktop
pip install -r requirements.txt
python main.py
```

Fill in `sara_desktop/config/api_keys.json` with your provider keys or export `LLM_API_KEY` and `ANYMODEL_API_KEY`. The assistant uses those keys for direct model access and calls the local backend for Ultron, fleet, and hub-agent actions. The optional phone dashboard listens on port 8010 by default; override it with `SARA_DESKTOP_DASH_PORT`.

Desktop control is Windows-only. A Linux container cannot reach the host's mouse, keyboard, screen, or microphone, so `sara_desktop` runs on the PC.

## Configuration

| File | Purpose |
| --- | --- |
| `backend/.env` | Backend secrets and provider settings. Copy from the root `.env.example`, which documents where each variable is used. |
| `frontend/.env.local` or `frontend/.env.docker` | Backend URLs, Clerk keys, and the public API key. |
| `sara_desktop/config/api_keys.json` | Desktop provider keys. Copy from the tracked example. |

Key settings:

- `SARA_API_KEY` gates most `/api` routes.
- `SARA_MASTER_KEY` encrypts stored channel credentials. Set it once and keep it stable; changing it makes existing credentials undecryptable.
- `FULLAI_API_TOKEN` gates the desktop automation agent separately from the web API.
- `DATABASE_URL` selects SQLite or PostgreSQL.

Never commit real credentials. All env files are git-ignored, and a pre-commit secret scanner runs against the repository.

## Testing

```sh
cd backend && python -m pytest tests
cd sara_desktop && python -m pytest tests
npm --prefix frontend run lint
npm --prefix frontend run build
```

Install `pytest` in the active environment if it is not already present.

## Security notes

- Only templates are tracked; every real key stays in a git-ignored env file.
- Paired-device tokens are stored hashed in the `sara_state` volume.
- The Docker backend binds to loopback. Put it behind a reverse proxy if it needs to be reachable externally.
- Rotate provider keys immediately if one is exposed.

## Documentation

- [OpenMuse integration audit and plan](docs/openmuse-integration/00-audit-and-plan.md)
