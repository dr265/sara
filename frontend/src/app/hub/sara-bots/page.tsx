"use client";

/**
 * SARA BOTS — the real-agent workspace.
 *
 * Every card here is a REAL agent from the backend registry:
 *   • hub bots      — the platform's live backend agents (real health probes)
 *   • agency bot    — the 267 specialist .md agents (real /api/agency route)
 *   • local bots    — sara_desktop actions discovered STATICALLY (AST only),
 *                     executed in a separate process only after approval
 *
 * Statuses come from the backend and are rendered verbatim — the UI never
 * invents an "online", a progress bar, or a result. If a run fails, it shows
 * the failure. If an agent needs configuration, it says exactly that.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { SARA_API_URL, saraFetch } from "@/lib/api";
import CustomBots from "./custom-bots";
import { SB_CSS } from "./sb-css";

/** Fleet cards that are full workspaces of their own — clicking opens them
 *  directly instead of the fleet detail panel (backend registry ids). */
const DIRECT_OPEN_IDS = new Set(["saramuse", "sales-agent"]);

/* ── types (mirrors backend payload) ─────────────────────────────────────── */

type FieldSpec = { type: string; description: string; required: boolean; default?: unknown };

interface Bot {
  id: string;
  name: string;
  kind: "hub" | "agency" | "local";
  category: string;
  description: string;
  capabilities: string[];
  adapter: string | null;
  accepts: Record<string, FieldSpec>;
  route: string | null;
  probe: string | null;
  requires: string[];
  cancellable: boolean;
  hue: number;
  source_path?: string;
  source_module?: string;
  state?: string;
  detail?: string;
  invocable?: boolean;
  why_not?: string;
  approved?: boolean;
  orb?: { hue: number };
  note?: string;
}

interface RunRecord {
  run_id: string;
  bot_id: string;
  bot_name: string;
  kind: string;
  status: "queued" | "running" | "completed" | "failed" | "timed_out" | "cancelled";
  params_preview: string;
  output: string;
  error: string;
  events: { t: number; event: string }[];
  started_at: number | null;
  finished_at: number | null;
  duration_ms: number | null;
  exit_code: number | null;
  pid: number | null;
}

interface ScanInfo {
  root: string | null;
  configured_root: string;
  default_root: string;
  approved_scope: string;
  discovered: number;
  problems: { file: string; reason: string }[];
}

const STATE_META: Record<string, { label: string; color: string; bg: string }> = {
  available: { label: "Available", color: "#34d399", bg: "rgba(52,211,153,0.12)" },
  ready: { label: "Ready", color: "#22d3ee", bg: "rgba(34,211,238,0.12)" },
  awaiting_approval: { label: "Awaiting approval", color: "#a78bfa", bg: "rgba(167,139,250,0.14)" },
  needs_configuration: { label: "Needs configuration", color: "#fbbf24", bg: "rgba(251,191,36,0.12)" },
  unavailable: { label: "Unavailable", color: "#f87171", bg: "rgba(248,113,113,0.10)" },
  disabled: { label: "Disabled", color: "#9ca3af", bg: "rgba(156,163,175,0.10)" },
};

const RUN_STATE_META: Record<string, { label: string; color: string }> = {
  queued: { label: "Queued", color: "#9ca3af" },
  running: { label: "Running", color: "#22d3ee" },
  completed: { label: "Completed", color: "#34d399" },
  failed: { label: "Failed", color: "#f87171" },
  timed_out: { label: "Timed out", color: "#fb923c" },
  cancelled: { label: "Cancelled", color: "#9ca3af" },
};

const TERMINAL_RUN_STATES = new Set(["completed", "failed", "timed_out", "cancelled"]);

/* ── the orb — pure CSS 3D sphere, state-driven, honors reduced motion ────── */

