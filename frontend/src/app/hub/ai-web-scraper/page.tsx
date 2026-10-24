"use client";

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { API_SCRAPING_URL } from "@/lib/api";

/* ========================================================================== */
/*  AI WEB SCRAPER — SARA Nexus Hub                                           */
/*  Live-wired to the unified SARA backend (POST /api/agent NDJSON stream).   */
/*  All API/service logic is isolated below in the `Api` namespace.           */
/* ========================================================================== */

/* -------------------------------------------------------------------------- */
/*  Types                                                                      */
/* -------------------------------------------------------------------------- */

type ProviderChoice = "auto" | "openai" | "ollama";
type ScrapeMode = "single" | "crawl" | "pagination";
type RunStatus = "running" | "completed" | "failed" | "cancelled";
type View = "scrape" | "history" | "saved" | "settings";

interface ScrapeConfig {
  url?: string;
  urls: string[];
  prompt: string;
  mode: ScrapeMode;
  maxPages: number;
  maxDepth: number;
  sameDomain: boolean;
  timeoutSec: number;
  retries: number;
  delayMs: number;
  llmTimeoutSec: number;
  schemaFields: string[];
  stripNoise: boolean;
  provider: ProviderChoice;
}

interface PageResult {
  url: string;
  finalUrl: string;
  depth: number;
  ok: boolean;
  statusCode: number | null;
  error: string;
  fetchMs: number;
  extractMs: number;
  htmlChars: number;
  htmlUsed: number;
  rowCount: number;
  raw: unknown;
}

type ScrapeRow = Record<string, unknown> & { source_url: string };

interface ScrapeStats {
  urlsRequested?: number;
  urlsAccepted?: number;
  pagesAttempted: number;
  pagesOk: number;
  pagesFailed: number;
  rowsRaw: number;
  rowsFinal: number;
  duplicateUrlsSkipped: number;
  externalSkipped: number;
  fetchMs: number;
  extractMs: number;
  htmlChars: number;
  durationMs: number;
}

interface RunRecord {
  id: string;
  startedAt: string;
  endedAt?: string;
  status: RunStatus;
  config: ScrapeConfig & { model: string };
  pages: PageResult[];
  rows: ScrapeRow[];
  stats: ScrapeStats;
  warnings: string[];
  error?: string;
}

interface LogLine {
  t: string;
  kind: "info" | "ok" | "error" | "warn";
  text: string;
}

interface LiveRun extends RunRecord {
  log: LogLine[];
  progress: { done: number; total: number };
}

type StreamEvent =
  | { type: "status"; phase: string; message: string }
  | { type: "progress"; done: number; total: number }
  | { type: "page"; page: PageResult }
  | { type: "warning"; message: string }
  | { type: "done"; record: RunRecord }
  | { type: "error"; message: string };

interface BackendStatus {
  serverTime: string;
  openai: { configured: boolean; model: string };
  ollama: {
    baseUrl: string;
    model: string;
    embeddingModel: string;
    reachable: boolean;
    installed: string[];
    error: string;
  };
}

interface AppSettings {
  backendBaseUrl: string;
  defaultProvider: ProviderChoice;
  defaultTimeoutSec: number;
  defaultDelayMs: number;
  defaultMaxPages: number;
  defaultStripNoise: boolean;
}

interface FormState {
  urlsRaw: string;
  prompt: string;
  mode: ScrapeMode;
  maxPages: number;
  maxDepth: number;
  sameDomain: boolean;
  timeoutSec: number;
  retries: number;
  delayMs: number;
  llmTimeoutSec: number;
  schemaFields: string;
  stripNoise: boolean;
  provider: ProviderChoice;
}

interface RefineState {
  column: string;
  minOn: boolean;
  minValue: number;
  maxOn: boolean;
  maxValue: number;
  sort: "none" | "asc" | "desc";
  text: string;
}

/* -------------------------------------------------------------------------- */
/*  Constants                                                                  */
/* -------------------------------------------------------------------------- */

const API_BASE = `${API_SCRAPING_URL}/api/agent`;

const DEFAULT_SETTINGS: AppSettings = {
  backendBaseUrl: API_BASE,
  defaultProvider: "auto",
  defaultTimeoutSec: 30,
  defaultDelayMs: 500,
  defaultMaxPages: 5,
  defaultStripNoise: true,
};

const DEFAULT_FORM: FormState = {
  urlsRaw: "",
  prompt: "",
  mode: "single",
  maxPages: 5,
  maxDepth: 2,
  sameDomain: true,
  timeoutSec: 30,
  retries: 2,
  delayMs: 500,
  llmTimeoutSec: 180,
  schemaFields: "",
  stripNoise: true,
  provider: "auto",
};

const ZERO_STATS: ScrapeStats = {
  pagesAttempted: 0,
  pagesOk: 0,
  pagesFailed: 0,
  rowsRaw: 0,
  rowsFinal: 0,
  duplicateUrlsSkipped: 0,
  externalSkipped: 0,
  fetchMs: 0,
  extractMs: 0,
  htmlChars: 0,
  durationMs: 0,
};

const EXAMPLE_PROMPTS = [
  "List all products with their title, price and rating.",
  "Find all products priced under ₹50,000 and list title, price and rating.",
  "Extract every article headline with its author and publication date.",
  "Summarize the main features described on this page.",
  "List every job opening with title, location and salary range.",
  "Extract all FAQ questions with their answers.",
];

const MODE_META: Record<ScrapeMode, { label: string; hint: string }> = {
  single: { label: "Single page", hint: "Extract from one URL" },
  crawl: { label: "Crawl", hint: "Follow same-scope links" },
  pagination: { label: "Pagination", hint: "Follow next-page links" },
};

/* -------------------------------------------------------------------------- */
/*  Utilities                                                                  */
/* -------------------------------------------------------------------------- */

function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

