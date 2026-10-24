// Central backend configuration.
//
// By default EVERYTHING — the SARA AI app, the legacy agent (chat/orchestrate/
// WhatsApp), the Telegram agent (/agent/*) and the desktop agent API + WebSocket
// feed — is served by ONE unified backend (python run_backend.py on port 8000).
//
// Override the base URL with NEXT_PUBLIC_BACKEND_URL, or split individual
// services onto separate hosts/ports via the specific NEXT_PUBLIC_*_URL vars:
//   NEXT_PUBLIC_BACKEND_URL      default "http://127.0.0.1:8000"
//   NEXT_PUBLIC_SARA_API_URL     SARA AI RAG/voice/users API
//   NEXT_PUBLIC_LEGACY_API_URL   legacy chat + Telegram agent
//   NEXT_PUBLIC_DESKTOP_API_URL / NEXT_PUBLIC_DESKTOP_WS_URL  desktop agent
const DEFAULT_BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";

export const SARA_API_URL =
  process.env.NEXT_PUBLIC_SARA_API_URL ?? DEFAULT_BACKEND_URL;
export const LEGACY_API_URL =
  process.env.NEXT_PUBLIC_LEGACY_API_URL ?? DEFAULT_BACKEND_URL;
export const DESKTOP_API_URL =
  process.env.NEXT_PUBLIC_DESKTOP_API_URL ?? DEFAULT_BACKEND_URL;
export const DESKTOP_WS_URL =
  process.env.NEXT_PUBLIC_DESKTOP_WS_URL ??
  `${DEFAULT_BACKEND_URL.replace(/^http/, "ws")}/ws`;
// WhatsApp hotel agent (Grand Stay) — same unified backend, /whatsapp/*
export const WHATSAPP_API_URL =
  process.env.NEXT_PUBLIC_WHATSAPP_API_URL ?? DEFAULT_BACKEND_URL;
// Social Media Agency — same unified backend, /social/*
export const SOCIAL_API_URL =
  process.env.NEXT_PUBLIC_SOCIAL_API_URL ?? DEFAULT_BACKEND_URL;
export const API_GAMEFORGE_URL =
  process.env.NEXT_PUBLIC_GAMEFORGE_API_URL ?? DEFAULT_BACKEND_URL;
export const API_MUSIC_URL =
  process.env.NEXT_PUBLIC_MUSIC_API_URL ?? DEFAULT_BACKEND_URL;
export const API_SCRAPING_URL =
  process.env.NEXT_PUBLIC_SCRAPING_API_URL ?? DEFAULT_BACKEND_URL;
export const VOICE_AGENT_API_URL =
  process.env.NEXT_PUBLIC_VOICE_AGENT_API_URL ?? DEFAULT_BACKEND_URL;
// EduLearner AI — adaptive learning engine, same unified backend, /api/edulearner/*
export const EDULEARNER_API_URL =
  process.env.NEXT_PUBLIC_EDULEARNER_API_URL ?? DEFAULT_BACKEND_URL;
// SARA Ultron — supervisory agent, same unified backend, /ultron/*
export const ULTRON_API_URL =
  process.env.NEXT_PUBLIC_ULTRON_API_URL ?? DEFAULT_BACKEND_URL;

const SARA_API_KEY = process.env.NEXT_PUBLIC_SARA_API_KEY ?? "";
const DESKTOP_API_TOKEN = process.env.NEXT_PUBLIC_DESKTOP_API_TOKEN ?? "";

// Clerk session bridge — registered by <AuthTokenBridge /> (client only).
// When a signed-in user's session token is available it is sent as a Bearer
// credential so the backend scopes data (bots, tasks, memory, ...) to THAT
// user; signed out or server-side the request is unchanged and the shared
// API key resolves to the local operator tenant.
let authTokenGetter: (() => Promise<string | null>) | null = null;

export function setAuthTokenGetter(
  getter: (() => Promise<string | null>) | null,
): void {
  authTokenGetter = getter;
}

/** fetch() against the SARA AI backend with identity attached. */
export async function saraFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers);
  if (SARA_API_KEY) headers.set("X-API-Key", SARA_API_KEY);
  if (!headers.has("Authorization") && authTokenGetter) {
    try {
      const token = await authTokenGetter();
      if (token) headers.set("Authorization", `Bearer ${token}`);
    } catch {
      // Signed out or Clerk unavailable — the API key still applies.
    }
  }
  return fetch(input, { ...init, headers });
}

/** Headers for desktop-agent requests, with the Bearer token attached. */
export function desktopHeaders(
  extra: Record<string, string> = {},
): Record<string, string> {
  const headers: Record<string, string> = { ...extra };
  if (DESKTOP_API_TOKEN) headers["Authorization"] = `Bearer ${DESKTOP_API_TOKEN}`;
  return headers;
}

/** WebSocket URL for the desktop agent, with the token as a query parameter. */
export function desktopWebSocketUrl(): string {
  return DESKTOP_API_TOKEN
    ? `${DESKTOP_WS_URL}?token=${encodeURIComponent(DESKTOP_API_TOKEN)}`
    : DESKTOP_WS_URL;
}