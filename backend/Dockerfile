# syntax=docker/dockerfile:1
#
# SARA unified backend — CLOUD image (Linux).
#
# Python 3.13 is a HARD requirement, not a preference. PyPI publishes no cp314
# manylinux wheel for pygame 2.6.1, so a Python 3.14 base image cannot install
# it and GameForge is permanently broken with "pygame is not installed in the
# backend venv". On 3.13 the wheel exists and installs cleanly:
#   pygame-2.6.1-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl
#
# This image contains the CLOUD half of the platform: HTTP API, WebSockets,
# agent orchestration, LLM routing, document processing, database access.
#
# It deliberately contains NO desktop-control libraries. pyautogui, mss,
# uiautomation, comtypes, pycaw and pyttsx3 are Win32 bindings; on Linux they
# install as useless shims and fail at the first call. Mouse, keyboard, screen
# capture, window control and voice live in the Windows agent
# (sara_desktop/), which drives this API over an authenticated link.

FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl  -> container healthcheck
# libpq -> psycopg2 talks to Postgres/Neon over the wire; without libpq.so.5
#          the import succeeds and the first query raises OSError.
# ffmpeg-> Whisper and the audio pipeline shell out to it.
# tini  -> real PID 1 so SIGTERM reaches uvicorn and shutdown is graceful.
RUN apt-get update && apt-get install --no-install-recommends -y \
        curl \
        libpq5 \
        ffmpeg \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so a code edit does not invalidate the pip layer.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    # pygbag vendors its own typing_extensions.py and puts its support/ dir on
    # sys.path at import time, which shadows the real typing_extensions and
    # breaks fastapi / sqlalchemy / openai for anything imported afterwards.
    # GameForge only ever runs it as `python -m pygbag --build` in a
    # subprocess, so the package is dropped from the image and
    # _module_available("pygbag") reports it as absent -> browser builds are
    # honestly unavailable instead of silently corrupting the interpreter.
    && pip uninstall -y pygbag \
    # pytest ships in the image so the test suite can be run against the exact
    # environment that will serve production — `docker exec sara-backend
    # pytest tests/`. A test that only passes on the developer's machine is
    # the failure mode this whole exercise exists to eliminate.
    && pip install --no-cache-dir pytest

# Application code. sara_desktop/ is copied too because
# app/agents/sara_ultron_agent/core/tools.py resolves the desktop action
# registry at REPO_ROOT/sara_desktop — the tool catalogue is read from it at
# startup even though executing those actions is the Windows agent's job.
COPY backend/ /app/
COPY sara_desktop/ /app/sara_desktop/

# Run as a non-root user. The backend never needs root.
RUN useradd --create-home --shell /bin/bash sara \
    && chown -R sara:sara /app

# The paired-device registry lives here. Creating it IN THE IMAGE, owned by the
# runtime user, is what makes the named volume work: Docker seeds a fresh named
# volume from the image directory, inheriting this ownership. Create it only at
# runtime and Docker makes the volume root-owned, the non-root backend cannot
# write to it, and every pairing is silently lost on the next restart.
RUN mkdir -p /var/lib/sara && chown -R sara:sara /var/lib/sara
USER sara

EXPOSE 8000

ENV PORT=8000 \
    PYTHONPATH=/app

# Real endpoint, real check: /api/health is defined in app/main.py and returns
# 200 once the routers are mounted and the lifespan startup has run.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# exec form + tini: uvicorn becomes PID 1's child and receives SIGTERM, so
# `docker stop` drains in-flight requests instead of cutting them off.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec python run_backend.py --port ${PORT}"]