function uid(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `id_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
  }
}

function errMsg(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

function fmtMs(ms: number): string {
  if (!Number.isFinite(ms)) return "0s";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

function fmtBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function fmtClock(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString(undefined, {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return "";
  }
}

const MONEY_SYMBOLS = ["₹", "₨", "$", "€", "£", "¥", "Rs", "INR", "USD", "EUR"];

function parseNumeric(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === "boolean") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  let text = String(value);
  for (const symbol of MONEY_SYMBOLS) text = text.split(symbol).join("");
  const match = /[-+]?\d[\d,]*(?:\.\d+)?/.exec(text.replace(/\s+/g, ""));
  if (!match) return null;
  const parsed = parseFloat(match[0].replace(/,/g, ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function validateUrl(raw: string): string | null {
  const url = raw.trim();
  if (!url) return "Enter a target URL.";
  if (!/^https?:\/\//i.test(url)) return "URL must start with http:// or https://";
  try {
    const parsed = new URL(url);
    if (!parsed.hostname) return "URL has no hostname.";
  } catch {
    return "That doesn't look like a valid URL.";
  }
  if (url.length > 2048) return "URL is too long.";
  return null;
}

const MAX_URLS = 20;

function parseUrlList(raw: string): Array<{ url: string; error: string | null }> {
  const seen = new Set<string>();
  const entries: Array<{ url: string; error: string | null }> = [];
  for (const line of raw.split(/\n/)) {
    const url = line.trim();
    if (!url) continue;
    const key = url.toLowerCase();
    if (seen.has(key)) {
      entries.push({ url, error: "duplicate — will be ignored" });
      continue;
    }
    seen.add(key);
    entries.push({ url, error: validateUrl(url) });
  }
  return entries;
}

function deriveColumns(rows: ScrapeRow[]): string[] {
  const columns = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (key !== "source_url") columns.add(key);
    }
  }
  return Array.from(columns).sort();
}

function rowsToCSV(rows: ScrapeRow[]): string {
  const columns = deriveColumns(rows);
  const header = ["source_url", ...columns];
  const esc = (v: unknown) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [header.map(esc).join(",")];
  for (const row of rows) {
    lines.push(header.map((c) => esc(row[c] ?? "")).join(","));
  }
  return lines.join("\n");
}

function rowsToMarkdown(rows: ScrapeRow[]): string {
  const columns = deriveColumns(rows);
  const header = ["source_url", ...columns];
  const clean = (v: unknown) =>
    (v === null || v === undefined ? "" : String(v))
      .replace(/\|/g, "\\|")
      .replace(/\n/g, "<br>");
  const lines = [
    `| ${header.join(" | ")} |`,
    `| ${header.map(() => "---").join(" | ")} |`,
  ];
  for (const row of rows) {
    lines.push(`| ${header.map((c) => clean(row[c] ?? "")).join(" | ")} |`);
  }
  return lines.join("\n");
}

function rowsToTXT(rows: ScrapeRow[]): string {
  const columns = deriveColumns(rows);
  const header = ["source_url", ...columns];
  const clean = (v: unknown) =>
    (v === null || v === undefined ? "" : String(v)).replace(/[\t\n]/g, " ");
  const lines = [header.join("\t")];
  for (const row of rows) {
    lines.push(header.map((c) => clean(row[c] ?? "")).join("\t"));
  }
  return lines.join("\n");
}

function downloadFile(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      return true;
    } catch {
      return false;
    }
  }
}

/* -------------------------------------------------------------------------- */
/*  API service layer (isolated)                                               */
/* -------------------------------------------------------------------------- */

class AgentApiError extends Error {}

function apiBaseUrl(): string {
  try {
    const raw = window.localStorage.getItem("scrapework.settings.v1");
    if (raw) {
      const settings = JSON.parse(raw) as Partial<AppSettings>;
      if (settings.backendBaseUrl) return settings.backendBaseUrl;
    }
  } catch {
    /* ignore */
  }
  return API_BASE;
}

const Api = {
  async status(signal?: AbortSignal): Promise<BackendStatus> {
    const res = await fetch(apiBaseUrl(), {
      signal,
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`Status endpoint returned HTTP ${res.status}`);
    return (await res.json()) as BackendStatus;
  },

  async scrape(
    config: ScrapeConfig,
    onEvent: (evt: StreamEvent) => void,
    signal: AbortSignal,
  ): Promise<RunRecord> {
    const res = await fetch(apiBaseUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
      signal,
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const text = await res.text();
        if (text.trim()) detail = text.trim().slice(0, 300);
      } catch {
        /* keep status detail */
      }
      throw new Error(detail);
    }
    if (!res.body) throw new Error("Streaming response is not supported here.");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let record: RunRecord | null = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx = buffer.indexOf("\n");
      while (idx >= 0) {
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 1);
        if (line) {
          try {
            const evt = JSON.parse(line) as StreamEvent;
            onEvent(evt);
            if (evt.type === "done") record = evt.record;
            if (evt.type === "error") throw new AgentApiError(evt.message);
          } catch (err) {
            if (err instanceof AgentApiError) throw err;
            /* ignore malformed event lines */
          }
        }
        idx = buffer.indexOf("\n");
      }
    }
    if (!record) throw new Error("The stream ended without a result record.");
    return record;
  },

  async history(): Promise<RunRecord[]> {
    const res = await fetch(`${apiBaseUrl()}/history`, { cache: "no-store" });
    if (!res.ok) throw new Error(`History endpoint returned HTTP ${res.status}`);
    const data = (await res.json()) as { runs: RunRecord[] };
    return data.runs ?? [];
  },
};

/* -------------------------------------------------------------------------- */
/*  Hooks                                                                      */
/* -------------------------------------------------------------------------- */

function useLocalStorage<T>(
  key: string,
  initial: T,
): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    if (typeof window === "undefined") return initial;
    try {
      const raw = window.localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* storage may be unavailable — app still works in memory */
    }
  }, [key, value]);
  return [value, setValue];
}

function useElapsed(sinceIso?: string, running = false): number {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!sinceIso) {
      setElapsed(0);
      return;
    }
    const tick = () => {
      try {
        setElapsed(Math.max(0, Date.now() - new Date(sinceIso).getTime()));
      } catch {
        setElapsed(0);
      }
    };
    tick();
    if (running) {
      const id = window.setInterval(tick, 500);
      return () => window.clearInterval(id);
    }
  }, [sinceIso, running]);
  return elapsed;
}

function useBackendStatus() {
  const [status, setStatus] = useState<BackendStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const refresh = useCallback(async () => {
    setChecking(true);
    try {
      setStatus(await Api.status());
      setError(null);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 30_000);
    return () => window.clearInterval(id);
  }, [refresh]);

  return { status, error, checking, refresh };
}

function useScrapeRunner(onFinished: (record: RunRecord) => void) {
  const [run, setRun] = useState<LiveRun | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  const start = useCallback(
    async (config: ScrapeConfig) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      const initial: LiveRun = {
        id: uid(),
        startedAt: new Date().toISOString(),
        status: "running",
        config: { ...config, model: "…" },
        pages: [],
        rows: [],
        stats: { ...ZERO_STATS, urlsRequested: config.urls.length, urlsAccepted: config.urls.length },
        warnings: [],
        log: [],
        progress: { done: 0, total: config.mode === "single" ? 1 : config.maxPages },
      };
      setRun(initial);

      const applyEvent = (evt: StreamEvent) => {
        setRun((prev) => {
          if (!prev || prev.status !== "running") return prev;
          const stamp = new Date().toISOString();
          switch (evt.type) {
            case "status":
              return {
                ...prev,
                log: [...prev.log, { t: stamp, kind: "info", text: evt.message }],
              };
            case "progress":
              return { ...prev, progress: { done: evt.done, total: evt.total } };
            case "page": {
              const page = evt.page;
              return {
                ...prev,
                pages: [...prev.pages, page],
                stats: {
                  ...prev.stats,
                  pagesAttempted: prev.pages.length + 1,
                  pagesOk: prev.stats.pagesOk + (page.ok ? 1 : 0),
                  fetchMs: prev.stats.fetchMs + page.fetchMs,
                  extractMs: prev.stats.extractMs + page.extractMs,
                  htmlChars: prev.stats.htmlChars + page.htmlChars,
                },
                log: [
                  ...prev.log,
                  {
                    t: stamp,
                    kind: page.ok ? "ok" : "error",
                    text: page.ok
                      ? `${page.finalUrl} — HTTP ${page.statusCode} · ${page.rowCount} rows · ${(page.extractMs / 1000).toFixed(1)}s`
                      : `${page.finalUrl} — ${page.error || "failed"}`,
                  },
                ],
              };
            }
            case "warning":
              return {
                ...prev,
                warnings: [...prev.warnings, evt.message],
                log: [...prev.log, { t: stamp, kind: "warn", text: evt.message }],
              };
            case "done":
              return {
                ...prev,
                ...evt.record,
                status: "completed",
                log: [
                  ...prev.log,
                  {
                    t: stamp,
                    kind: "ok",
                    text: `Complete — ${evt.record.stats.pagesAttempted} page(s) · ${evt.record.stats.rowsFinal} rows · ${fmtMs(evt.record.stats.durationMs)}`,
                  },
                ],
              };
            case "error":
              return {
                ...prev,
                status: "failed",
                error: evt.message,
                endedAt: stamp,
                log: [...prev.log, { t: stamp, kind: "error", text: evt.message }],
              };
            default:
              return prev;
          }
        });
      };

      try {
        const record = await Api.scrape(config, applyEvent, controller.signal);
        setRun((prev) => (prev ? { ...prev, ...record, status: "completed" } : prev));
        onFinished(record);
      } catch (err) {
        if (controller.signal.aborted) {
          setRun((prev) =>
            prev && prev.status === "running"
              ? {
                  ...prev,
                  status: "cancelled",
                  endedAt: new Date().toISOString(),
                  log: [...prev.log, { t: new Date().toISOString(), kind: "warn", text: "Run cancelled by user." }],
                }
              : prev,
          );
        } else {
          const message = errMsg(err);
          setRun((prev) =>
            prev && prev.status === "running"
              ? {
                  ...prev,
                  status: "failed",
                  error: message,
                  endedAt: new Date().toISOString(),
                  log: [
                    ...prev.log,
                    { t: new Date().toISOString(), kind: "error", text: message },
                  ],
                }
              : prev,
          );
        }
      }
    },
    [onFinished],
  );

  const cancel = useCallback(() => {
    controllerRef.current?.abort();
  }, []);

  const reset = useCallback(() => setRun(null), []);

  return { run, start, cancel, reset };
}

/* -------------------------------------------------------------------------- */
/*  Icons (inline, stroke-based — no external assets)                          */
/* -------------------------------------------------------------------------- */

interface IconProps {
  size?: number;
  className?: string;
}

type IconComponent = (props: IconProps) => React.ReactElement;

function makeIcon(paths: React.ReactNode): IconComponent {
  return function Icon({ size = 16, className = "" }: IconProps) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.6}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={className}
        aria-hidden="true"
      >
        {paths}
      </svg>
    );
  };
}

const IconBrand = makeIcon(
  <>
    <rect x="3.5" y="3.5" width="17" height="17" rx="4.5" />
    <path d="M9 9h6v6H9z" />
    <path d="M12 3.5v2M12 18.5v2M3.5 12h2M18.5 12h2" />
    <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
  </>,
);
const IconPlus = makeIcon(<path d="M12 5v14M5 12h14" />);
const IconHistory = makeIcon(
  <>
    <path d="M3.5 12a8.5 8.5 0 1 0 2.5-6L3.5 8.5" />
    <path d="M3.5 3.5v5h5" />
    <path d="M12 7.5V12l3 2" />
  </>,
);
const IconBookmark = makeIcon(
  <path d="M6.5 4.5h11v15L12 16.2l-5.5 3.3v-15z" />,
);
const IconSettings = makeIcon(
  <>
    <path d="M4 7h9M17.5 7h2.5M4 17h2.5M11 17h9" />
    <circle cx="15" cy="7" r="2.2" />
    <circle cx="8.7" cy="17" r="2.2" />
  </>,
);
const IconPlay = makeIcon(<path d="M7 5.5v13l11-6.5-11-6.5z" />);
const IconStop = makeIcon(<rect x="6.5" y="6.5" width="11" height="11" rx="2" />);
const IconCheck = makeIcon(<path d="M4.5 12.5l5 5 10-11" />);
const IconX = makeIcon(<path d="M6 6l12 12M18 6L6 18" />);
const IconChevronDown = makeIcon(<path d="M6 9l6 6 6-6" />);
const IconDownload = makeIcon(
  <>
    <path d="M12 4v11" />
    <path d="M7.5 11l4.5 4.5L16.5 11" />
    <path d="M4 19.5h16" />
  </>,
);
const IconCopy = makeIcon(
  <>
    <rect x="9" y="9" width="11" height="11" rx="2.5" />
    <path d="M5 15H4.5A1.5 1.5 0 0 1 3 13.5v-9A1.5 1.5 0 0 1 4.5 3h9A1.5 1.5 0 0 1 15 4.5V5" />
  </>,
);
const IconAlert = makeIcon(
  <>
    <path d="M12 3.5L21 19H3l9-15.5z" />
    <path d="M12 10v4" />
    <path d="M12 16.8v.4" />
  </>,
);
const IconRefresh = makeIcon(
  <>
    <path d="M20 11.5A8 8 0 1 0 18.6 16" />
    <path d="M20 5.5v6h-6" />
  </>,
);
const IconExternal = makeIcon(
  <>
    <path d="M14 4h6v6" />
    <path d="M20 4l-9 9" />
    <path d="M19 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 18.5v-11A1.5 1.5 0 0 1 6.5 6H11" />
  </>,
);
const IconCode = makeIcon(
  <>
    <path d="M8 6.5L3 12l5 5.5" />
    <path d="M16 6.5L21 12l-5 5.5" />
    <path d="M13.5 4l-3 16" />
  </>,
);
const IconLink = makeIcon(
  <>
    <path d="M10 14a4.5 4.5 0 0 0 6.4.4l3-3a4.5 4.5 0 1 0-6.4-6.4l-1.6 1.6" />
    <path d="M14 10a4.5 4.5 0 0 0-6.4-.4l-3 3a4.5 4.5 0 1 0 6.4 6.4l1.6-1.6" />
  </>,
);
const IconDoc = makeIcon(
  <>
    <path d="M6 3.5h8l4 4v13h-12v-17z" />
    <path d="M14 3.5v4h4" />
  </>,
);
const IconLayers = makeIcon(
  <>
    <path d="M12 3.5L21 8l-9 4.5L3 8l9-4.5z" />
    <path d="M3 12.5l9 4.5 9-4.5" />
    <path d="M3 17l9 4.5 9-4.5" />
  </>,
);
const IconChevronsRight = makeIcon(
  <>
    <path d="M6.5 6.5L11.5 12l-5 5.5" />
    <path d="M13.5 6.5L18.5 12l-5 5.5" />
  </>,
);
const IconFilter = makeIcon(
  <path d="M4 6h16M7 12h10M10 18h4" />,
);
const IconTrash = makeIcon(
  <>
    <path d="M4.5 6.5h15" />
    <path d="M9 6.5V5a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 5v1.5" />
    <path d="M6.5 6.5l1 13.5h9l1-13.5" />
  </>,
);
const IconTerminal = makeIcon(
  <>
    <rect x="3.5" y="4.5" width="17" height="15" rx="3" />
    <path d="M7 9.5l3 2.5-3 2.5M13 14.5h4" />
  </>,
);
const IconGlobe = makeIcon(
  <>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M3.5 12h17M12 3.5c2.6 2.3 4 5.2 4 8.5s-1.4 6.2-4 8.5c-2.6-2.3-4-5.2-4-8.5s1.4-6.2 4-8.5z" />
  </>,
);
const IconTable = makeIcon(
  <>
    <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
    <path d="M3.5 9.5h17M9.5 9.5v10" />
  </>,
);
const IconSpinner = makeIcon(
  <path d="M12 3.5a8.5 8.5 0 1 0 8.5 8.5" className="origin-center animate-spin" />,
);

/* -------------------------------------------------------------------------- */
/*  Primitives                                                                 */
/* -------------------------------------------------------------------------- */

const CSS_GLOBALS = `
/* injected hero styles */
@import url('https://fonts.googleapis.com/css2?family=Geist&display=swap');
@import url('https://api.fontshare.com/v2/css?f[]=general-sans@400,500,600,700&display=swap');

.hero-wrapper {
  --bg-color: hsl(260, 87%, 3%);
  --fg-color: hsl(40, 6%, 95%);
  --sub-color: hsl(40, 6%, 82%);
  background-color: var(--bg-color);
  color: var(--fg-color);
  font-family: 'Geist', sans-serif;
}
.hero-wrapper h1 {
  font-family: 'General Sans', sans-serif;
}

.liquid-glass {
  background: rgba(255, 255, 255, 0.01);
  background-blend-mode: luminosity;
  backdrop-filter: blur(4px);
  border: none;
  box-shadow: inset 0 1px 1px rgba(255, 255, 255, 0.1);
  position: relative;
  overflow: hidden;
}
.liquid-glass::before {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: inherit;
  padding: 1.4px;
  background: linear-gradient(180deg, rgba(255,255,255,0.45) 0%, rgba(255,255,255,0.15) 20%, rgba(255,255,255,0) 40%, rgba(255,255,255,0) 60%, rgba(255,255,255,0.15) 80%, rgba(255,255,255,0.45) 100%);
  -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  -webkit-mask-composite: xor;
  mask-composite: exclude;
  pointer-events: none;
}

.marquee-container {
  display: flex;
  overflow: hidden;
  user-select: none;
  gap: 4rem;
}
.marquee-content {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 4rem;
  animation: marquee-scroll 20s linear infinite;
}
@keyframes marquee-scroll {
  from { transform: translateX(0%); }
  to { transform: translateX(-100%); }
}

.heroSecondary {
  background: rgba(255,255,255,0.1);
  border: 1px solid rgba(255,255,255,0.2);
  color: var(--fg-color);
  border-radius: 9999px;
  transition: all 0.2s;
  font-weight: 500;
}
.heroSecondary:hover {
  background: rgba(255,255,255,0.15);
}

:root { color-scheme: dark; }
::selection { background: rgba(125, 140, 255, 0.28); }
.sg-scroll { scrollbar-width: thin; scrollbar-color: rgba(148,163,184,.22) transparent; }
.sg-scroll::-webkit-scrollbar { width: 8px; height: 8px; }
.sg-scroll::-webkit-scrollbar-thumb {
  background: rgba(148,163,184,.18); border-radius: 8px;
  border: 2px solid transparent; background-clip: content-box;
}
.sg-scroll::-webkit-scrollbar-thumb:hover {
  background: rgba(148,163,184,.34); background-clip: content-box; border: 2px solid transparent;
}
.sg-grid-bg {
  background-image:
    linear-gradient(rgba(148,163,184,.045) 1px, transparent 1px),
    linear-gradient(90deg, rgba(148,163,184,.045) 1px, transparent 1px);
  background-size: 44px 44px;
  -webkit-mask-image: radial-gradient(ellipse 90% 70% at 50% 0%, black 25%, transparent 78%);
  mask-image: radial-gradient(ellipse 90% 70% at 50% 0%, black 25%, transparent 78%);
}
@keyframes sg-fade-up {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}
.sg-fade-up { animation: sg-fade-up .45s cubic-bezier(.2,.7,.3,1) both; }
.sg-num { font-variant-numeric: tabular-nums; }
.sg-mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; }
@keyframes sg-pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
.sg-live-dot { animation: sg-pulse 2.2s ease-in-out infinite; }
@media (prefers-reduced-motion: reduce) {
  .sg-fade-up { animation: none; }
  .sg-live-dot { animation: none; }
  * { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
}
`;

const INPUT_CLS =
  "w-full rounded-lg border border-white/[0.08] bg-[#0c1019]/80 px-3 py-2 text-sm text-slate-100 " +
  "placeholder:text-slate-500 outline-none transition " +
  "focus:border-sky-300/30 focus:bg-[#10151f]/90 focus:ring-2 focus:ring-sky-300/15 " +
  "aria-[invalid=true]:border-rose-400/40 aria-[invalid=true]:focus:ring-rose-400/15";

const LABEL_CLS =
  "mb-1.5 block text-[11px] font-medium uppercase tracking-[0.09em] text-slate-400";

interface GlassProps {
  children: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}

function Glass({ children, className, style }: GlassProps) {
  return (
    <div
      style={style}
      className={cn(
        "relative overflow-hidden rounded-2xl border border-white/[0.07] bg-[#111623]/70 backdrop-blur-2xl",
        "shadow-[inset_0_1px_0_rgba(255,255,255,0.05),0_24px_60px_-28px_rgba(0,0,0,0.85)]",
        className,
      )}
    >
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-8 top-0 h-px bg-gradient-to-r from-transparent via-white/[0.12] to-transparent"
      />
      {children}
    </div>
  );
}

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  loading?: boolean;
  icon?: IconComponent;
}

function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  icon: Icon,
  children,
  className,
  disabled,
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={cn(
        "inline-flex select-none items-center justify-center gap-2 rounded-lg font-medium transition",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/50 focus-visible:ring-offset-2 focus-visible:ring-offset-[#0B0E14]",
        "disabled:pointer-events-none disabled:opacity-40",
        size === "md" ? "px-4 py-2 text-sm" : "px-2.5 py-1.5 text-xs",
        variant === "primary" &&
          "bg-[#e9edf5] text-[#0b0e14] shadow-[inset_0_1px_0_rgba(255,255,255,0.7),0_10px_24px_-10px_rgba(233,237,245,0.35)] hover:bg-white active:translate-y-px",
        variant === "secondary" &&
          "border border-white/[0.09] bg-white/[0.05] text-slate-200 hover:border-white/[0.16] hover:bg-white/[0.09] active:translate-y-px",
        variant === "ghost" &&
          "text-slate-400 hover:bg-white/[0.06] hover:text-slate-100",
        variant === "danger" &&
          "border border-rose-400/20 bg-rose-500/[0.08] text-rose-200 hover:bg-rose-500/[0.16]",
        className,
      )}
      {...rest}
    >
      {loading ? <IconSpinner size={size === "md" ? 15 : 13} /> : Icon ? <Icon size={size === "md" ? 15 : 13} /> : null}
      {children}
    </button>
  );
}

function IconButton({
  label,
  icon: Icon,
  onClick,
  className,
  disabled,
  variant = "ghost",
}: {
  label: string;
  icon: IconComponent;
  onClick?: () => void;
  className?: string;
  disabled?: boolean;
  variant?: "ghost" | "danger";
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex h-8 w-8 items-center justify-center rounded-lg transition",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/50",
        "disabled:pointer-events-none disabled:opacity-40",
        variant === "ghost"
          ? "text-slate-400 hover:bg-white/[0.07] hover:text-slate-100"
          : "text-rose-300/70 hover:bg-rose-500/[0.12] hover:text-rose-200",
        className,
      )}
    >
      <Icon size={15} />
    </button>
  );
}

function StatusDot({
  tone,
  live = false,
}: {
  tone: "ok" | "warn" | "error" | "idle";
  live?: boolean;
}) {
  const color =
    tone === "ok"
      ? "bg-emerald-300/90 shadow-[0_0_8px_rgba(110,231,183,0.5)]"
      : tone === "warn"
        ? "bg-amber-300/90 shadow-[0_0_8px_rgba(252,211,77,0.4)]"
        : tone === "error"
          ? "bg-rose-400/90 shadow-[0_0_8px_rgba(251,113,133,0.5)]"
          : "bg-slate-500/80";
  return (
    <span
      aria-hidden
      className={cn("inline-block h-1.5 w-1.5 shrink-0 rounded-full", color, live && "sg-live-dot")}
    />
  );
}

function Badge({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "ok" | "error" | "warn" | "accent";
}) {
  const tones: Record<string, string> = {
    neutral: "border-white/[0.09] bg-white/[0.05] text-slate-300",
    ok: "border-emerald-400/20 bg-emerald-400/[0.08] text-emerald-200",
    error: "border-rose-400/20 bg-rose-400/[0.08] text-rose-200",
    warn: "border-amber-400/20 bg-amber-400/[0.08] text-amber-200",
    accent: "border-sky-300/20 bg-sky-400/[0.08] text-sky-200",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] font-medium",
        tones[tone],
      )}
    >
      {children}
    </span>
  );
}

function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string | null;
  children: React.ReactNode;
}) {
  return (
    <div>
      <span className={LABEL_CLS}>{label}</span>
      {children}
      {error ? (
        <p role="alert" className="mt-1.5 text-xs text-rose-300/90">
          {error}
        </p>
      ) : hint ? (
        <p className="mt-1.5 text-xs text-slate-500">{hint}</p>
      ) : null}
    </div>
  );
}

function Segmented<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
  className,
}: {
  options: Array<{ value: T; label: React.ReactNode; hint?: string }>;
  value: T;
  onChange: (value: T) => void;
  ariaLabel: string;
  className?: string;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn(
        "inline-flex items-center gap-0.5 rounded-xl border border-white/[0.07] bg-white/[0.03] p-1",
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={active}
            title={option.hint}
            onClick={() => onChange(option.value)}
            className={cn(
              "rounded-lg px-3 py-1.5 text-xs font-medium transition",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40",
              active
                ? "bg-white/[0.09] text-slate-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_2px_rgba(0,0,0,0.4)]"
                : "text-slate-400 hover:text-slate-200",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full border transition",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/50",
        checked
          ? "border-sky-300/40 bg-sky-400/25"
          : "border-white/10 bg-white/[0.06]",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "absolute top-1/2 h-3.5 w-3.5 -translate-y-1/2 rounded-full transition-all",
          checked
            ? "left-[calc(100%-1.1rem)] bg-sky-100"
            : "left-0.5 bg-slate-400",
        )}
      />
    </button>
  );
}

function EmptyState({
  icon: Icon,
  title,
  body,
  action,
}: {
  icon: IconComponent;
  title: string;
  body: string;
  action?: React.ReactNode;
}) {
  return (
    <Glass className="flex flex-col items-center px-8 py-16 text-center">
      <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-xl border border-white/[0.08] bg-white/[0.04] text-slate-400">
        <Icon size={22} />
      </div>
      <h3 className="text-sm font-semibold text-slate-200">{title}</h3>
      <p className="mt-1.5 max-w-sm text-[13px] leading-relaxed text-slate-400">
        {body}
      </p>
      {action ? <div className="mt-5">{action}</div> : null}
    </Glass>
  );
}

/* -------------------------------------------------------------------------- */
/*  Sidebar                                                                    */
/* -------------------------------------------------------------------------- */

function ProviderCard({
  status,
  error,
  checking,
  onRefresh,
}: {
  status: BackendStatus | null;
  error: string | null;
  checking: boolean;
  onRefresh: () => void;
}) {
  return (
    <Glass className="p-4">
      <div className="mb-3 flex items-center justify-between">
        <span className={LABEL_CLS + " mb-0"}>Providers</span>
        <IconButton
          label="Refresh provider status"
          icon={IconRefresh}
          onClick={onRefresh}
          disabled={checking}
        />
      </div>
      {error ? (
        <p className="text-xs leading-relaxed text-rose-300/80">
          Backend unreachable — {error}
        </p>
      ) : status ? (
        <ul className="space-y-2.5">
          <li className="flex items-start gap-2">
            <StatusDot tone={status.openai.configured ? "ok" : "idle"} />
            <div className="min-w-0">
              <p className="text-xs font-medium text-slate-200">Cloud LLM</p>
              <p className="truncate text-[11px] text-slate-400">
                {status.openai.configured
                  ? `Configured · ${status.openai.model}`
                  : "Not configured (.env)"}
              </p>
            </div>
          </li>
          <li className="flex items-start gap-2">
            <StatusDot
              tone={status.ollama.reachable ? "ok" : "idle"}
              live={status.ollama.reachable}
            />
            <div className="min-w-0">
              <p className="text-xs font-medium text-slate-200">Ollama</p>
              <p className="truncate text-[11px] text-slate-400">
                {status.ollama.reachable
                  ? `Reachable · ${status.ollama.installed.length} model(s)`
                  : `Unreachable · ${status.ollama.baseUrl.replace(/^https?:\/\//, "")}`}
              </p>
            </div>
          </li>
        </ul>
      ) : (
        <p className="flex items-center gap-2 text-xs text-slate-500">
          <IconSpinner size={13} /> Checking providers…
        </p>
      )}
    </Glass>
  );
}