function Orb({ bot, state, size = 92, active = false }: {
  bot: Bot; state: string; size?: number; active?: boolean;
}) {
  const hue = bot.orb?.hue ?? bot.hue ?? 200;
  const dim = state === "disabled" || state === "unavailable";
  const urgent = state === "awaiting_approval" || state === "needs_configuration";
  const stateClass =
    active ? "sb-orb-running"
      : state === "awaiting_approval" ? "sb-orb-attention"
        : state === "disabled" || state === "unavailable" ? "sb-orb-dim"
          : state === "needs_configuration" ? "sb-orb-warn" : "";
  return (
    <span
      aria-hidden="true"
      className={`sb-orb ${stateClass}`}
      style={{
        width: size, height: size,
        ["--orb-hue" as string]: String(hue),
        ["--orb-glow" as string]: dim ? "0.12" : urgent ? "0.75" : "0.5",
      }}
    >
      <span className="sb-orb-core" />
      <span className="sb-orb-atmo" />
      <span className="sb-orb-ring" />
      <span className="sb-orb-spec" />
    </span>
  );
}

/* ── small parts ──────────────────────────────────────────────────────────── */

function StatePill({ state }: { state: string }) {
  const meta = STATE_META[state] ?? { label: state, color: "#9ca3af", bg: "rgba(156,163,175,0.1)" };
  return (
    <span
      className="sb-pill"
      style={{ color: meta.color, background: meta.bg, borderColor: `${meta.color}33` }}
    >
      <span className="sb-pill-dot" style={{ background: meta.color }} />
      {meta.label}
    </span>
  );
}

