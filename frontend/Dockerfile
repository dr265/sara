# syntax=docker/dockerfile:1
#
# SARA frontend — Next.js production image.
#
# Multi-stage: the build needs the full toolchain (typescript, tailwind,
# postcss), the runtime does not. Copying `standalone` out keeps the final
# image to the server plus node_modules instead of ~1 GB of devDependencies.
#
# NEXT_PUBLIC_* variables are BAKED IN AT BUILD TIME by Next.js — they are
# inlined into the client bundle, not read at request time. That is the single
# most important thing to understand about deploying this image:
#
#   * Changing a NEXT_PUBLIC_* value requires `docker compose build frontend`,
#     not just a restart.
#   * For browser-side calls the value MUST be a URL the *visitor* can reach.
#     So it is either this same origin (via the compose network / reverse
#     proxy) or a public https host — never "backend:8000", which only resolves
#     inside the Docker network and fails in every real browser.
#
# The split that makes desktop control work:
#   NEXT_PUBLIC_BACKEND_URL   -> the reachable cloud backend  (public URL)
#   NEXT_PUBLIC_API_URL       -> the LOCAL machine, for PC control
#                               (http://127.0.0.1:8000 in the visitor's browser)
#   NEXT_PUBLIC_DESKTOP_WS_URL-> ws://127.0.0.1:8000/ws, same reason.
# Pointing the desktop vars at the cloud backend is exactly what made
# "JARVIS lost its powers": the cloud container has no pyautogui, so the
# request executed in a Linux container and could never touch the user's PC.

FROM node:22-bookworm-slim AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
# `npm ci` when a lockfile exists (reproducible), `npm install` otherwise.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

FROM node:22-bookworm-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ ./

# NEXT_PUBLIC_* must be present as ARGs (not just ENV) because Next.js inlines
# them into the client bundle. docker-compose passes these from
# frontend/.env.docker; an unset var simply becomes an empty string, which the
# app already treats as "not configured" rather than crashing.
ARG NEXT_PUBLIC_BACKEND_URL
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_ULTRON_API_URL
ARG NEXT_PUBLIC_DESKTOP_API_URL
ARG NEXT_PUBLIC_DESKTOP_WS_URL
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY
ARG NEXT_PUBLIC_CLERK_SIGN_IN_URL
ARG NEXT_PUBLIC_CLERK_SIGN_UP_URL
ARG NEXT_PUBLIC_GOOGLE_CLIENT_ID
ARG NEXT_PUBLIC_SARA_API_KEY
ARG NEXT_PUBLIC_DESKTOP_API_TOKEN

ENV NEXT_PUBLIC_BACKEND_URL=$NEXT_PUBLIC_BACKEND_URL \
    NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_ULTRON_API_URL=$NEXT_PUBLIC_ULTRON_API_URL \
    NEXT_PUBLIC_DESKTOP_API_URL=$NEXT_PUBLIC_DESKTOP_API_URL \
    NEXT_PUBLIC_DESKTOP_WS_URL=$NEXT_PUBLIC_DESKTOP_WS_URL \
    NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=$NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY \
    NEXT_PUBLIC_CLERK_SIGN_IN_URL=$NEXT_PUBLIC_CLERK_SIGN_IN_URL \
    NEXT_PUBLIC_CLERK_SIGN_UP_URL=$NEXT_PUBLIC_CLERK_SIGN_UP_URL \
    NEXT_PUBLIC_GOOGLE_CLIENT_ID=$NEXT_PUBLIC_GOOGLE_CLIENT_ID \
    NEXT_PUBLIC_SARA_API_KEY=$NEXT_PUBLIC_SARA_API_KEY \
    NEXT_PUBLIC_DESKTOP_API_TOKEN=$NEXT_PUBLIC_DESKTOP_API_TOKEN \
    NEXT_TELEMETRY_DISABLED=1

RUN npm run build

FROM node:22-bookworm-slim AS runner
WORKDIR /app
# HOSTNAME must be 0.0.0.0, NOT left to Docker.
#
# Docker sets HOSTNAME to the container id, and Next's standalone server.js
# binds to process.env.HOSTNAME. That publishes on the container's own
# interface only, so the Docker bridge gets connection refused and every
# request answers 500 "Internal Server Error" - a container that looks
# healthy in its logs and serves nothing at all.
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN apt-get update && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash nextjs

COPY --from=builder --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nextjs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nextjs /app/public ./public

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:3000/" >/dev/null || exit 1

CMD ["node", "server.js"]