interface SidebarProps {
  view: View;
  onNavigate: (view: View) => void;
  historyCount: number;
  savedCount: number;
  status: BackendStatus | null;
  statusError: string | null;
  statusChecking: boolean;
  onRefreshStatus: () => void;
}

const NAV_ITEMS: Array<{ view: View; label: string; icon: IconComponent }> = [
  { view: "scrape", label: "New scrape", icon: IconPlus },
  { view: "history", label: "History", icon: IconHistory },
  { view: "saved", label: "Saved results", icon: IconBookmark },
  { view: "settings", label: "Settings", icon: IconSettings },
];

function Sidebar(props: SidebarProps) {
  const { view, onNavigate, historyCount, savedCount } = props;
  return (
    <aside className="sticky top-0 hidden h-screen w-[264px] shrink-0 flex-col border-r border-white/[0.06] bg-[#0d1119]/60 px-4 py-5 backdrop-blur-xl lg:flex">
      {/* Brand */}
      <div className="flex items-center gap-3 px-2">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl border border-white/[0.1] bg-gradient-to-b from-white/[0.09] to-white/[0.02] text-sky-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_8px_20px_-8px_rgba(0,0,0,0.8)]">
          <IconBrand size={19} />
        </div>
        <div>
          <p className="text-[15px] font-semibold tracking-tight text-slate-100">
            AI Web Scrapper
          </p>
          <p className="text-[11px] text-slate-500">Web scraping agent</p>
        </div>
      </div>

      {/* Navigation */}
      <nav aria-label="Main" className="mt-7 flex flex-col gap-1">
        {NAV_ITEMS.map((item) => {
          const active = view === item.view;
          const count =
            item.view === "history" ? historyCount : item.view === "saved" ? savedCount : 0;
          return (
            <button
              key={item.view}
              type="button"
              aria-current={active ? "page" : undefined}
              onClick={() => onNavigate(item.view)}
              className={cn(
                "group flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] font-medium transition",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40",
                active
                  ? "bg-white/[0.07] text-slate-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.07)]"
                  : "text-slate-400 hover:bg-white/[0.04] hover:text-slate-200",
              )}
            >
              <item.icon
                size={15}
                className={active ? "text-sky-300/90" : "text-slate-500 group-hover:text-slate-300"}
              />
              <span className="flex-1 text-left">{item.label}</span>
              {count > 0 ? (
                <span className="sg-num rounded-md bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-slate-400">
                  {count}
                </span>
              ) : null}
            </button>
          );
        })}
      </nav>

      <div className="mt-auto space-y-4">
        <ProviderCard
          status={props.status}
          error={props.statusError}
          checking={props.statusChecking}
          onRefresh={props.onRefreshStatus}
        />
        <p className="px-2 text-[10px] leading-relaxed text-slate-600">
          Keys are read server-side from backend/.env — never entered or exposed
          in this UI. Scraped content is untrusted data.
        </p>
      </div>
    </aside>
  );
}

/* -------------------------------------------------------------------------- */
/*  Execution console                                                          */
/* -------------------------------------------------------------------------- */

function ConsolePanel({ run }: { run: LiveRun }) {
  const elapsed = useElapsed(run.startedAt, run.status === "running");
  const progress =
    run.progress.total > 0
      ? Math.min(1, run.progress.done / run.progress.total)
      : 0;
  const logRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [run.log.length]);

  const kindIcon: Record<LogLine["kind"], React.ReactElement> = {
    info: <span className="text-slate-500">·</span>,
    ok: <IconCheck size={11} className="mt-[3px] text-emerald-300/80" />,
    error: <IconX size={11} className="mt-[3px] text-rose-300/80" />,
    warn: <IconAlert size={11} className="mt-[3px] text-amber-300/80" />,
  };

  return (
    <Glass className="sg-fade-up" style={{ animationDelay: "60ms" }}>
      <div className="flex items-center gap-3 border-b border-white/[0.06] px-4 py-3">
        <IconTerminal size={15} className="text-slate-400" />
        <p className="text-xs font-semibold uppercase tracking-[0.09em] text-slate-300">
          Execution log
        </p>
        <div className="ml-auto flex items-center gap-3">
          {run.status === "running" && (
            <Badge tone="accent">
              <StatusDot tone="ok" live /> Running
            </Badge>
          )}
          <span className="sg-num text-[11px] text-slate-500">
            {fmtMs(elapsed)}
          </span>
        </div>
      </div>
      <div className="h-0.5 w-full bg-white/[0.04]">
        <div
          className="h-full bg-gradient-to-r from-sky-400/70 to-indigo-400/70 transition-all duration-300"
          style={{ width: `${Math.round(progress * 100)}%` }}
        />
      </div>
      <div
        ref={logRef}
        role="log"
        aria-live="polite"
        className="sg-scroll max-h-60 space-y-1.5 overflow-y-auto px-4 py-3"
      >
        {run.log.length === 0 ? (
          <p className="text-xs text-slate-500">Waiting for the pipeline…</p>
        ) : (
          run.log.map((line, index) => (
            <p
              key={index}
              className="sg-mono flex items-start gap-2 text-[11.5px] leading-relaxed"
            >
              <span className="sg-num shrink-0 pt-px text-slate-600">
                {fmtClock(line.t)}
              </span>
              {kindIcon[line.kind]}
              <span
                className={cn(
                  "break-all",
                  line.kind === "error"
                    ? "text-rose-300/90"
                    : line.kind === "warn"
                      ? "text-amber-200/90"
                      : line.kind === "ok"
                        ? "text-emerald-200/80"
                        : "text-slate-300",
                )}
              >
                {line.text}
              </span>
            </p>
          ))
        )}
      </div>
    </Glass>
  );
}