function FieldInput({ field, value, onChange, id }: {
  field: FieldSpec; value: string; onChange: (v: string) => void; id: string;
}) {
  if (field.type === "boolean") {
    return (
      <label htmlFor={id} className="sb-field-check">
        <input id={id} type="checkbox" checked={value === "true"}
          onChange={(e) => onChange(e.target.checked ? "true" : "false")} />
        <span>{field.description || "enable"}</span>
      </label>
    );
  }
  if (field.type === "array") {
    return (
      <textarea
        id={id}
        className="sb-input sb-textarea"
        rows={3}
        placeholder={field.description || "one entry per line"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <input
      id={id}
      className="sb-input"
      type={field.type === "number" ? "number" : "text"}
      placeholder={field.description || ""}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

function formatRunWhen(record: RunRecord): string {
  const ts = record.finished_at ?? record.started_at;
  if (!ts) return "";
  const s = Math.round((Date.now() / 1000 - ts));
  if (s < 60) return `${Math.max(s, 0)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

/* ── page ─────────────────────────────────────────────────────────────────── */

export default function SaraBotsPage() {
  const router = useRouter();
  const [bots, setBots] = useState<Bot[]>([]);
  const [scan, setScan] = useState<ScanInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [probing, setProbing] = useState(false);
  const [search, setSearch] = useState("");
  const [kindFilter, setKindFilter] = useState<"all" | "hub" | "local" | "agency">("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeRun, setActiveRun] = useState<RunRecord | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [notice, setNotice] = useState("");
  const [noticeKind, setNoticeKind] = useState<"info" | "error" | "ok">("info");
  const [confirmPayload, setConfirmPayload] = useState(false);
  // Fleet = the registered real agents; My Bots = custom worker bots you build.
  const [view, setView] = useState<"fleet" | "mine">("fleet");

  // run form state: { botId: { fieldKey: value } }
  const [formValues, setFormValues] = useState<Record<string, Record<string, string>>>({});

  const load = useCallback(async (refreshProbes: boolean) => {
    if (refreshProbes) setProbing(true);
    try {
      const q = refreshProbes ? "?probe=refresh" : "";
      const res = await saraFetch(`${SARA_API_URL}/api/sara-bots/bots${q}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`backend responded ${res.status}`);
      const body = await res.json();
      setBots(body.bots ?? []);
      setScan(body.scan ?? null);
      setLoadError("");
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
      setProbing(false);
    }
  }, []);

  // initial registry load — async callback so state updates never fire
  // synchronously inside the effect body
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await saraFetch(`${SARA_API_URL}/api/sara-bots/bots`, { cache: "no-store" });
        if (!res.ok) throw new Error(`backend responded ${res.status}`);
        const body = await res.json();
        if (!alive) return;
        setBots(body.bots ?? []);
        setScan(body.scan ?? null);
        setLoadError("");
      } catch (err) {
        if (alive) setLoadError(err instanceof Error ? err.message : String(err));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  // stop any run poller on unmount
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const pollRun = useCallback((runId: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const res = await saraFetch(`${SARA_API_URL}/api/sara-bots/runs/${runId}`, { cache: "no-store" });
        if (!res.ok) return;
        const record: RunRecord = await res.json();
        setActiveRun(record);
        if (TERMINAL_RUN_STATES.has(record.status) && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
          load(false);
        }
      } catch { /* transient — keep polling */ }
    }, 1000);
  }, [load]);

  const selected = useMemo(
    () => bots.find((b) => b.id === selectedId) ?? null,
    [bots, selectedId]);

  const filtered = useMemo(() => {
    const q = search.toLowerCase().trim();
    return bots.filter((b) => {
      if (kindFilter !== "all" && b.kind !== kindFilter) return false;
      if (!q) return true;
      return (
        b.name.toLowerCase().includes(q) ||
        b.category.toLowerCase().includes(q) ||
        b.description.toLowerCase().includes(q) ||
        b.capabilities.some((c) => c.toLowerCase().includes(q))
      );
    });
  }, [bots, search, kindFilter]);

  const counts = useMemo(() => {
    const byState: Record<string, number> = {};
    for (const b of bots) byState[b.state ?? "?"] = (byState[b.state ?? "?"] ?? 0) + 1;
    return byState;
  }, [bots]);

  /* ── actions ─────────────────────────────────────────────────────────── */

  const startRun = useCallback(async (bot: Bot) => {
    setNotice(""); setNoticeKind("info");
    const wantsConfirm = formValues[bot.id]?.__confirm === "true";
    if (wantsConfirm) setConfirmPayload(false);
    const params: Record<string, unknown> = {};
    for (const [key, field] of Object.entries(bot.accepts ?? {})) {
      const raw = formValues[bot.id]?.[key] ?? "";
      if (raw === "" && !field.required) continue;
      if (field.type === "number") params[key] = Number(raw);
      else if (field.type === "boolean") params[key] = raw === "true";
      else if (field.type === "array") {
        const items = raw.split("\n").map((s) => s.trim()).filter(Boolean);
        if (items.length) params[key] = items;
      } else params[key] = raw;
    }
    try {
      const res = await saraFetch(`${SARA_API_URL}/api/sara-bots/bots/${encodeURIComponent(bot.id)}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ params, timeout_s: 90, confirm: wantsConfirm }),
      });
      if (res.status === 409) {
        // backend demands confirmation — show the reason and ask
        const body = await res.json().catch(() => ({}));
        const detail = typeof body.detail === "string" ? body.detail : "This run needs confirmation.";
        setNotice(`${detail} Tick “confirm this run” and submit again to execute exactly this payload.`);
        setNoticeKind("error");
        setConfirmPayload(true);
        return;
      }
      if (res.status === 403) {
        const body = await res.json().catch(() => ({}));
        setNotice(typeof body.detail === "string" ? body.detail : "Not authorized.");
        setNoticeKind("error");
        return;
      }
      if (res.status === 429) {
        setNotice("Rate limit reached — wait a moment and retry.");
        setNoticeKind("error");
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setNotice(typeof body.detail === "string" ? body.detail : `Run refused (HTTP ${res.status}).`);
        setNoticeKind("error");
        return;
      }
      const record: RunRecord = await res.json();
      setActiveRun(record);
      setNotice(`Run ${record.run_id} started — live status below.`);
      setNoticeKind("ok");
      pollRun(record.run_id);
      load(false);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err));
      setNoticeKind("error");
    }
  }, [formValues, pollRun, load]);

  const cancelRun = useCallback(async () => {
    if (!activeRun) return;
    try {
      const res = await saraFetch(`${SARA_API_URL}/api/sara-bots/runs/${activeRun.run_id}/cancel`, { method: "POST" });
      if (res.ok) {
        setActiveRun(await res.json());
        setNotice("Cancellation sent — the backend terminates the real process tree.");
        setNoticeKind("info");
      } else {
        const body = await res.json().catch(() => ({}));
        setNotice(typeof body.detail === "string" ? body.detail : "Could not cancel.");
        setNoticeKind("error");
      }
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err));
      setNoticeKind("error");
    }
  }, [activeRun]);

  const approval = useCallback(async (bot: Bot, action: "approve" | "revoke" | "disable" | "enable") => {
    try {
      const res = await saraFetch(
        `${SARA_API_URL}/api/sara-bots/bots/${encodeURIComponent(bot.id)}/approval`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action }) });
      if (res.ok) {
        setNotice(`Bot ${action === "approve" ? "approved — it can now execute for real" : action === "revoke" ? "approval revoked" : action === "disable" ? "disabled — source untouched" : "enabled"}.`);
        setNoticeKind(action === "approve" ? "ok" : "info");
        await load(false);
      } else {
        const body = await res.json().catch(() => ({}));
        setNotice(typeof body.detail === "string" ? body.detail : "Approval action failed.");
        setNoticeKind("error");
      }
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err));
      setNoticeKind("error");
    }
  }, [load]);

  const rescan = useCallback(async () => {
    setNotice("");
    try {
      const res = await saraFetch(`${SARA_API_URL}/api/sara-bots/rescan`, { method: "POST" });
      const body = await res.json();
      if (res.ok) {
        setNotice(`Discovery complete — ${body.discovered} action file(s) parsed statically, none executed.`);
        setNoticeKind("ok");
        await load(false);
      } else {
        setNotice(typeof body.detail === "string" ? body.detail : "Rescan failed.");
        setNoticeKind("error");
      }
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err));
      setNoticeKind("error");
    }
  }, [load]);

  /* ── render ───────────────────────────────────────────────────────────── */

  return (
    <main className="relative z-10 min-h-screen w-full bg-transparent text-white pb-24">
      <style>{SB_CSS}</style>

      {/* header */}
      <header className="sb-header">
        <div>
          <div className="sb-eyebrow">NEXUS · REAL AGENT WORKSPACE</div>
          <h1 className="sb-title">Sara Bots</h1>
          <p className="sb-sub">
            Every orb is a real agent — live backend services, the agency specialist fleet,
            approved local automations — plus the worker bots you build yourself under
            “My Bots”. Statuses come from actual health probes and executions; nothing
            here is simulated.
          </p>
        </div>
        <div className="sb-header-actions">
          <div className="sb-kind-switch" role="tablist" aria-label="Sara Bots view">
            <button role="tab" aria-selected={view === "fleet"}
              className={`sb-kind-btn ${view === "fleet" ? "sb-kind-on" : ""}`}
              onClick={() => setView("fleet")}>
              Fleet
            </button>
            <button role="tab" aria-selected={view === "mine"}
              className={`sb-kind-btn ${view === "mine" ? "sb-kind-on" : ""}`}
              onClick={() => setView("mine")}>
              My Bots
            </button>
          </div>
          {view === "fleet" && (
            <>
              <button className="sb-btn" onClick={() => load(true)} disabled={probing}>
                {probing ? "Probing…" : "Re-probe fleet"}
              </button>
              <button className="sb-btn" onClick={rescan}>Rescan local bots</button>
            </>
          )}
        </div>
      </header>

      {view === "mine" ? (
        <CustomBots />
      ) : (
        <>
      {/* banner */}
      {notice && (
        <div className={`sb-banner sb-banner-${noticeKind}`} role="status">
          {notice}
          <button className="sb-banner-x" onClick={() => setNotice("")} aria-label="Dismiss">×</button>
        </div>
      )}
      {loadError && (
        <div className="sb-banner sb-banner-error" role="alert">
          Backend unreachable: {loadError} — start it with <code>python run_backend.py</code>.
        </div>
      )}

      {/* summary + filters */}
      <section className="sb-toolbar">
        <div className="sb-counts">
          {Object.entries(counts).map(([state]) => (
            <StatePill key={state} state={state} />
          )).slice(0, 6)}
          <span className="sb-count-total">{bots.length} bots</span>
        </div>
        <div className="sb-toolbar-right">
          <div className="sb-kind-switch" role="tablist" aria-label="Bot source">
            {(["all", "hub", "local", "agency"] as const).map((k) => (
              <button key={k} role="tab" aria-selected={kindFilter === k}
                className={`sb-kind-btn ${kindFilter === k ? "sb-kind-on" : ""}`}
                onClick={() => setKindFilter(k)}>
                {k === "all" ? "All" : k === "hub" ? "Hub agents" : k === "local" ? "Local actions" : "Agency"}
              </button>
            ))}
          </div>
          <input
            className="sb-input sb-search"
            placeholder="Search bots by name, category, or capability…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search bots"
          />
        </div>
      </section>

      {/* gallery */}
      {loading ? (
        <div className="sb-empty">Loading the real registry…</div>
      ) : filtered.length === 0 ? (
        <div className="sb-empty">
          No bots match your filter.
          <button className="sb-btn sb-mt" onClick={() => { setSearch(""); setKindFilter("all"); }}>
            Reset filters
          </button>
        </div>
      ) : (
        <section className="sb-grid" aria-label="Registered bots">
          {filtered.map((bot) => (
            <button
              key={bot.id}
              className={`sb-card ${selectedId === bot.id ? "sb-card-on" : ""}`}
              onClick={() => {
                // SaraMuse is a full workspace of its own — open it directly
                // instead of showing the fleet detail panel.
                if (DIRECT_OPEN_IDS.has(bot.id) && bot.route) { router.push(bot.route); return; }
                setSelectedId(bot.id); setNotice(""); setActiveRun(null); setConfirmPayload(false);
              }}
              aria-pressed={selectedId === bot.id}
            >
              <div className="sb-card-orb">
                <Orb bot={bot} state={bot.state ?? ""} size={84} active={activeRun?.bot_id === bot.id && !TERMINAL_RUN_STATES.has(activeRun.status)} />
              </div>
              <div className="sb-card-body">
                <div className="sb-card-name-row">
                  <span className="sb-card-name">{bot.name}</span>
                  <span className="sb-kind-tag">{bot.kind === "hub" ? "HUB" : bot.kind === "agency" ? "AGENCY" : "LOCAL"}</span>
                </div>
                <div className="sb-card-desc">{bot.description}</div>
                <div className="sb-card-foot">
                  <StatePill state={bot.state ?? ""} />
                  {bot.adapter ? (
                    <span className="sb-runnable">runnable</span>
                  ) : (
                    <span className="sb-link-tag" title="This agent runs in its own workspace">workspace →</span>
                  )}
                </div>
              </div>
            </button>
          ))}
        </section>
      )}

      {/* details — SaraMuse never renders this panel; its card opens the workspace */}
      {selected && !DIRECT_OPEN_IDS.has(selected.id) && (
        <section className="sb-detail" aria-label={`Details for ${selected.name}`}>
          <div className="sb-detail-head">
            <Orb bot={selected} state={selected.state ?? ""} size={64}
              active={activeRun?.bot_id === selected.id && !TERMINAL_RUN_STATES.has(activeRun?.status ?? "completed")} />
            <div className="sb-detail-title">
              <h2>{selected.name}</h2>
              <div className="sb-detail-meta">
                <StatePill state={selected.state ?? ""} />
                <span className="sb-kind-tag">{selected.kind.toUpperCase()}</span>
                <span className="sb-detail-cat">{selected.category}</span>
              </div>
            </div>
            <div className="sb-detail-actions">
              {selected.kind === "local" && (
                selected.approved ? (
                  <>
                    <button className="sb-btn" onClick={() => approval(selected, "revoke")}>Revoke approval</button>
                    <button className="sb-btn" onClick={() => approval(selected, "disable")}>Disable</button>
                  </>
                ) : (
                  <button className="sb-btn sb-btn-primary" onClick={() => approval(selected, "approve")}>
                    Approve execution
                  </button>
                )
              )}
              {selected.kind === "local" && selected.approved && selected.state !== "disabled" && (
                <button className="sb-btn sb-btn-ghost" onClick={() => approval(selected, "disable")}>Disable</button>
              )}
              {selected.kind === "local" && selected.state === "disabled" && (
                <button className="sb-btn sb-btn-primary" onClick={() => approval(selected, "enable")}>Enable</button>
              )}
              {selected.kind !== "local" && (
                <button className="sb-btn" onClick={() => approval(selected, selected.state === "disabled" ? "enable" : "disable")}>
                  {selected.state === "disabled" ? "Enable" : "Disable"}
                </button>
              )}
            </div>
          </div>

          <p className="sb-detail-desc">{selected.description}</p>

          {selected.detail && (
            <p className="sb-detail-note">
              <strong>Status detail:</strong> {selected.detail}
            </p>
          )}
          {!selected.adapter && selected.why_not && (
            <p className="sb-detail-note sb-detail-warn">
              This bot has no direct run contract from Sara Bots — {selected.why_not}.{" "}
              {selected.route ? <a className="sb-link" href={selected.route}>Open the {selected.name} workspace →</a> : null}
            </p>
          )}
          {selected.requires?.length > 0 && (
            <p className="sb-detail-note sb-detail-warn">
              Requires: {selected.requires.join("; ")}
            </p>
          )}
          {selected.source_path && (
            <p className="sb-detail-note">
              Source (read-only, static analysis): <code>{selected.source_path}</code>
              {selected.note ? <> — {selected.note}</> : null}
            </p>
          )}

          {/* run form — only when the backend says this is invocable */}
          {selected.adapter && (
            <form
              className="sb-runform"
              onSubmit={(e) => { e.preventDefault(); startRun(selected); }}
            >
              <h3 className="sb-runform-title">Run {selected.name}</h3>
              {Object.keys(selected.accepts ?? {}).length === 0 && (
                <p className="sb-detail-note">This bot takes no parameters — submit to execute it.</p>
              )}
              {Object.entries(selected.accepts ?? {}).map(([key, field]) => (
                <div className="sb-field" key={key}>
                  <label htmlFor={`f-${selected.id}-${key}`} className="sb-field-label">
                    {key}
                    {field.required ? <span className="sb-req"> *</span> : null}
                    <span className="sb-field-type">{field.type}</span>
                  </label>
                  <FieldInput
                    id={`f-${selected.id}-${key}`}
                    field={field}
                    value={formValues[selected.id]?.[key] ?? ""}
                    onChange={(v) => setFormValues((prev) => ({
                      ...prev,
                      [selected.id]: { ...(prev[selected.id] ?? {}), [key]: v },
                    }))}
                  />
                </div>
              ))}

              {confirmPayload && (
                <label className="sb-confirm-row">
                  <input type="checkbox" checked={formValues[selected.id]?.__confirm === "true"}
                    onChange={(e) => setFormValues((prev) => ({
                      ...prev,
                      [selected.id]: { ...(prev[selected.id] ?? {}), __confirm: e.target.checked ? "true" : "false" },
                    }))} />
                  <span>Confirm this run — I understand exactly what will execute.</span>
                </label>
              )}

              <div className="sb-runform-foot">
                <button
                  type="submit"
                  className="sb-btn sb-btn-primary"
                  disabled={selected.state === "disabled" || (selected.kind === "local" && !selected.approved)}
                >
                  {activeRun && activeRun.bot_id === selected.id && !TERMINAL_RUN_STATES.has(activeRun.status)
                    ? "Run in progress…" : "Execute"}
                </button>
                {activeRun && activeRun.bot_id === selected.id && !TERMINAL_RUN_STATES.has(activeRun.status) && (
                  <button type="button" className="sb-btn" onClick={cancelRun}>Cancel run</button>
                )}
                {selected.state === "disabled" && (
                  <span className="sb-detail-warn">This bot is disabled — enable it first.</span>
                )}
                {selected.kind === "local" && !selected.approved && selected.state !== "disabled" && (
                  <span className="sb-detail-warn">Approve this bot before it can execute.</span>
                )}
              </div>
            </form>
          )}

          {/* live run panel */}
          {activeRun && activeRun.bot_id === selected.id && (
            <div className="sb-runpanel">
              <div className="sb-runpanel-head">
                <span className="sb-run-id">run {activeRun.run_id}</span>
                <span className="sb-run-status" style={{ color: RUN_STATE_META[activeRun.status]?.color }}>
                  <span className="sb-pill-dot" style={{ background: RUN_STATE_META[activeRun.status]?.color }} />
                  {RUN_STATE_META[activeRun.status]?.label ?? activeRun.status}
                </span>
                {activeRun.duration_ms != null && <span className="sb-run-dur">{activeRun.duration_ms} ms</span>}
                {activeRun.pid != null && <span className="sb-run-dur">pid {activeRun.pid}</span>}
              </div>
              <div className="sb-run-events">
                {activeRun.events.map((ev, i) => (
                  <span key={i} className="sb-run-event">{ev.event}</span>
                ))}
              </div>
              {activeRun.error && (
                <pre className="sb-output sb-output-err">{activeRun.error}</pre>
              )}
              {activeRun.output && (
                <pre className="sb-output">{activeRun.output}</pre>
              )}
            </div>
          )}

          {/* recent runs (real history) */}
          <RecentRuns botId={selected.id} onOpen={(r) => setActiveRun(r)} />
        </section>
      )}

      {/* discovery info */}
      {scan && (
        <section className="sb-scan" aria-label="Discovery configuration">
          <h3 className="sb-scan-title">Local bot discovery</h3>
          <p className="sb-scan-line">
            Root: <code>{scan.root ?? scan.default_root}</code>
            {scan.configured_root ? " (configured)" : " (default)"}
          </p>
          <p className="sb-scan-line">
            Approved scope: <code>{scan.approved_scope}</code> — paths outside it are rejected.
            {scan.discovered} action file(s) found; discovery never imports or executes code.
          </p>
          {scan.problems?.length > 0 && (
            <details className="sb-scan-problems">
              <summary>{scan.problems.length} file(s) skipped or unparsable</summary>
              <ul>
                {scan.problems.slice(0, 12).map((p, i) => (
                  <li key={i}><code>{p.file}</code> — {p.reason}</li>
                ))}
              </ul>
            </details>
          )}
        </section>
      )}
        </>
      )}
    </main>
  );
}

/* ── recent runs (real backend history) ───────────────────────────────────── */

function RecentRuns({ botId, onOpen }: { botId: string; onOpen: (r: RunRecord) => void }) {
  const [runs, setRuns] = useState<RunRecord[] | null>(null);
  useEffect(() => {
    let alive = true;
    saraFetch(`${SARA_API_URL}/api/sara-bots/runs?bot_id=${encodeURIComponent(botId)}&limit=8`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { runs: [] }))
      .then((j) => { if (alive) setRuns(j.runs ?? []); })
      .catch(() => { if (alive) setRuns([]); });
    return () => { alive = false; };
  }, [botId]);
  if (!runs) return null;
  if (runs.length === 0) return null;
  return (
    <div className="sb-history">
      <h4 className="sb-history-title">Recent runs</h4>
      <ul className="sb-history-list">
        {runs.map((r) => (
          <li key={r.run_id}>
            <button className="sb-history-row" onClick={() => onOpen(r)}>
              <span className="sb-run-status" style={{ color: RUN_STATE_META[r.status]?.color }}>
                <span className="sb-pill-dot" style={{ background: RUN_STATE_META[r.status]?.color }} />
                {RUN_STATE_META[r.status]?.label ?? r.status}
              </span>
              <span className="sb-history-preview">
                {r.output ? r.output.slice(0, 60) : r.error ? r.error.slice(0, 60) : r.params_preview.slice(0, 60)}
              </span>
              <span className="sb-history-when">{formatRunWhen(r)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