/* -------------------------------------------------------------------------- */
/*  Workspace                                                                  */
/* -------------------------------------------------------------------------- */

function Workspace({
  form,
  setForm,
  valid,
  backend,
  onRun,
  onCancel,
  running,
  onReset,
}: {
  form: FormState;
  setForm: (patch: Partial<FormState>) => void;
  valid: { urls: boolean; prompt: boolean };
  backend: BackendStatus | null;
  onRun: () => void;
  onCancel: () => void;
  running: boolean;
  onReset: () => void;
}) {
  const urlEntries = useMemo(() => parseUrlList(form.urlsRaw), [form.urlsRaw]);
  const validUrlCount = urlEntries.filter((e) => !e.error || e.error.startsWith("duplicate")).length;
  const invalidCount = urlEntries.filter((e) => e.error && !e.error.startsWith("duplicate")).length;
  const promptLength = form.prompt.trim().length;
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const providerHint = useMemo(() => {
    if (!backend) return "Checking provider availability…";
    if (form.provider === "openai") {
      return backend.openai.configured
        ? `Cloud LLM configured · ${backend.openai.model}`
        : "OPENAI_API_KEY / LLM_API_KEY is not set on the server (.env) — cloud provider unavailable.";
    }
    if (form.provider === "ollama") {
      return backend.ollama.reachable
        ? `Ollama reachable · ${backend.ollama.installed.length} model(s) installed`
        : `Ollama unreachable at ${backend.ollama.baseUrl} — start \`ollama serve\`.`;
    }
    const resolved = backend.openai.configured
      ? `cloud (${backend.openai.model})`
      : backend.ollama.reachable
        ? "Ollama"
        : "no backend available";
    return `Auto resolves to ${resolved}.`;
  }, [backend, form.provider]);

  const providerTone: "ok" | "warn" =
    !backend
      ? "warn"
      : form.provider === "openai"
        ? backend.openai.configured
          ? "ok"
          : "warn"
        : form.provider === "ollama"
          ? backend.ollama.reachable
            ? "ok"
            : "warn"
          : backend.openai.configured || backend.ollama.reachable
            ? "ok"
            : "warn";

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-end justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-slate-100">
            New scrape
          </h2>
          <p className="mt-0.5 text-[13px] text-slate-400">
            Describe what to extract — the agent fetches, parses and structures
            the page for you.
          </p>
        </div>
        <Button variant="ghost" size="sm" icon={IconRefresh} onClick={onReset}>
          Reset
        </Button>
      </div>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
        {/* Input column */}
        <div
          className="[perspective:1800px]"
        >
          <Glass
            className="p-5 transition-transform duration-500 lg:[transform:rotateX(1.1deg)] lg:hover:[transform:rotateX(0deg)]"
            style={{ transformOrigin: "center top" }}
          >
            <div className="space-y-5">
              <Field
                label={`Target URLs (${validUrlCount} valid${invalidCount ? ` · ${invalidCount} invalid` : ""})`}
                error={
                  invalidCount
                    ? `${invalidCount} URL(s) have problems — fix or remove them before running.`
                    : null
                }
                hint="One URL per line — every listed target is scraped in the same run. In single mode each URL is scraped once; crawl and pagination expand from every seed."
              >
                <div className="relative">
                  <span className="pointer-events-none absolute left-3 top-3 text-slate-500">
                    <IconGlobe size={15} />
                  </span>
                  <textarea
                    inputMode="url"
                    spellCheck={false}
                    value={form.urlsRaw}
                    onChange={(e) => setForm({ urlsRaw: e.target.value })}
                    placeholder={"https://example.com/catalog\nhttps://another-store.com/products\nhttps://blog.example.org/posts"}
                    rows={4}
                    aria-label="Target URLs, one per line"
                    className={cn(INPUT_CLS, "resize-y pl-9 leading-relaxed")}
                  />
                </div>
                {urlEntries.length > 0 && (
                  <ul className="mt-2 space-y-1">
                    {urlEntries.map((entry, index) => (
                      <li
                        key={`${entry.url}-${index}`}
                        className="flex items-center gap-1.5 text-[11px] leading-snug"
                      >
                        {entry.error && !entry.error.startsWith("duplicate") ? (
                          <IconAlert size={11} className="shrink-0 text-rose-300/80" />
                        ) : (
                          <IconCheck size={11} className="shrink-0 text-emerald-300/70" />
                        )}
                        <span className="sg-mono truncate text-slate-400">{entry.url}</span>
                        {entry.error && (
                          <span className={cn("shrink-0", entry.error.startsWith("duplicate") ? "text-amber-300/70" : "text-rose-300/80")}>
                            {entry.error}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </Field>

              <Field
                label="Extraction instructions"
                error={
                  promptLength > 0 && promptLength < 8
                    ? "Instructions are too short — describe what you need (min 8 characters)."
                    : null
                }
                hint="Natural language works best. For lists, name the fields you want per item."
              >
                <textarea
                  value={form.prompt}
                  onChange={(e) => setForm({ prompt: e.target.value })}
                  placeholder={
                    "e.g. Find all products priced under ₹50,000 and list their title, price and rating."
                  }
                  rows={4}
                  maxLength={4000}
                  aria-invalid={promptLength > 0 && promptLength < 8}
                  aria-label="Extraction instructions"
                  className={cn(INPUT_CLS, "resize-y leading-relaxed")}
                />
                <div className="mt-1.5 flex items-center justify-between">
                  <div className="flex flex-wrap gap-1.5">
                    {EXAMPLE_PROMPTS.slice(0, 3).map((example) => (
                      <button
                        key={example}
                        type="button"
                        onClick={() => setForm({ prompt: example })}
                        className="rounded-md border border-white/[0.07] bg-white/[0.03] px-2 py-1 text-[11px] text-slate-400 transition hover:border-white/[0.14] hover:text-slate-200"
                      >
                        {example.length > 44 ? `${example.slice(0, 44)}…` : example}
                      </button>
                    ))}
                  </div>
                  <span className="sg-num shrink-0 text-[11px] text-slate-500">
                    {promptLength}/4000
                  </span>
                </div>
              </Field>

              <div>
                <span className={LABEL_CLS}>Scraping mode</span>
                <Segmented
                  ariaLabel="Scraping mode"
                  value={form.mode}
                  onChange={(mode) => setForm({ mode })}
                  className="w-full [&>button]:flex-1"
                  options={[
                    {
                      value: "single",
                      label: (
                        <span className="flex items-center justify-center gap-1.5">
                          <IconDoc size={13} /> {MODE_META.single.label}
                        </span>
                      ),
                      hint: MODE_META.single.hint,
                    },
                    {
                      value: "crawl",
                      label: (
                        <span className="flex items-center justify-center gap-1.5">
                          <IconLayers size={13} /> {MODE_META.crawl.label}
                        </span>
                      ),
                      hint: MODE_META.crawl.hint,
                    },
                    {
                      value: "pagination",
                      label: (
                        <span className="flex items-center justify-center gap-1.5">
                          <IconChevronsRight size={13} /> {MODE_META.pagination.label}
                        </span>
                      ),
                      hint: MODE_META.pagination.hint,
                    },
                  ]}
                />
                <p className="mt-1.5 text-xs text-slate-500">
                  {MODE_META[form.mode].hint}. Duplicate URLs are always
                  prevented; crawling honours the depth and page caps.
                </p>
              </div>

              {/* Advanced controls */}
              <div className="rounded-xl border border-white/[0.06] bg-white/[0.02]">
                <button
                  type="button"
                  aria-expanded={advancedOpen}
                  onClick={() => setAdvancedOpen((open) => !open)}
                  className="flex w-full items-center gap-2 px-3.5 py-2.5 text-xs font-medium text-slate-300 transition hover:text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40"
                >
                  <IconSettings size={13} className="text-slate-500" />
                  Advanced controls
                  <IconChevronDown
                    size={13}
                    className={cn(
                      "ml-auto text-slate-500 transition-transform",
                      advancedOpen && "rotate-180",
                    )}
                  />
                </button>
                {advancedOpen && (
                  <div className="grid gap-4 border-t border-white/[0.06] p-4 sm:grid-cols-2">
                    {form.mode !== "single" && (
                      <>
                        <Field label="Max pages">
                          <input
                            type="number"
                            min={1}
                            max={50}
                            value={form.maxPages}
                            onChange={(e) =>
                              setForm({
                                maxPages: Math.min(50, Math.max(1, Number(e.target.value) || 1)),
                              })
                            }
                            aria-label="Maximum pages"
                            className={INPUT_CLS}
                          />
                        </Field>
                        {form.mode === "crawl" ? (
                          <Field label="Max depth">
                            <input
                              type="number"
                              min={0}
                              max={10}
                              value={form.maxDepth}
                              onChange={(e) =>
                                setForm({
                                  maxDepth: Math.min(10, Math.max(0, Number(e.target.value) || 0)),
                                })
                              }
                              aria-label="Maximum crawl depth"
                              className={INPUT_CLS}
                            />
                          </Field>
                        ) : (
                          <div className="flex items-end pb-1">
                            <div className="flex items-center gap-2.5">
                              <Toggle
                                checked={form.sameDomain}
                                onChange={(sameDomain) => setForm({ sameDomain })}
                                label="Restrict to the same domain"
                              />
                              <div>
                                <p className="text-xs font-medium text-slate-300">
                                  Same domain only
                                </p>
                                <p className="text-[11px] text-slate-500">
                                  Never follow external links
                                </p>
                              </div>
                            </div>
                          </div>
                        )}
                      </>
                    )}
                    <Field label="Request timeout (s)">
                      <input
                        type="number"
                        min={5}
                        max={120}
                        value={form.timeoutSec}
                        onChange={(e) =>
                          setForm({
                            timeoutSec: Math.min(120, Math.max(5, Number(e.target.value) || 5)),
                          })
                        }
                        aria-label="Request timeout in seconds"
                        className={INPUT_CLS}
                      />
                    </Field>
                    <Field label="Retries per page">
                      <input
                        type="number"
                        min={0}
                        max={5}
                        value={form.retries}
                        onChange={(e) =>
                          setForm({
                            retries: Math.min(5, Math.max(0, Number(e.target.value) || 0)),
                          })
                        }
                        aria-label="Retries per page"
                        className={INPUT_CLS}
                      />
                    </Field>
                    <Field label="Delay between requests (ms)">
                      <input
                        type="number"
                        min={0}
                        max={10000}
                        step={100}
                        value={form.delayMs}
                        onChange={(e) =>
                          setForm({
                            delayMs: Math.min(10000, Math.max(0, Number(e.target.value) || 0)),
                          })
                        }
                        aria-label="Delay between requests in milliseconds"
                        className={INPUT_CLS}
                      />
                    </Field>
                    <Field label="LLM timeout (s)">
                      <input
                        type="number"
                        min={30}
                        max={900}
                        step={30}
                        value={form.llmTimeoutSec}
                        onChange={(e) =>
                          setForm({
                            llmTimeoutSec: Math.min(900, Math.max(30, Number(e.target.value) || 30)),
                          })
                        }
                        aria-label="LLM timeout in seconds"
                        className={INPUT_CLS}
                      />
                    </Field>
                    <Field
                      label="Structured schema fields"
                      hint="Comma-separated. Forces the exact JSON keys per record."
                    >
                      <input
                        type="text"
                        value={form.schemaFields}
                        onChange={(e) => setForm({ schemaFields: e.target.value })}
                        placeholder="title, price, rating"
                        aria-label="Structured schema fields"
                        className={INPUT_CLS}
                      />
                    </Field>
                    <div className="flex items-end pb-1">
                      <div className="flex items-center gap-2.5">
                        <Toggle
                          checked={form.stripNoise}
                          onChange={(stripNoise) => setForm({ stripNoise })}
                          label="Strip scripts, styles and comments before extraction"
                        />
                        <div>
                          <p className="text-xs font-medium text-slate-300">
                            Clean HTML noise
                          </p>
                          <p className="text-[11px] text-slate-500">
                            Reduces tokens and junk
                          </p>
                        </div>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </Glass>
        </div>

        {/* Run column */}
        <div className="space-y-5">
          <Glass className="p-5">
            <div className="space-y-5">
              <div>
                <span className={LABEL_CLS}>Provider</span>
                <Segmented
                  ariaLabel="LLM provider"
                  value={form.provider}
                  onChange={(provider) => setForm({ provider })}
                  className="w-full [&>button]:flex-1"
                  options={[
                    { value: "auto", label: "Auto" },
                    { value: "openai", label: "Cloud" },
                    { value: "ollama", label: "Ollama" },
                  ]}
                />
                <p className="mt-2 flex items-start gap-1.5 text-[11px] leading-relaxed text-slate-400">
                  <StatusDot tone={providerTone} />
                  {providerHint}
                </p>
              </div>

              {form.mode === "crawl" && (
                <div className="flex items-center justify-between rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
                  <div>
                    <p className="text-xs font-medium text-slate-300">
                      Same domain only
                    </p>
                    <p className="text-[11px] text-slate-500">
                      External links are skipped
                    </p>
                  </div>
                  <Toggle
                    checked={form.sameDomain}
                    onChange={(sameDomain) => setForm({ sameDomain })}
                    label="Restrict to the same domain"
                  />
                </div>
              )}

              <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
                <p className="text-[11px] uppercase tracking-[0.09em] text-slate-500">
                  Run summary
                </p>
                <ul className="sg-num mt-2 space-y-1 text-xs text-slate-300">
                  <li className="flex justify-between">
                    <span className="text-slate-500">Mode</span>
                    <span>{MODE_META[form.mode].label}</span>
                  </li>
                  <li className="flex justify-between">
                    <span className="text-slate-500">Seeds</span>
                    <span>{validUrlCount} URL{validUrlCount === 1 ? "" : "s"}</span>
                  </li>
                  <li className="flex justify-between">
                    <span className="text-slate-500">Page budget</span>
                    <span>
                      {form.mode === "single"
                        ? validUrlCount
                        : Math.max(form.maxPages, validUrlCount)}
                    </span>
                  </li>
                  {form.mode === "crawl" && (
                    <li className="flex justify-between">
                      <span className="text-slate-500">Depth</span>
                      <span>{form.maxDepth}</span>
                    </li>
                  )}
                  <li className="flex justify-between">
                    <span className="text-slate-500">Timeout / retries</span>
                    <span>
                      {form.timeoutSec}s · {form.retries}
                    </span>
                  </li>
                  <li className="flex justify-between">
                    <span className="text-slate-500">Schema</span>
                    <span className="truncate pl-4 text-right">
                      {form.schemaFields.trim() ? form.schemaFields.trim() : "free-form"}
                    </span>
                  </li>
                </ul>
              </div>

              {running ? (
                <Button
                  variant="danger"
                  className="w-full"
                  icon={IconStop}
                  onClick={onCancel}
                >
                  Cancel run
                </Button>
              ) : (
                <Button
                  variant="primary"
                  className="w-full"
                  icon={IconPlay}
                  onClick={onRun}
                  disabled={!valid.urls || !valid.prompt}
                >
                  Run scrape
                </Button>
              )}

              <p className="text-center text-[10px] leading-relaxed text-slate-600">
                The run streams real events — fetching, extraction and page
                discovery are reported as they happen. Nothing is simulated.
              </p>
            </div>
          </Glass>

          {form.mode === "crawl" && (
            <Glass className="p-4">
              <div className="flex items-start gap-2.5">
                <IconLayers size={15} className="mt-0.5 shrink-0 text-slate-500" />
                <p className="text-[11.5px] leading-relaxed text-slate-400">
                  Crawling performs a breadth-first walk from the target URL,
                  limited to{" "}
                  <span className="sg-num text-slate-200">{form.maxPages} pages</span>{" "}
                  and depth{" "}
                  <span className="sg-num text-slate-200">{form.maxDepth}</span>. Every
                  visited URL is de-duplicated by canonical form.
                </p>
              </div>
            </Glass>
          )}
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Results                                                                    */
/* -------------------------------------------------------------------------- */

function Metric({
  label,
  value,
  sub,
}: {
  label: string;
  value: React.ReactNode;
  sub?: string;
}) {
  return (
    <div className="rounded-xl border border-white/[0.06] bg-white/[0.03] px-3.5 py-2.5">
      <p className="text-[10.5px] uppercase tracking-[0.09em] text-slate-500">
        {label}
      </p>
      <p className="sg-num mt-1 text-lg font-semibold leading-none tracking-tight text-slate-100">
        {value}
      </p>
      {sub ? <p className="mt-1 text-[11px] text-slate-500">{sub}</p> : null}
    </div>
  );
}

function StatusBadge({ status }: { status: RunStatus }) {
  const meta: Record<RunStatus, { tone: "ok" | "error" | "warn" | "accent"; label: string }> = {
    running: { tone: "accent", label: "Running" },
    completed: { tone: "ok", label: "Completed" },
    failed: { tone: "error", label: "Failed" },
    cancelled: { tone: "warn", label: "Cancelled" },
  };
  return (
    <Badge tone={meta[status].tone}>
      {status === "running" && <StatusDot tone="ok" live />}
      {meta[status].label}
    </Badge>
  );
}

function RefineBar({
  rows,
  onChange,
}: {
  rows: ScrapeRow[];
  onChange: (refine: RefineState) => void;
}) {
  const columns = useMemo(() => deriveColumns(rows), [rows]);
  const [refine, setRefine] = useState<RefineState>({
    column: "",
    minOn: false,
    minValue: 0,
    maxOn: false,
    maxValue: 50000,
    sort: "none",
    text: "",
  });
  const [open, setOpen] = useState(false);

  const patch = (next: Partial<RefineState>) => {
    setRefine((prev) => {
      const merged = { ...prev, ...next };
      onChange(merged);
      return merged;
    });
  };

  const reset = () => {
    const fresh: RefineState = {
      column: "",
      minOn: false,
      minValue: 0,
      maxOn: false,
      maxValue: 50000,
      sort: "none",
      text: "",
    };
    setRefine(fresh);
    onChange(fresh);
  };

  const active =
    refine.column ||
    refine.minOn ||
    refine.maxOn ||
    refine.sort !== "none" ||
    Boolean(refine.text.trim());

  return (
    <div className="rounded-xl border border-white/[0.06] bg-white/[0.02]">
      <div className="flex flex-wrap items-center gap-2 px-3.5 py-2.5">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-2 text-xs font-medium text-slate-300 transition hover:text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40"
        >
          <IconFilter size={13} className={active ? "text-sky-300/80" : "text-slate-500"} />
          Refine results
          {active && <Badge tone="accent">active</Badge>}
          <IconChevronDown
            size={13}
            className={cn("text-slate-500 transition-transform", open && "rotate-180")}
          />
        </button>
        {active && (
          <Button variant="ghost" size="sm" onClick={reset}>
            Clear
          </Button>
        )}
        <p className="ml-auto text-[11px] text-slate-500">
          Filters and sorting run locally on the actual scraped rows — the
          model never executes code.
        </p>
      </div>
      {open && (
        <div className="grid gap-3 border-t border-white/[0.06] p-3.5 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Column">
            <select
              value={refine.column}
              onChange={(e) => patch({ column: e.target.value })}
              aria-label="Refine column"
              className={cn(INPUT_CLS, "appearance-none")}
            >
              <option value="">(all columns)</option>
              {columns.map((col) => (
                <option key={col} value={col}>
                  {col}
                </option>
              ))}
            </select>
          </Field>
          <div className="flex gap-2">
            <Field label="Min ≥">
              <div className="flex items-center gap-2">
                <Toggle
                  checked={refine.minOn}
                  onChange={(minOn) => patch({ minOn })}
                  label="Enable minimum filter"
                />
                <input
                  type="number"
                  value={refine.minValue}
                  onChange={(e) => patch({ minValue: Number(e.target.value) || 0 })}
                  disabled={!refine.minOn}
                  aria-label="Minimum numeric value"
                  className={cn(INPUT_CLS, "disabled:opacity-40")}
                />
              </div>
            </Field>
          </div>
          <div className="flex gap-2">
            <Field label="Max ≤">
              <div className="flex items-center gap-2">
                <Toggle
                  checked={refine.maxOn}
                  onChange={(maxOn) => patch({ maxOn })}
                  label="Enable maximum filter"
                />
                <input
                  type="number"
                  value={refine.maxValue}
                  onChange={(e) => patch({ maxValue: Number(e.target.value) || 0 })}
                  disabled={!refine.maxOn}
                  aria-label="Maximum numeric value"
                  className={cn(INPUT_CLS, "disabled:opacity-40")}
                />
              </div>
            </Field>
          </div>
          <div className="flex gap-2">
            <Field label="Sort">
              <select
                value={refine.sort}
                onChange={(e) => patch({ sort: e.target.value as RefineState["sort"] })}
                aria-label="Sort direction"
                className={cn(INPUT_CLS, "appearance-none")}
              >
                <option value="none">No sorting</option>
                <option value="asc">Numeric ascending</option>
                <option value="desc">Numeric descending</option>
              </select>
            </Field>
            <Field label="Contains text">
              <input
                type="text"
                value={refine.text}
                onChange={(e) => patch({ text: e.target.value })}
                placeholder="filter…"
                aria-label="Text filter for selected column"
                className={INPUT_CLS}
              />
            </Field>
          </div>
        </div>
      )}
    </div>
  );
}

function applyRefine(rows: ScrapeRow[], refine: RefineState): ScrapeRow[] {
  let out = rows;
  if (refine.column) {
    if (refine.minOn) {
      out = out.filter((row) => {
        const value = parseNumeric(row[refine.column]);
        return value !== null && value >= refine.minValue;
      });
    }
    if (refine.maxOn) {
      out = out.filter((row) => {
        const value = parseNumeric(row[refine.column]);
        return value !== null && value <= refine.maxValue;
      });
    }
    if (refine.text.trim()) {
      const needle = refine.text.trim().toLowerCase();
      out = out.filter((row) =>
        String(row[refine.column] ?? "").toLowerCase().includes(needle),
      );
    }
    if (refine.sort !== "none") {
      const direction = refine.sort === "asc" ? 1 : -1;
      out = [...out].sort((a, b) => {
        const an = parseNumeric(a[refine.column]);
        const bn = parseNumeric(b[refine.column]);
        if (an !== null && bn !== null) return (an - bn) * direction;
        if (an !== null) return -1;
        if (bn !== null) return 1;
        return String(a[refine.column] ?? "").localeCompare(String(b[refine.column] ?? ""));
      });
    }
  }
  return out;
}

function DataTable({ rows, recordId }: { rows: ScrapeRow[]; recordId: string }) {
  const columns = useMemo(() => deriveColumns(rows), [rows]);
  const [sort, setSort] = useState<{ column: string; direction: 1 | -1 } | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const { column, direction } = sort;
    return [...rows].sort((a, b) => {
      const an = parseNumeric(a[column]);
      const bn = parseNumeric(b[column]);
      if (an !== null && bn !== null) return (an - bn) * direction;
      if (an !== null) return -1;
      if (bn !== null) return 1;
      return String(a[column] ?? "").localeCompare(String(b[column] ?? "")) * direction;
    });
  }, [rows, sort]);

  const toggleSort = (column: string) => {
    setSort((prev) => {
      if (prev?.column === column) {
        if (prev.direction === 1) return { column, direction: -1 };
        return null;
      }
      return { column, direction: 1 };
    });
  };

  if (columns.length === 0) return null;

  return (
    <div className="sg-scroll overflow-auto rounded-xl border border-white/[0.06]">
      <table className="w-full border-collapse text-left text-[12.5px]">
        <thead>
          <tr className="bg-white/[0.03]">
            {columns.map((column) => {
              const active = sort?.column === column;
              return (
                <th
                  key={column}
                  aria-sort={
                    active
                      ? sort.direction === 1
                        ? "ascending"
                        : "descending"
                      : "none"
                  }
                  className="whitespace-nowrap border-b border-white/[0.06] px-3 py-2 font-medium text-slate-300"
                >
                  <button
                    type="button"
                    onClick={() => toggleSort(column)}
                    className={cn(
                      "flex items-center gap-1 transition hover:text-slate-100",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40",
                      active ? "text-sky-200" : "",
                    )}
                  >
                    {column}
                    <span className="text-[9px]">
                      {active ? (sort.direction === 1 ? "▲" : "▼") : "↕"}
                    </span>
                  </button>
                </th>
              );
            })}
            <th className="whitespace-nowrap border-b border-white/[0.06] px-3 py-2 font-medium text-slate-300">
              Source
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, rowIndex) => (
            <tr
              key={`${recordId}-${rowIndex}`}
              className="border-b border-white/[0.04] last:border-0 hover:bg-white/[0.025]"
            >
              {columns.map((column) => {
                const value = row[column];
                const numeric = parseNumeric(value);
                return (
                  <td
                    key={column}
                    className="max-w-[320px] truncate px-3 py-2 text-slate-300"
                    title={value === null || value === undefined ? "" : String(value)}
                  >
                    {value === null || value === undefined || value === "" ? (
                      <span className="text-slate-600">—</span>
                    ) : (
                      <span className={numeric !== null ? "sg-num" : ""}>
                        {String(value)}
                      </span>
                    )}
                  </td>
                );
              })}
              <td className="max-w-[240px] px-3 py-2">
                <a
                  href={String(row.source_url)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="group flex items-center gap-1.5 truncate text-sky-300/70 transition hover:text-sky-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40"
                  title={String(row.source_url)}
                >
                  <span className="truncate">
                    {String(row.source_url).replace(/^https?:\/\//, "")}
                  </span>
                  <IconExternal
                    size={11}
                    className="shrink-0 opacity-50 transition group-hover:opacity-100"
                  />
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RawViewer({ record }: { record: RunRecord }) {
  const [pageIndex, setPageIndex] = useState(0);
  const page = record.pages[pageIndex] ?? null;
  const rawText = useMemo(() => {
    if (!page || page.raw === null || page.raw === undefined) return "";
    try {
      return JSON.stringify(page.raw, null, 2);
    } catch {
      return String(page.raw);
    }
  }, [page]);
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    if (await copyText(rawText)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {record.pages.map((p, index) => (
          <button
            key={`${p.url}-${index}`}
            type="button"
            onClick={() => setPageIndex(index)}
            aria-pressed={index === pageIndex}
            className={cn(
              "rounded-md border px-2.5 py-1 text-[11px] transition",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40",
              index === pageIndex
                ? "border-sky-300/30 bg-sky-400/[0.08] text-sky-100"
                : "border-white/[0.07] bg-white/[0.03] text-slate-400 hover:text-slate-200",
            )}
          >
            {p.ok ? "✓" : "✗"} P{index + 1}
          </button>
        ))}
        {rawText && (
          <Button variant="ghost" size="sm" icon={IconCopy} onClick={copy} className="ml-auto">
            {copied ? "Copied" : "Copy JSON"}
          </Button>
        )}
      </div>
      {page ? (
        rawText ? (
          <pre className="sg-scroll sg-mono max-h-96 overflow-auto rounded-xl border border-white/[0.06] bg-[#0a0d14]/80 p-4 text-[11.5px] leading-relaxed text-slate-300">
            {rawText.length > 120_000
              ? `${rawText.slice(0, 120_000)}\n… (truncated for display — exports contain the normalized rows)`
              : rawText}
          </pre>
        ) : (
          <p className="text-xs text-slate-500">
            No raw payload — this page failed:{" "}
            <span className="text-rose-300/80">{page.error || "unknown error"}</span>
          </p>
        )
      ) : (
        <p className="text-xs text-slate-500">No pages recorded.</p>
      )}
    </div>
  );
}

function SourcesList({ record }: { record: RunRecord }) {
  return (
    <ul className="divide-y divide-white/[0.04]">
      {record.pages.length === 0 ? (
        <li className="py-3 text-xs text-slate-500">No pages recorded.</li>
      ) : (
        record.pages.map((page, index) => (
          <li key={`${page.finalUrl}-${index}`} className="flex items-center gap-3 py-2.5">
            <StatusDot tone={page.ok ? "ok" : "error"} />
            <span className="sg-num w-8 shrink-0 text-[11px] text-slate-500">
              P{index + 1}
            </span>
            <div className="min-w-0 flex-1">
              <a
                href={page.finalUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="group flex items-center gap-1.5 truncate text-[12.5px] text-sky-300/70 hover:text-sky-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40"
                title={page.finalUrl}
              >
                <span className="truncate">{page.finalUrl}</span>
                <IconExternal size={11} className="shrink-0 opacity-50" />
              </a>
              {page.finalUrl !== page.url && (
                <p className="mt-0.5 truncate text-[10.5px] text-slate-600">
                  redirected from {page.url}
                </p>
              )}
            </div>
            <div className="shrink-0 text-right">
              <p className="sg-num text-[11px] text-slate-400">
                {page.ok ? `${page.rowCount} rows` : "failed"}
              </p>
              <p className="sg-num text-[10.5px] text-slate-600">
                {fmtMs(page.fetchMs + page.extractMs)} · depth {page.depth}
              </p>
            </div>
          </li>
        ))
      )}
    </ul>
  );
}

function Diagnostics({ record }: { record: RunRecord }) {
  const lines = [
    `Pages attempted ${record.stats.pagesAttempted} — ${record.stats.pagesOk} succeeded, ${record.stats.pagesFailed} failed.`,
    `Raw rows ${record.stats.rowsRaw} → normalized ${record.stats.rowsFinal} (record de-duplication applied).`,
    `Duplicate URLs prevented: ${record.stats.duplicateUrlsSkipped} — external links skipped: ${record.stats.externalSkipped}.`,
    `Distinct source URLs in results: ${new Set(record.rows.map((r) => r.source_url)).size}.`,
  ];
  return (
    <div className="space-y-4">
      <ul className="space-y-1.5">
        {lines.map((line, index) => (
          <li key={index} className="flex items-start gap-2 text-[12.5px] text-slate-300">
            <IconCheck size={13} className="mt-0.5 shrink-0 text-emerald-300/60" />
            {line}
          </li>
        ))}
      </ul>
      {record.warnings.length > 0 && (
        <div className="rounded-xl border border-amber-400/15 bg-amber-400/[0.04] p-3.5">
          <p className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.09em] text-amber-200/90">
            <IconAlert size={13} /> Warnings
          </p>
          <ul className="space-y-1">
            {record.warnings.map((warning, index) => (
              <li key={index} className="text-[12px] leading-relaxed text-amber-100/80">
                {warning}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="sg-scroll overflow-auto rounded-xl border border-white/[0.06]">
        <table className="w-full text-left text-[12px]">
          <thead>
            <tr className="bg-white/[0.03]">
              {["Page", "URL", "HTTP", "Rows", "Fetch", "Extract", "HTML", "Result"].map(
                (header) => (
                  <th
                    key={header}
                    className="whitespace-nowrap border-b border-white/[0.06] px-3 py-2 font-medium text-slate-300"
                  >
                    {header}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {record.pages.map((page, index) => (
              <tr key={`${page.finalUrl}-${index}`} className="border-b border-white/[0.04] last:border-0">
                <td className="sg-num px-3 py-2 text-slate-400">{index + 1}</td>
                <td className="max-w-[280px] truncate px-3 py-2 text-slate-300" title={page.finalUrl}>
                  {page.finalUrl.replace(/^https?:\/\//, "")}
                </td>
                <td className="sg-num px-3 py-2 text-slate-400">{page.statusCode ?? "—"}</td>
                <td className="sg-num px-3 py-2 text-slate-300">{page.rowCount}</td>
                <td className="sg-num px-3 py-2 text-slate-400">{fmtMs(page.fetchMs)}</td>
                <td className="sg-num px-3 py-2 text-slate-400">{fmtMs(page.extractMs)}</td>
                <td className="sg-num px-3 py-2 text-slate-400">{fmtBytes(page.htmlChars)}</td>
                <td className="px-3 py-2">
                  {page.ok ? (
                    <span className="text-emerald-300/80">OK</span>
                  ) : (
                    <span className="text-rose-300/80" title={page.error}>
                      {page.error.slice(0, 48) || "failed"}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ExportBar({ record, rows }: { record: RunRecord; rows: ScrapeRow[] }) {
  const [copied, setCopied] = useState(false);
  const base = `scrapework-${record.id}`;

  const exportJson = () =>
    downloadFile(`${base}.json`, JSON.stringify(rows, null, 2), "application/json");
  const exportCsv = () => downloadFile(`${base}.csv`, rowsToCSV(rows), "text/csv");
  const exportMd = () => downloadFile(`${base}.md`, rowsToMarkdown(rows), "text/markdown");
  const exportTxt = () => downloadFile(`${base}.txt`, rowsToTXT(rows), "text/plain");

  const copyJson = async () => {
    if (await copyText(JSON.stringify(rows, null, 2))) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="mr-1 flex items-center gap-1.5 text-[11px] uppercase tracking-[0.09em] text-slate-500">
        <IconDownload size={13} /> Export
      </span>
      <Button variant="secondary" size="sm" onClick={exportJson}>
        JSON
      </Button>
      <Button variant="secondary" size="sm" onClick={exportCsv}>
        CSV
      </Button>
      <Button variant="secondary" size="sm" onClick={exportMd}>
        Markdown
      </Button>
      <Button variant="secondary" size="sm" onClick={exportTxt}>
        TXT
      </Button>
      <Button variant="ghost" size="sm" icon={IconCopy} onClick={copyJson}>
        {copied ? "Copied" : "Copy JSON"}
      </Button>
      <span className="ml-auto sg-num text-[11px] text-slate-500">
        {rows.length} row(s) · {fmtBytes(rowsToCSV(rows).length)}
      </span>
    </div>
  );
}

function ResultsPanel({ record }: { record: RunRecord }) {
  const [refine, setRefine] = useState<RefineState>({
    column: "",
    minOn: false,
    minValue: 0,
    maxOn: false,
    maxValue: 50000,
    sort: "none",
    text: "",
  });
  const [showRaw, setShowRaw] = useState(false);
  const [showSources, setShowSources] = useState(false);
  const [showDiagnostics, setShowDiagnostics] = useState(Boolean(record.warnings.length));

  const displayed = useMemo(
    () => applyRefine(record.rows, refine),
    [record.rows, refine],
  );
  const filteredOut = record.rows.length - displayed.length;

  return (
    <div className="sg-fade-up space-y-5" style={{ animationDelay: "120ms" }}>
      <Glass className="p-5">
        {/* Header */}
        <div className="flex flex-wrap items-center gap-3">
          <div>
            <h3 className="text-[15px] font-semibold tracking-tight text-slate-100">
              Results
            </h3>
            <p className="sg-mono mt-0.5 text-[11px] text-slate-500">
              {record.id} · started {fmtTime(record.startedAt)}
            </p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Badge>{MODE_META[record.config.mode]?.label ?? record.config.mode}</Badge>
            <Badge tone="accent">
              {record.config.provider} · {record.config.model}
            </Badge>
            <StatusBadge status={record.status} />
          </div>
        </div>

        {record.error && (
          <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-rose-400/20 bg-rose-500/[0.06] p-3.5">
            <IconAlert size={15} className="mt-0.5 shrink-0 text-rose-300/90" />
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.09em] text-rose-200">
                Run failed
              </p>
              <p className="mt-1 text-[12.5px] leading-relaxed text-rose-100/80">
                {record.error}
              </p>
            </div>
          </div>
        )}

        {/* Metrics */}
        <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
          <Metric
            label="Pages"
            value={`${record.stats.pagesOk}/${record.stats.pagesAttempted}`}
            sub={`${record.stats.pagesFailed} failed`}
          />
          <Metric
            label="Rows"
            value={record.stats.rowsFinal}
            sub={`${record.stats.rowsRaw} raw`}
          />
          <Metric label="Duration" value={fmtMs(record.stats.durationMs)} sub="wall time" />
          <Metric
            label="Fetch"
            value={fmtMs(record.stats.fetchMs)}
            sub={fmtBytes(record.stats.htmlChars)}
          />
          <Metric
            label="Extraction"
            value={fmtMs(record.stats.extractMs)}
            sub="model time"
          />
          <Metric
            label="Warnings"
            value={record.warnings.length}
            sub={record.warnings.length ? "see diagnostics" : "clean run"}
          />
        </div>

        {/* Refine + table */}
        {record.rows.length > 0 && (
          <div className="mt-5 space-y-3">
            <RefineBar rows={record.rows} onChange={setRefine} />
            {displayed.length > 0 ? (
              <DataTable rows={displayed} recordId={record.id} />
            ) : (
              <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-8 text-center">
                <p className="text-[13px] text-slate-400">
                  The current filters exclude every row.
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  Clear the refine controls to restore {record.rows.length} row(s).
                </p>
              </div>
            )}
            <p className="sg-num text-[11px] text-slate-500">
              Showing {displayed.length} of {record.rows.length} row(s)
              {filteredOut > 0 ? ` · ${filteredOut} hidden by filters` : ""}
            </p>
          </div>
        )}

        {record.rows.length === 0 && record.status === "completed" && (
          <div className="mt-5 rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-8 text-center">
            <p className="text-[13px] text-slate-300">
              The pipeline completed but produced zero rows.
            </p>
            <p className="mt-1 text-xs text-slate-500">
              Try a more specific instruction, a different page, or enable
              structured schema fields.
            </p>
          </div>
        )}
      </Glass>

      {/* Exports */}
      {displayed.length > 0 && (
        <Glass className="px-5 py-4">
          <ExportBar record={record} rows={displayed} />
        </Glass>
      )}

      {/* Raw responses */}
      <Glass className="p-5">
        <button
          type="button"
          aria-expanded={showRaw}
          onClick={() => setShowRaw((v) => !v)}
          className="flex w-full items-center gap-2 text-xs font-semibold uppercase tracking-[0.09em] text-slate-300 transition hover:text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40"
        >
          <IconCode size={14} className="text-slate-500" />
          Raw model responses
          <span className="text-slate-600">(untrusted output, for inspection)</span>
          <IconChevronDown
            size={13}
            className={cn("ml-auto text-slate-500 transition-transform", showRaw && "rotate-180")}
          />
        </button>
        {showRaw && (
          <div className="mt-4">
            <RawViewer record={record} />
          </div>
        )}
      </Glass>

      {/* Sources */}
      <Glass className="p-5">
        <button
          type="button"
          aria-expanded={showSources}
          onClick={() => setShowSources((v) => !v)}
          className="flex w-full items-center gap-2 text-xs font-semibold uppercase tracking-[0.09em] text-slate-300 transition hover:text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40"
        >
          <IconLink size={14} className="text-slate-500" />
          Source URLs
          <span className="text-slate-600">({record.pages.length} page(s))</span>
          <IconChevronDown
            size={13}
            className={cn("ml-auto text-slate-500 transition-transform", showSources && "rotate-180")}
          />
        </button>
        {showSources && (
          <div className="mt-4">
            <SourcesList record={record} />
          </div>
        )}
      </Glass>

      {/* Diagnostics */}
      <Glass className="p-5">
        <button
          type="button"
          aria-expanded={showDiagnostics}
          onClick={() => setShowDiagnostics((v) => !v)}
          className="flex w-full items-center gap-2 text-xs font-semibold uppercase tracking-[0.09em] text-slate-300 transition hover:text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40"
        >
          <IconTable size={14} className="text-slate-500" />
          Diagnostics & validation
          {record.warnings.length > 0 && <Badge tone="warn">{record.warnings.length}</Badge>}
          <IconChevronDown
            size={13}
            className={cn("ml-auto text-slate-500 transition-transform", showDiagnostics && "rotate-180")}
          />
        </button>
        {showDiagnostics && (
          <div className="mt-4">
            <Diagnostics record={record} />
          </div>
        )}
      </Glass>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  History / Saved views                                                      */
/* -------------------------------------------------------------------------- */

function RecordCard({
  record,
  onOpen,
  onSave,
  onDelete,
  onUnsave,
  savedMode = false,
}: {
  record: RunRecord;
  onOpen: () => void;
  onSave?: () => void;
  onDelete: () => void;
  onUnsave?: () => void;
  savedMode?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <Glass className="p-4">
      <div className="flex flex-wrap items-center gap-3">
        <StatusBadge status={record.status} />
        <div className="min-w-0">
          <p className="truncate text-[13px] font-medium text-slate-200">
            {record.config.urls && record.config.urls.length > 1
              ? `${record.config.urls.length} URLs`
              : (record.config.url ?? record.config.urls?.[0] ?? "").replace(/^https?:\/\//, "")}
          </p>
          <p className="sg-num mt-0.5 text-[11px] text-slate-500">
            {fmtTime(record.startedAt)} · {record.config.provider} ·{" "}
            {MODE_META[record.config.mode]?.label ?? record.config.mode} ·{" "}
            {record.stats.pagesOk}/{record.stats.pagesAttempted} pages ·{" "}
            {record.stats.rowsFinal} rows · {fmtMs(record.stats.durationMs)}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-1">
          <Button variant="ghost" size="sm" onClick={onOpen} icon={IconExternal}>
            Open
          </Button>
          {savedMode ? (
            <Button variant="ghost" size="sm" onClick={onUnsave} icon={IconX}>
              Unsave
            </Button>
          ) : (
            <Button variant="ghost" size="sm" onClick={onSave} icon={IconBookmark}>
              Save
            </Button>
          )}
          <IconButton label="Delete run" icon={IconTrash} variant="danger" onClick={onDelete} />
        </div>
      </div>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
        className="mt-3 flex items-center gap-1.5 text-[11px] text-slate-500 transition hover:text-slate-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40"
      >
        <IconChevronDown
          size={12}
          className={cn("transition-transform", expanded && "rotate-180")}
        />
        {expanded ? "Hide details" : "Show details"}
      </button>
      {expanded && (
        <div className="mt-3 space-y-2.5 rounded-xl border border-white/[0.06] bg-white/[0.02] p-3.5">
          <p className="text-xs text-slate-400">
            <span className="text-slate-500">Instructions: </span>
            {record.config.prompt}
          </p>
          {record.error && (
            <p className="text-xs text-rose-300/80">
              <span className="text-slate-500">Error: </span>
              {record.error}
            </p>
          )}
          <div className="sg-num grid grid-cols-4 gap-2 text-center">
            {[
              ["Pages OK", record.stats.pagesOk],
              ["Rows", record.stats.rowsFinal],
              ["Fetch", fmtMs(record.stats.fetchMs)],
              ["Extract", fmtMs(record.stats.extractMs)],
            ].map(([label, value]) => (
              <div key={label} className="rounded-lg bg-white/[0.03] px-2 py-1.5">
                <p className="text-[9.5px] uppercase tracking-[0.08em] text-slate-500">
                  {label}
                </p>
                <p className="text-[12.5px] font-medium text-slate-200">{value}</p>
              </div>
            ))}
          </div>
        </div>
      )}
    </Glass>
  );
}

function HistoryView({
  records,
  onOpen,
  onSave,
  onDelete,
  onClearAll,
  loading,
}: {
  records: RunRecord[];
  onOpen: (record: RunRecord) => void;
  onSave: (record: RunRecord) => void;
  onDelete: (id: string) => void;
  onClearAll: () => void;
  loading: boolean;
}) {
  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-slate-100">History</h2>
          <p className="mt-0.5 text-[13px] text-slate-400">
            Runs recorded on the backend — persisted across sessions.
          </p>
        </div>
        {records.length > 0 && (
          <Button variant="danger" size="sm" icon={IconTrash} onClick={onClearAll}>
            Clear local view
          </Button>
        )}
      </div>
      {loading ? (
        <Glass className="flex items-center justify-center px-8 py-16 text-slate-400">
          <p className="flex items-center gap-2 text-xs">
            <IconSpinner size={14} /> Loading backend history…
          </p>
        </Glass>
      ) : records.length === 0 ? (
        <EmptyState
          icon={IconHistory}
          title="No runs yet"
          body="Run a scrape and the full record — configuration, pages, rows and diagnostics — is persisted on the SARA backend and listed here."
        />
      ) : (
        <div className="space-y-3">
          {records.map((record, index) => (
            <RecordCard
              key={record.id || index}
              record={record}
              onOpen={() => onOpen(record)}
              onSave={() => onSave(record)}
              onDelete={() => onDelete(record.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function SavedView({
  records,
  onOpen,
  onDelete,
}: {
  records: RunRecord[];
  onOpen: (record: RunRecord) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-slate-100">
            Saved results
          </h2>
          <p className="mt-0.5 text-[13px] text-slate-400">
            Results you bookmarked — kept locally between sessions.
          </p>
        </div>
        {records.length > 0 && (
          <Button
            variant="danger"
            size="sm"
            icon={IconTrash}
            onClick={() => records.forEach((r) => onDelete(r.id))}
          >
            Clear all
          </Button>
        )}
      </div>
      {records.length === 0 ? (
        <EmptyState
          icon={IconBookmark}
          title="Nothing saved"
          body="Use “Save” on a history record to pin a result here for later review and export."
        />
      ) : (
        <div className="space-y-3">
          {records.map((record) => (
            <RecordCard
              key={record.id}
              record={record}
              savedMode
              onOpen={() => onOpen(record)}
              onDelete={() => onDelete(record.id)}
              onUnsave={() => onDelete(record.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Settings                                                                   */
/* -------------------------------------------------------------------------- */

function SettingsView({
  settings,
  setSettings,
  backend,
  backendError,
  checking,
  onRefresh,
  onClearSaved,
}: {
  settings: AppSettings;
  setSettings: (patch: Partial<AppSettings>) => void;
  backend: BackendStatus | null;
  backendError: string | null;
  checking: boolean;
  onRefresh: () => void;
  onClearSaved: () => void;
}) {
  const [confirmSaved, setConfirmSaved] = useState(false);

  useEffect(() => {
    if (!confirmSaved) return;
    const id = window.setTimeout(() => setConfirmSaved(false), 3000);
    return () => window.clearTimeout(id);
  }, [confirmSaved]);

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold tracking-tight text-slate-100">Settings</h2>
        <p className="mt-0.5 text-[13px] text-slate-400">
          Local preferences for this browser. Secrets never appear here.
        </p>
      </div>

      <div className="grid gap-5 xl:grid-cols-2">
        <Glass className="p-5">
          <h3 className="flex items-center gap-2 text-[13px] font-semibold text-slate-200">
            <IconSettings size={14} className="text-slate-500" /> Preferences
          </h3>
          <div className="mt-4 space-y-4">
            <Field
              label="Backend endpoint"
              hint="Where this agent sends scraping requests. Defaults to the unified SARA backend."
            >
              <input
                type="text"
                value={settings.backendBaseUrl}
                onChange={(e) => setSettings({ backendBaseUrl: e.target.value.trim() })}
                aria-label="Backend endpoint URL"
                className={INPUT_CLS}
              />
            </Field>
            <div>
              <span className={LABEL_CLS}>Default provider</span>
              <Segmented
                ariaLabel="Default provider"
                value={settings.defaultProvider}
                onChange={(defaultProvider) => setSettings({ defaultProvider })}
                options={[
                  { value: "auto", label: "Auto" },
                  { value: "openai", label: "Cloud" },
                  { value: "ollama", label: "Ollama" },
                ]}
              />
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Default timeout (s)">
                <input
                  type="number"
                  min={5}
                  max={120}
                  value={settings.defaultTimeoutSec}
                  onChange={(e) =>
                    setSettings({
                      defaultTimeoutSec: Math.min(120, Math.max(5, Number(e.target.value) || 5)),
                    })
                  }
                  aria-label="Default request timeout"
                  className={INPUT_CLS}
                />
              </Field>
              <Field label="Default delay (ms)">
                <input
                  type="number"
                  min={0}
                  max={10000}
                  step={100}
                  value={settings.defaultDelayMs}
                  onChange={(e) =>
                    setSettings({
                      defaultDelayMs: Math.min(10000, Math.max(0, Number(e.target.value) || 0)),
                    })
                  }
                  aria-label="Default request delay"
                  className={INPUT_CLS}
                />
              </Field>
            </div>
            <div className="flex items-center justify-between rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
              <div>
                <p className="text-xs font-medium text-slate-300">
                  Clean HTML noise by default
                </p>
                <p className="text-[11px] text-slate-500">
                  Strip scripts, styles and comments before extraction
                </p>
              </div>
              <Toggle
                checked={settings.defaultStripNoise}
                onChange={(defaultStripNoise) => setSettings({ defaultStripNoise })}
                label="Clean HTML noise by default"
              />
            </div>
          </div>
        </Glass>

        <Glass className="p-5">
          <h3 className="flex items-center gap-2 text-[13px] font-semibold text-slate-200">
            <IconGlobe size={14} className="text-slate-500" /> Backend connection
          </h3>
          <div className="mt-4 space-y-3">
            <div className="flex items-center justify-between rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
              <span className="text-xs text-slate-400">Endpoint health</span>
              {backendError ? (
                <Badge tone="error">Unreachable</Badge>
              ) : backend ? (
                <Badge tone="ok">
                  <StatusDot tone="ok" /> Connected
                </Badge>
              ) : (
                <Badge>Checking…</Badge>
              )}
            </div>
            {backend && (
              <>
                <div className="flex items-center justify-between rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
                  <div>
                    <p className="text-xs font-medium text-slate-300">Cloud LLM</p>
                    <p className="text-[11px] text-slate-500">Server-side key (masked)</p>
                  </div>
                  {backend.openai.configured ? (
                    <Badge tone="ok">{backend.openai.model}</Badge>
                  ) : (
                    <Badge tone="warn">Not configured</Badge>
                  )}
                </div>
                <div className="flex items-center justify-between rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
                  <div>
                    <p className="text-xs font-medium text-slate-300">Ollama</p>
                    <p className="sg-mono text-[11px] text-slate-500">{backend.ollama.baseUrl}</p>
                  </div>
                  {backend.ollama.reachable ? (
                    <Badge tone="ok">{backend.ollama.installed.length} models</Badge>
                  ) : (
                    <Badge tone="warn">Unreachable</Badge>
                  )}
                </div>
                {backend.ollama.reachable && (
                  <p className="text-[11px] leading-relaxed text-slate-500">
                    Installed:{" "}
                    <span className="sg-mono">{backend.ollama.installed.join(", ") || "none"}</span>
                  </p>
                )}
              </>
            )}
            {backendError && (
              <p className="text-[11.5px] leading-relaxed text-rose-300/80">{backendError}</p>
            )}
            <Button
              variant="secondary"
              size="sm"
              icon={IconRefresh}
              loading={checking}
              onClick={onRefresh}
            >
              Refresh status
            </Button>
          </div>

          <h3 className="mt-6 flex items-center gap-2 text-[13px] font-semibold text-slate-200">
            <IconTrash size={14} className="text-slate-500" /> Local data
          </h3>
          <div className="mt-4 space-y-2">
            <div className="flex items-center justify-between rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
              <p className="text-xs text-slate-300">Clear saved results</p>
              <Button
                variant={confirmSaved ? "danger" : "secondary"}
                size="sm"
                onClick={() => {
                  if (confirmSaved) {
                    onClearSaved();
                    setConfirmSaved(false);
                  } else {
                    setConfirmSaved(true);
                  }
                }}
              >
                {confirmSaved ? "Confirm?" : "Clear"}
              </Button>
            </div>
          </div>
        </Glass>
      </div>

      <Glass className="p-5">
        <h3 className="flex items-center gap-2 text-[13px] font-semibold text-slate-200">
          <IconAlert size={14} className="text-slate-500" /> Security notes
        </h3>
        <ul className="mt-3 grid gap-2 text-[12px] leading-relaxed text-slate-400 md:grid-cols-3">
          <li className="rounded-lg border border-white/[0.06] bg-white/[0.02] p-3">
            <p className="font-medium text-slate-300">Keys stay server-side</p>
            <p className="mt-1">
              API credentials are read from backend/.env and are never
              requested, displayed or exported in this UI.
            </p>
          </li>
          <li className="rounded-lg border border-white/[0.06] bg-white/[0.02] p-3">
            <p className="font-medium text-slate-300">Untrusted content</p>
            <p className="mt-1">
              Scraped pages and model output are treated as data — parsed,
              normalized and filtered locally, never executed.
            </p>
          </li>
          <li className="rounded-lg border border-white/[0.06] bg-white/[0.02] p-3">
            <p className="font-medium text-slate-300">Request guardrails</p>
            <p className="mt-1">
              Loopback, link-local and private-network targets are blocked;
              crawl scope, depth and rate are capped per run.
            </p>
          </li>
        </ul>
      </Glass>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Application                                                                */
/* -------------------------------------------------------------------------- */


function HeroSection() {
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const vid = videoRef.current;
    if (!vid) return;
    
    // Custom JS-controlled fade loop
    const handlePlay = () => {
       vid.style.transition = 'opacity 0.5s ease-in';
       vid.style.opacity = '1';
    };
    
    const handleTimeUpdate = () => {
       if (vid.duration && vid.currentTime >= vid.duration - 0.5) {
          vid.style.transition = 'opacity 0.5s ease-out';
          vid.style.opacity = '0';
       }
    };
    
    const handleEnded = () => {
       vid.style.transition = 'none';
       vid.style.opacity = '0';
       setTimeout(() => {
          vid.play().catch(()=>{});
       }, 100);
    };

    vid.addEventListener('playing', handlePlay);
    vid.addEventListener('timeupdate', handleTimeUpdate);
    vid.addEventListener('ended', handleEnded);
    
    return () => {
       vid.removeEventListener('playing', handlePlay);
       vid.removeEventListener('timeupdate', handleTimeUpdate);
       vid.removeEventListener('ended', handleEnded);
    };
  }, []);

  const logos = ['Vortex', 'Nimbus', 'Prysma', 'Cirrus', 'Kynder', 'Halcyn'];

  const scrollToScraper = () => {
    document.getElementById('scraper-app')?.scrollIntoView({ behavior: 'smooth' });
  };

  return (
    <div className="hero-wrapper relative min-h-screen flex flex-col overflow-hidden">
      {/* Background Video */}
      <div className="absolute inset-0 overflow-hidden w-full h-full -z-20">
        <video 
          ref={videoRef}
          src="https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260328_065045_c44942da-53c6-4804-b734-f9e07fc22e08.mp4"
          className="absolute inset-0 w-full h-full object-cover opacity-0"
          autoPlay muted playsInline
        />
      </div>

      {/* Blurred overlay shape */}
      <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[984px] h-[527px] opacity-90 bg-gray-950 blur-[82px] pointer-events-none -z-10 rounded-full" />

      {/* Navbar */}
      <nav className="w-full py-5 px-8 flex flex-row items-center justify-between z-10 relative">
        <div className="flex items-center">
          <img src="/assets/logo.png" alt="Logo" style={{ height: '32px' }} onError={(e) => { e.currentTarget.style.display='none'; }} />
        </div>
        <div className="flex items-center gap-6">
          <button className="text-[var(--fg-color)] opacity-90 hover:opacity-100 transition-opacity flex items-center gap-1 font-medium">Features <IconChevronDown size={14}/></button>
          <button className="text-[var(--fg-color)] opacity-90 hover:opacity-100 transition-opacity font-medium">Solutions</button>
          <button className="text-[var(--fg-color)] opacity-90 hover:opacity-100 transition-opacity font-medium">Plans</button>
          <button className="text-[var(--fg-color)] opacity-90 hover:opacity-100 transition-opacity flex items-center gap-1 font-medium">Learning <IconChevronDown size={14}/></button>
        </div>
        <div>
          <button className="heroSecondary px-4 py-2 text-sm">Sign Up</button>
        </div>
      </nav>
      {/* Divider */}
      <div className="w-full h-[1px] bg-gradient-to-r from-transparent via-[var(--fg-color)] opacity-20 to-transparent mt-[3px] z-10 relative" />

      {/* Hero Content */}
      <div className="flex-1 flex flex-col items-center justify-center text-center z-10 relative px-4">
        <h1 className="text-[120px] md:text-[220px] font-normal leading-[1.02] tracking-[-0.024em]">
          Power <span className="bg-clip-text text-transparent" style={{ backgroundImage: 'linear-gradient(to left, #6366f1, #a855f7, #fcd34d)' }}>AI</span>
        </h1>
        <p className="text-[var(--sub-color)] text-lg leading-8 max-w-md mt-[9px] opacity-80">
          The most powerful AI ever deployed<br/>in talent acquisition
        </p>
        <button onClick={scrollToScraper} className="heroSecondary px-[29px] py-[24px] mt-[25px] text-lg">
          Schedule a Consult
        </button>
      </div>

      {/* Logo Marquee */}
      <div className="w-full z-10 relative pb-10">
        <div className="max-w-5xl mx-auto flex flex-col md:flex-row items-center gap-12 px-6">
          <div className="text-[var(--fg-color)] opacity-50 text-sm whitespace-nowrap text-center md:text-left leading-relaxed">
            Relied on by brands<br/>across the globe
          </div>
          <div className="marquee-container flex-1 mask-image: linear-gradient(to right, transparent, black 10%, black 90%, transparent)">
            <div className="marquee-content">
              {[...logos, ...logos, ...logos].map((name, i) => (
                <div key={i} className="flex items-center gap-3">
                  <div className="liquid-glass w-8 h-8 rounded-lg flex items-center justify-center font-bold text-[14px]">
                    {name[0]}
                  </div>
                  <span className="text-base font-semibold text-[var(--fg-color)]">{name}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function ScrapeworkPage() {
  const [view, setView] = useState<View>("scrape");
  const [form, setForm] = useState<FormState>(DEFAULT_FORM);
  const [settings, setSettings] = useLocalStorage<AppSettings>(
    "scrapework.settings.v1",
    DEFAULT_SETTINGS,
  );
  const [history, setHistory] = useState<RunRecord[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [saved, setSaved] = useLocalStorage<RunRecord[]>(
    "scrapework.saved.v1",
    [],
  );
  const [selected, setSelected] = useState<RunRecord | null>(null);
  const { status, error: statusError, checking: statusChecking, refresh: refreshStatus } =
    useBackendStatus();

  // Load persisted backend history whenever the History view opens.
  useEffect(() => {
    if (view !== "history") return;
    let cancelled = false;
    setHistoryLoading(true);
    Api.history()
      .then((runs) => {
        if (!cancelled) setHistory(runs);
      })
      .catch(() => {
        if (!cancelled) setHistory([]);
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [view]);

  const onFinished = useCallback(
    (record: RunRecord) => {
      setSelected(record);
    },
    [],
  );
  const { run, start, cancel, reset } = useScrapeRunner(onFinished);

  const mainUrlEntries = useMemo(() => parseUrlList(form.urlsRaw), [form.urlsRaw]);
  const validUrlCount = mainUrlEntries.filter(
    (e) => !e.error || e.error.startsWith("duplicate"),
  ).length;
  const invalidCount = mainUrlEntries.filter(
    (e) => e.error && !e.error.startsWith("duplicate"),
  ).length;

  const valid = useMemo(
    () => ({
      urls: validUrlCount > 0 && invalidCount === 0,
      prompt: form.prompt.trim().length >= 8,
    }),
    [validUrlCount, invalidCount, form.prompt],
  );

  const handleRun = useCallback(() => {
    if (!valid.urls || !valid.prompt) return;
    setSelected(null);
    const urls = parseUrlList(form.urlsRaw)
      .filter((e) => !e.error)
      .map((e) => e.url)
      .slice(0, MAX_URLS);
    const config: ScrapeConfig = {
      urls,
      prompt: form.prompt.trim(),
      mode: form.mode,
      maxPages: form.mode === "single" ? 1 : form.maxPages,
      maxDepth: form.maxDepth,
      sameDomain: form.sameDomain,
      timeoutSec: form.timeoutSec,
      retries: form.retries,
      delayMs: form.delayMs,
      llmTimeoutSec: form.llmTimeoutSec,
      schemaFields: form.schemaFields
        .split(",")
        .map((f) => f.trim())
        .filter(Boolean),
      stripNoise: form.stripNoise,
      provider: form.provider,
    };
    void start(config);
  }, [form, valid, start]);

  const handleReset = useCallback(() => {
    setForm({
      ...DEFAULT_FORM,
      provider: settings.defaultProvider,
      timeoutSec: settings.defaultTimeoutSec,
      delayMs: settings.defaultDelayMs,
      maxPages: settings.defaultMaxPages,
      stripNoise: settings.defaultStripNoise,
    });
    setSelected(null);
  }, [settings]);

  const openRecord = useCallback(
    (record: RunRecord) => {
      setSelected(record);
      setView("scrape");
      window.scrollTo({ top: 0, behavior: "smooth" });
    },
    [],
  );

  const navigate = useCallback((next: View) => {
    setView(next);
    window.scrollTo({ top: 0 });
  }, []);

  const displayedRecord: RunRecord | null = run ?? selected;

  const mobileNav = (
    <nav
      aria-label="Main"
      className="sg-scroll -mx-4 flex gap-1 overflow-x-auto px-4 pb-3 lg:hidden"
    >
      {NAV_ITEMS.map((item) => {
        const active = view === item.view;
        return (
          <button
            key={item.view}
            type="button"
            aria-current={active ? "page" : undefined}
            onClick={() => navigate(item.view)}
            className={cn(
              "flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/40",
              active
                ? "border-white/[0.12] bg-white/[0.07] text-slate-100"
                : "border-white/[0.06] bg-white/[0.03] text-slate-400",
            )}
          >
            <item.icon size={13} />
            {item.label}
          </button>
        );
      })}
    </nav>
  );

  return (
    <div className="flex flex-col w-full h-full overflow-y-auto">
      <HeroSection />
      <div id="scraper-app" className="min-h-screen bg-[#0B0E14] text-slate-200 antialiased relative z-20">
      <style>{CSS_GLOBALS}</style>

      {/* Ambient backdrop */}
      <div aria-hidden className="pointer-events-none fixed inset-0">
        <div className="absolute inset-0 bg-[#0B0E14]" />
        <div className="sg-grid-bg absolute inset-0" />
        <div className="absolute -left-40 -top-40 h-[34rem] w-[34rem] rounded-full bg-[#4653c9]/[0.15] blur-[130px]" />
        <div className="absolute -right-48 top-1/3 h-[30rem] w-[30rem] rounded-full bg-[#0e7490]/[0.11] blur-[130px]" />
        <div className="absolute -bottom-56 left-1/3 h-[28rem] w-[28rem] rounded-full bg-[#7c5cff]/[0.07] blur-[140px]" />
        <div
          className="absolute inset-0"
          style={{
            background:
              "radial-gradient(120% 90% at 50% 0%, transparent 45%, rgba(5,7,12,0.75) 100%)",
          }}
        />
      </div>

      <div className="relative mx-auto flex min-h-screen w-full max-w-[1520px]">
        <Sidebar
          view={view}
          onNavigate={navigate}
          historyCount={history.length}
          savedCount={saved.length}
          status={status}
          statusError={statusError}
          statusChecking={statusChecking}
          onRefreshStatus={() => void refreshStatus()}
        />

        <main className="min-w-0 flex-1 px-4 pb-16 pt-6 sm:px-6 lg:px-8">
          {/* Mobile brand + nav */}
          <div className="mb-4 flex items-center gap-3 lg:hidden">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-white/[0.1] bg-white/[0.05] text-sky-200">
              <IconBrand size={17} />
            </div>
            <p className="text-[15px] font-semibold tracking-tight text-slate-100">
              AI Web Scrapper
            </p>
            <div className="ml-auto">
              <ProviderCard
                status={status}
                error={statusError}
                checking={statusChecking}
                onRefresh={() => void refreshStatus()}
              />
            </div>
          </div>
          {mobileNav}

          {view === "scrape" && (
            <div className="space-y-6">
              <Workspace
                form={form}
                setForm={(patch) => setForm((prev) => ({ ...prev, ...patch }))}
                valid={valid}
                backend={status}
                onRun={handleRun}
                onCancel={cancel}
                running={run?.status === "running"}
                onReset={handleReset}
              />

              {run && <ConsolePanel run={run} />}

              {displayedRecord ? (
                <ResultsPanel record={displayedRecord} />
              ) : (
                <EmptyState
                  icon={IconTable}
                  title="No results yet"
                  body="Configure a target and instructions, then run the agent. This workspace will fill with real metrics, rows and diagnostics streamed from the backend."
                  action={
                    <Button
                      variant="secondary"
                      size="sm"
                      icon={IconPlay}
                      onClick={() =>
                        document
                          .querySelector<HTMLInputElement>('input[aria-label="Target URL"]')
                          ?.focus()
                      }
                    >
                      Start with the URL
                    </Button>
                  }
                />
              )}
            </div>
          )}

          {view === "history" && (
            <HistoryView
              records={history}
              loading={historyLoading}
              onOpen={openRecord}
              onSave={(record) =>
                setSaved((prev) => [record, ...prev.filter((r) => r.id !== record.id)].slice(0, 30))
              }
              onDelete={(id) => setHistory((prev) => prev.filter((r) => r.id !== id))}
              onClearAll={() => setHistory([])}
            />
          )}

          {view === "saved" && (
            <SavedView
              records={saved}
              onOpen={openRecord}
              onDelete={(id) => setSaved((prev) => prev.filter((r) => r.id !== id))}
            />
          )}

          {view === "settings" && (
            <SettingsView
              settings={settings}
              setSettings={(patch) => setSettings((prev) => ({ ...prev, ...patch }))}
              backend={status}
              backendError={statusError}
              checking={statusChecking}
              onRefresh={() => void refreshStatus()}
              onClearSaved={() => setSaved([])}
            />
          )}

          <footer className="mt-12 flex flex-wrap items-center justify-between gap-2 border-t border-white/[0.05] pt-5 text-[11px] text-slate-600">
            <p>
              AI Web Scrapper · SARA Nexus Hub agent · live API under{" "}
              <span className="sg-mono">/api/agent</span>
            </p>
            <p>Content is untrusted data — parsed, normalized, never executed.</p>
          </footer>
        </main>
      </div>
    </div>
    </div>
  );
}