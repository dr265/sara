"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { API_GAMEFORGE_URL } from "@/lib/api";

/**
 * GameForge — Autonomous AI Game Creation Agent (SARA Nexus Hub).
 * Backend: /api/gameforge/* (plan -> generate -> test -> repair -> package).
 * Every number shown here comes from the backend job snapshots — no fake progress.
 * UI notes: lives inside the hub layout's centered container, so it uses a top
 * tab bar (not a sidebar) and auto-recovers the status chip when the backend
 * comes back up (retry loop + manual refresh).
 */

type View = "create" | "games" | "builds" | "published" | "system";

type Stage =
  | "planning" | "generating" | "building" | "testing" | "repairing"
  | "packaging" | "ready" | "failed";

interface Game {
  id: string;
  title: string;
  genre?: string;
  engine?: string;
  runtime?: string;
  resolution?: string;
  quality?: string;
  createdAt?: string;
  buildStatus?: string;
  playable?: boolean;
  published?: boolean;
  previewUrl?: string | null;
  packagingError?: string | null;
  browserBuildOk?: boolean;
  prompt?: string;
}

interface JobStatus {
  jobId: string;
  gameId: string;
  stage: Stage | string;
  status: string;
  progress: number;
  message?: string;
  error?: string | null;
  log?: string[];
  game?: Game;
}

interface Health {
  ok?: boolean;
  llm?: { configured?: boolean; provider?: string | null; model?: string | null };
  pygame?: { installed?: boolean; version?: string };
  pygbag?: { installed?: boolean };
  maxRepairs?: number;
}

const FAILED_STAGES = new Set(["failed", "invalid", "rejected", "aborted", "cancelled"]);

const STAGES: Stage[] = ["planning", "generating", "building", "testing", "repairing", "packaging", "ready"];

const NAV: Array<[View, string]> = [
  ["create", "Create Game"],
  ["games", "My Games"],
  ["builds", "Builds"],
  ["published", "Published"],
  ["system", "System"],
];

const EXAMPLES = [
  "A quiet fishing game on a stormy moon",
  "A compact tactical arena with rewind mechanics",
  "A two-button platformer about restoring a lighthouse",
  "A polished 2D space survival game with enemy waves, power-ups, boss fights, score, health, levels and sound",
];

const CSS = `
.gf-root { color-scheme: dark; color: #e9eee9;
  background: radial-gradient(circle at 75% -10%, #3d5246 0, transparent 38%), linear-gradient(145deg, #10140f, #0b0e0c 62%, #121b16);
  border: 1px solid #ffffff10; border-radius: 20px;
  font-family: Manrope, system-ui, sans-serif; padding: 26px clamp(18px, 3vw, 40px) 34px; }
.gf-root * { box-sizing: border-box; }
.gf-root button, .gf-root input, .gf-root textarea, .gf-root select { font: inherit; }
.gf-root button { cursor: pointer; }
.gf-topbar { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; margin-bottom: 26px; }
.gf-brand { display: flex; align-items: center; gap: 10px; font-weight: 800; letter-spacing: -.04em; font-size: 18px; }
.gf-brand-mark { width: 30px; height: 30px; border-radius: 9px; background: #d9e6dc; color: #152018; display: grid; place-items: center; font-size: 14px; }
.gf-tabs { display: flex; gap: 4px; flex-wrap: wrap; }
.gf-tab { border: 1px solid transparent; color: #87928b; background: transparent; padding: 9px 14px; border-radius: 10px; font-size: 14px; }
.gf-tab:hover { color: #edf4ed; background: #ffffff0d; }
.gf-tab.active { color: #edf4ed; background: #ffffff10; border-color: #ffffff1c; }
.gf-chipbox { margin-left: auto; display: flex; align-items: center; gap: 10px; }
.gf-status { display: inline-flex; align-items: center; gap: 8px; color: #809087; font: 11px 'DM Mono', monospace;
  border: 1px solid #ffffff14; background: #ffffff06; border-radius: 999px; padding: 6px 12px; }
.gf-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #8fc49c; box-shadow: 0 0 14px #8fc49c; }
.gf-dot.gf-dot--err { background: #e08a7c; box-shadow: 0 0 14px #e08a7c; animation: gf-pulse 1.4s ease-in-out infinite; }
@keyframes gf-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
.gf-retry { border: 1px solid #ffffff1c; background: #ffffff08; color: #dce5dd; padding: 6px 12px; border-radius: 999px; font-size: 12px; }
.gf-retry:hover { background: #ffffff12; }
.gf-eyebrow { color: #8b9c91; text-transform: uppercase; letter-spacing: .14em; font: 11px 'DM Mono', monospace; }
.gf-h1 { font-size: clamp(26px, 3.4vw, 44px); line-height: 1.05; letter-spacing: -.065em; margin: 9px 0 12px; max-width: 720px; font-weight: 800; }
.gf-h2 { letter-spacing: -.04em; font-weight: 700; }
.gf-subtle { color: #99a59d; max-width: 680px; line-height: 1.7; font-size: 14px; }
.gf-workspace { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(280px, 1fr); gap: 20px; }
.gf-workspace--lower { grid-template-columns: minmax(0, 1.55fr) minmax(280px, 1fr); }
.gf-glass { border: 1px solid #ffffff16; background: linear-gradient(145deg, #ffffff0d, #ffffff04); box-shadow: 0 24px 80px #00000032, inset 0 1px #ffffff0c; backdrop-filter: blur(22px); border-radius: 18px; }
.gf-editor { padding: 22px; }
.gf-editor textarea { width: 100%; min-height: 200px; resize: vertical; color: #e9eee9; background: #0d110f99; border: 1px solid #ffffff14; border-radius: 12px; padding: 18px; outline: none; line-height: 1.65; }
.gf-editor textarea:focus { border-color: #9fc1a8; box-shadow: 0 0 0 3px #9fc1a822; }
.gf-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.gf-toolbar { justify-content: space-between; margin-top: 16px; }
.gf-primary { border: 0; background: #d8e6d9; color: #172018; padding: 12px 18px; border-radius: 9px; font-weight: 800; }
.gf-secondary { border: 1px solid #ffffff1c; background: #ffffff08; color: #dce5dd; padding: 10px 14px; border-radius: 9px; text-decoration: none; display: inline-flex; align-items: center; }
.gf-secondary:hover { background: #ffffff10; }
.gf-primary:disabled, .gf-secondary:disabled { opacity: .45; cursor: not-allowed; }
.gf-examples { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.gf-chip { border: 1px solid #ffffff12; color: #9daaa1; background: transparent; padding: 7px 10px; border-radius: 7px; font-size: 12px; text-align: left; }
.gf-chip:hover { color: #dce5dd; border-color: #ffffff2a; }
.gf-field { display: grid; gap: 8px; margin: 16px 0; color: #9aa79e; font-size: 12px; }
.gf-field select { background: #0e1210; color: #dfe8df; border: 1px solid #ffffff18; padding: 10px; border-radius: 8px; }
.gf-timeline { display: flex; gap: 6px; margin: 20px 0; overflow-x: auto; padding-bottom: 4px; }
.gf-step { min-width: 90px; color: #728078; font: 10px 'DM Mono', monospace; text-transform: uppercase; }
.gf-step::before { content: ""; display: block; height: 3px; background: #ffffff18; margin-bottom: 8px; border-radius: 4px; }
.gf-step.gf-step--done::before { background: #5d7a62; }
.gf-step.gf-step--active { color: #d7e5d9; }
.gf-step.gf-step--active::before { background: #a9c8ae; box-shadow: 0 0 15px #a9c8ae; }
.gf-step.gf-step--failed { color: #efb7ad; }
.gf-step.gf-step--failed::before { background: #b96456; }
.gf-progress { height: 6px; border-radius: 4px; background: #ffffff12; overflow: hidden; margin: 10px 0 6px; }
.gf-progress > div { height: 100%; background: linear-gradient(90deg, #8fc49c, #a9c8ae); transition: width .5s ease; }
.gf-preview { min-height: 420px; padding: 12px; display: flex; flex-direction: column; }
.gf-preview iframe { width: 100%; height: 400px; border: 0; border-radius: 12px; background: #080a09; flex: 1; }
.gf-preview-empty { height: 380px; display: grid; place-items: center; color: #718078; text-align: center; flex: 1; position: relative; overflow: hidden; }
.gf-scene-orbit { position: absolute; width: 210px; aspect-ratio: 1; border: 1px solid #a9c8ae35; border-radius: 50%; transform: perspective(320px) rotateX(66deg) rotateZ(-18deg); animation: gf-orbit 14s linear infinite; box-shadow: 0 0 70px #8fc49c18; }
.gf-scene-orbit::before, .gf-scene-orbit::after { content: ""; position: absolute; inset: 20px; border: 1px solid #a9c8ae22; border-radius: 50%; }
.gf-scene-orbit::after { inset: 48px; border-color: #d8e6d944; }
@keyframes gf-orbit { to { transform: perspective(320px) rotateX(66deg) rotateZ(342deg); } }
.gf-info { padding: 22px; }
.gf-info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 12px; }
.gf-info-item small { display: block; color: #718078; font: 10px 'DM Mono', monospace; text-transform: uppercase; margin-bottom: 5px; }
.gf-info-item span { color: #d7e2d8; font-size: 13px; overflow-wrap: anywhere; }
.gf-error { margin-top: 15px; padding: 12px 14px; color: #efb7ad; border: 1px solid #b9645645; background: #6e30251c; border-radius: 9px; font-size: 13px; white-space: pre-wrap; overflow-wrap: anywhere; }
.gf-note { margin-top: 15px; padding: 12px 14px; color: #e8d9a8; border: 1px solid #8a7c4555; background: #3a341d22; border-radius: 9px; font-size: 13px; white-space: pre-wrap; }
.gf-list { display: grid; gap: 10px; margin-top: 22px; }
.gf-card { border: 1px solid #ffffff14; background: #ffffff06; border-radius: 14px; padding: 16px 18px; display: flex; align-items: center; gap: 14px; cursor: pointer; }
.gf-card:hover { border-color: #ffffff2a; }
.gf-card--static { cursor: default; }
.gf-badge { padding: 3px 9px; border-radius: 999px; font-size: 11px; border: 1px solid; white-space: nowrap; }
.gf-badge--ok { color: #a9d8b4; border-color: #3f6a4a55; background: #1d3a2633; }
.gf-badge--fail { color: #efb7ad; border-color: #b9645645; background: #6e30251c; }
.gf-kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-top: 22px; }
.gf-back { margin-top: 26px; }
@media (max-width: 980px) { .gf-workspace, .gf-workspace--lower { grid-template-columns: 1fr; } .gf-chipbox { margin-left: 0; width: 100%; } }
`;

const API_BASE = `${API_GAMEFORGE_URL}/api/gameforge`;

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* keep default */ }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
};

export default function GameForgePage() {
  const [view, setView] = useState<View>("create");
  const [prompt, setPrompt] = useState("");
  const [resolution, setResolution] = useState("1280x720");
  const [quality, setQuality] = useState("Polished");
  const [game, setGame] = useState<Game | null>(null);
  const [games, setGames] = useState<Game[]>([]);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [jobs, setJobs] = useState<JobStatus[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const jobAbortRef = useRef<AbortController | null>(null);

  const loadHealth = useCallback(async () => {
    setChecking(true);
    try {
      setHealth(await api<Health>("/health"));
    } catch {
      setHealth(null);
    } finally {
      setChecking(false);
    }
  }, []);

  const loadGames = useCallback(async () => {
    try {
      setGames(await api<Game[]>("/games"));
    } catch {
      /* library stays as-is; the status chip surfaces connectivity */
    }
  }, []);

  useEffect(() => {
    void loadHealth();
    void loadGames();
    return () => { jobAbortRef.current?.abort(); };
  }, [loadHealth, loadGames]);

  // Self-healing status: while the backend is down, re-check every 8s so the
  // chip flips back to "online" on its own (no manual page refresh needed).
  useEffect(() => {
    if (health != null) return;
    const id = window.setInterval(() => { void loadHealth(); }, 8000);
    return () => window.clearInterval(id);
  }, [health, loadHealth]);

  const trackJob = useCallback(async (jobId: string, signal: AbortSignal) => {
    for (;;) {
      if (signal.aborted) return;
      const next = await api<JobStatus>(`/jobs/${encodeURIComponent(jobId)}`, { signal });
      setJob(next);
      if (next.status === "done" || next.status === "failed") {
        await loadGames();
        if (next.status === "done") {
          try {
            const all = await api<Game[]>("/games");
            const entry = all.find((g) => g.id === next.gameId);
            if (entry) setGame(entry);
          } catch { /* keep whatever we have */ }
        }
        break;
      }
      await new Promise<void>((resolve, reject) => {
        const timeout = window.setTimeout(resolve, 1600);
        signal.addEventListener("abort", () => {
          window.clearTimeout(timeout);
          reject(new DOMException("Job polling aborted", "AbortError"));
        }, { once: true });
      });
    }
  }, [loadGames]);

  const generate = useCallback(async () => {
    if (!prompt.trim() || busy) return;
    setBusy(true);
    setError("");
    setGame(null);
    jobAbortRef.current?.abort();
    const controller = new AbortController();
    jobAbortRef.current = controller;
    try {
      const created = await api<JobStatus>("/games", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({ prompt: prompt.trim(), resolution, quality }),
      });
      setJob(created);
      if (created.game) setGame(created.game);
      await trackJob(created.jobId, controller.signal);
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setError(e instanceof Error ? e.message : "Generation failed");
      }
    } finally {
      if (jobAbortRef.current === controller) jobAbortRef.current = null;
      setBusy(false);
    }
  }, [prompt, busy, resolution, quality, trackJob]);

  const restart = useCallback(async () => {
    if (!game) return;
    setBusy(true);
    setError("");
    jobAbortRef.current?.abort();
    const controller = new AbortController();
    jobAbortRef.current = controller;
    try {
      const created = await api<JobStatus>(`/games/${encodeURIComponent(game.id)}/restart`, { method: "POST", signal: controller.signal });
      setJob(created);
      await trackJob(created.jobId, controller.signal);
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setError(e instanceof Error ? e.message : "Restart failed");
      }
    } finally {
      if (jobAbortRef.current === controller) jobAbortRef.current = null;
      setBusy(false);
    }
  }, [game, trackJob]);

  // Archive finished jobs into the Builds trail.
  useEffect(() => {
    if (job && (job.status === "done" || job.status === "failed")) {
      setJobs((prev) => [job, ...prev.filter((j) => j.jobId !== job.jobId)].slice(0, 25));
    }
  }, [job]);

  // Fresh data whenever the user enters a library-ish view.
  useEffect(() => {
    if (view === "games" || view === "published") void loadGames();
  }, [view, loadGames]);

  const currentStage = job?.stage ?? (game?.buildStatus === "ready" ? "ready" : null);
  const stageIndex = currentStage ? STAGES.indexOf(currentStage as Stage) : -1;
  const failed = currentStage ? FAILED_STAGES.has(currentStage) : false;
  const running = job?.status === "running" || job?.status === "queued";
  const previewUrl = game?.previewUrl ? `${API_GAMEFORGE_URL}${game.previewUrl}` : null;
  const downloadUrl = game ? `${API_GAMEFORGE_URL}/api/gameforge/games/${encodeURIComponent(game.id)}/download` : null;
  const backendUp = health != null;

  const statusChip = (
    <div className="gf-chipbox">
      <span className="gf-status">
        <span className={`gf-dot${backendUp ? "" : " gf-dot--err"}`} />
        {checking && !backendUp
          ? "Checking backend…"
          : backendUp
            ? `Backend online${health?.llm?.model ? ` · ${health.llm.model}` : ""}`
            : "Backend offline — retrying"}
      </span>
      <button className="gf-retry" onClick={() => { void loadHealth(); void loadGames(); }}>
        Refresh
      </button>
    </div>
  );

  return (
    <div className="gf-root">
      <style>{CSS}</style>

      <div className="gf-topbar">
        <div className="gf-brand"><span className="gf-brand-mark">◈</span> GameForge</div>
        <nav className="gf-tabs" aria-label="GameForge navigation">
          {NAV.map(([id, label]) => (
            <button key={id} className={`gf-tab${view === id ? " active" : ""}`} onClick={() => setView(id)}>
              {label}
            </button>
          ))}
        </nav>
        {statusChip}
      </div>

      {view === "create" && (
        <>
          <div className="gf-eyebrow">Autonomous game creation workspace</div>
          <h1 className="gf-h1">Build a Game From an Idea.</h1>
          <p className="gf-subtle">
            Describe the experience you want. GameForge plans the design, writes the full pygame-ce
            game, runs it headlessly, repairs real failures, and packages a playable browser build.
          </p>

          <div className="gf-timeline">
            {STAGES.map((stage, i) => (
              <div
                key={stage}
                className={[
                  "gf-step",
                  failed && stage === "failed" ? "gf-step--failed" : "",
                  !failed && stageIndex > i ? "gf-step--done" : "",
                  !failed && currentStage === stage ? "gf-step--active" : "",
                ].join(" ")}
              >
                {stage}
              </div>
            ))}
          </div>

          {running && job && (
            <div style={{ maxWidth: 640, marginBottom: 18 }}>
              <div className="gf-progress"><div style={{ width: `${job.progress}%` }} /></div>
              <div className="gf-eyebrow">
                Stage: {job.stage} · {job.message ?? ""} · {job.progress}%
              </div>
            </div>
          )}

          <section className="gf-workspace">
            <div className="gf-glass gf-editor">
              <div className="gf-eyebrow">Creative brief</div>
              <textarea
                aria-label="Game idea"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="Describe the game: its player fantasy, controls, world, progression, and visual tone."
              />
              <div className="gf-examples">
                {EXAMPLES.map((example) => (
                  <button className="gf-chip" key={example} onClick={() => setPrompt(example)}>
                    {example.length > 58 ? `${example.slice(0, 58)}…` : example}
                  </button>
                ))}
              </div>
              <div className="gf-row gf-toolbar">
                <span className="gf-eyebrow">
                  {job?.message && job.status !== "done" ? job.message : busy ? "Agent working…" : "Ready for a new brief"}
                </span>
                <button className="gf-primary" disabled={!prompt.trim() || busy || !backendUp} onClick={() => void generate()}>
                  {busy ? "Working…" : "Generate game"}
                </button>
              </div>
              {error && <div className="gf-error" role="alert">{error}</div>}
              {failed && job?.error && (
                <div className="gf-error" role="alert">
                  <strong>Validation failed after all repair attempts:</strong>
                  {"\n"}
                  {job.error}
                </div>
              )}
              {game?.packagingError && (
                <div className="gf-note">
                  Browser build note: the desktop zip is ready; the pygbag bundle failed with:
                  {"\n"}
                  {game.packagingError}
                </div>
              )}
            </div>

            <div className="gf-glass gf-info">
              <div className="gf-eyebrow">Generation settings</div>
              <label className="gf-field">
                Target resolution
                <select value={resolution} onChange={(e) => setResolution(e.target.value)}>
                  <option>1280x720</option>
                  <option>1920x1080</option>
                  <option>1024x768</option>
                  <option>960x540</option>
                  <option>800x600</option>
                </select>
              </label>
              <label className="gf-field">
                Quality bar
                <select value={quality} onChange={(e) => setQuality(e.target.value)}>
                  <option>Balanced</option>
                  <option>Polished</option>
                  <option>Maximum</option>
                </select>
              </label>
              <p className="gf-subtle">
                The backend reports each real stage as it completes — planning, code generation,
                headless testing, auto-repairs, packaging. No progress is estimated locally.
              </p>
            </div>
          </section>

          <section className="gf-workspace gf-workspace--lower gf-back">
            <div className="gf-glass gf-preview">
              {previewUrl ? (
                <iframe title="Generated game preview" src={previewUrl} allow="autoplay; fullscreen; gamepad" />
              ) : (
                <div className="gf-preview-empty">
                  <div className="gf-scene-orbit" aria-hidden="true" />
                  <div>
                    <div className="gf-eyebrow">Live preview</div>
                    <p>Your playable browser build will appear here when the backend finishes.</p>
                  </div>
                </div>
              )}
              {previewUrl && (
                <div className="gf-row" style={{ marginTop: 10 }}>
                  <button className="gf-secondary" onClick={() => void restart()} disabled={busy}>
                    Restart build
                  </button>
                  <button className="gf-secondary" onClick={() => window.open(previewUrl, "_blank", "noopener,noreferrer")}>
                    Open in new tab
                  </button>
                  {downloadUrl && (
                    <a className="gf-secondary" href={downloadUrl}>
                      Download .zip
                    </a>
                  )}
                </div>
              )}
            </div>

            <div className="gf-glass gf-info">
              <div className="gf-eyebrow">Game information</div>
              <h2 className="gf-h2" style={{ marginTop: 8 }}>{game?.title ?? "Untitled project"}</h2>
              <div className="gf-info-grid">
                {([
                  ["Genre", game?.genre],
                  ["Engine", game?.engine],
                  ["Resolution", game?.resolution],
                  ["Quality", game?.quality],
                  ["Build", game?.buildStatus],
                  ["Playable", game?.playable == null ? undefined : game.playable ? "Yes" : "No"],
                  ["Created", game?.createdAt ? new Date(game.createdAt).toLocaleString() : undefined],
                  ["Runtime", game?.runtime],
                ] as Array<[string, string | undefined]>).map(([label, value]) => (
                  <div className="gf-info-item" key={label}>
                    <small>{label}</small>
                    <span>{value ?? "—"}</span>
                  </div>
                ))}
              </div>
            </div>
          </section>
        </>
      )}

      {view === "games" && (
        <>
          <div className="gf-eyebrow">Library</div>
          <h1 className="gf-h1">Your games.</h1>
          <p className="gf-subtle">{games.length} games returned by the backend. Click one to load it into the workspace.</p>
          <div className="gf-list">
            {games.length === 0 && <p className="gf-subtle">No generated games yet — create one from the brief editor.</p>}
            {games.map((g) => (
              <div
                className="gf-card"
                key={g.id}
                onClick={() => { setGame(g); setView("create"); }}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => { if (e.key === "Enter") { setGame(g); setView("create"); } }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <strong style={{ display: "block", overflowWrap: "anywhere" }}>{g.title}</strong>
                  <span className="gf-eyebrow">
                    {g.id} · {g.genre ?? "—"} · {g.resolution ?? "—"} · {g.createdAt?.slice(0, 10) ?? ""}
                  </span>
                </div>
                <span className={`gf-badge ${g.buildStatus === "ready" ? "gf-badge--ok" : "gf-badge--fail"}`}>
                  {g.buildStatus ?? "unknown"}
                </span>
              </div>
            ))}
          </div>
        </>
      )}

      {view === "builds" && (
        <>
          <div className="gf-eyebrow">Build trail</div>
          <h1 className="gf-h1">Builds.</h1>
          <p className="gf-subtle">Real job snapshots from this session — stage, progress, and any validation error reported by the backend.</p>
          <div className="gf-list">
            {jobs.length === 0 && <p className="gf-subtle">No builds run in this session yet.</p>}
            {jobs.map((j) => (
              <div className="gf-card gf-card--static" key={j.jobId}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <strong style={{ display: "block" }}>{j.gameId}</strong>
                  <span className="gf-eyebrow">{j.stage} · {j.message ?? ""}</span>
                  {(j.log ?? []).length > 0 && (
                    <div className="gf-note" style={{ marginTop: 10, maxHeight: 140, overflow: "auto" }}>
                      {(j.log ?? []).join("\n")}
                    </div>
                  )}
                  {j.error && <div className="gf-error" style={{ marginTop: 10 }}>{j.error.slice(0, 600)}</div>}
                </div>
                <span className={`gf-badge ${j.status === "done" ? "gf-badge--ok" : "gf-badge--fail"}`}>{j.status}</span>
              </div>
            ))}
          </div>
        </>
      )}

      {view === "published" && (
        <>
          <div className="gf-eyebrow">Distribution</div>
          <h1 className="gf-h1">Published games.</h1>
          <p className="gf-subtle">
            Games flagged as published by the backend. Publishing targets are configured on the backend —
            nothing here is simulated.
          </p>
          <div className="gf-list">
            {games.filter((g) => g.published).length === 0 && (
              <p className="gf-subtle">No published games yet. Every generated build is kept in My Games with its full source and zip.</p>
            )}
            {games.filter((g) => g.published).map((g) => (
              <div className="gf-card" key={g.id} onClick={() => { setGame(g); setView("create"); }} role="button" tabIndex={0}>
                <strong style={{ flex: 1 }}>{g.title}</strong>
                <span className="gf-badge gf-badge--ok">published</span>
              </div>
            ))}
          </div>
        </>
      )}

      {view === "system" && (
        <>
          <div className="gf-eyebrow">System</div>
          <h1 className="gf-h1">Agent engine.</h1>
          {health ? (
            <div className="gf-kv">
              <div className="gf-glass gf-info">
                <div className="gf-eyebrow">LLM brain</div>
                <div className="gf-info-grid">
                  <div className="gf-info-item">
                    <small>Provider</small>
                    <span>{health.llm?.provider ?? "—"}</span>
                  </div>
                  <div className="gf-info-item">
                    <small>Model</small>
                    <span>{health.llm?.model ?? "—"}</span>
                  </div>
                </div>
              </div>
              <div className="gf-glass gf-info">
                <div className="gf-eyebrow">Runtime</div>
                <div className="gf-info-grid">
                  <div className="gf-info-item">
                    <small>pygame</small>
                    <span>{health.pygame?.installed ? `installed ${health.pygame.version ?? ""}` : "missing"}</span>
                  </div>
                  <div className="gf-info-item">
                    <small>pygbag (browser builds)</small>
                    <span>{health.pygbag?.installed ? "installed" : "missing — desktop zips only"}</span>
                  </div>
                  <div className="gf-info-item">
                    <small>Auto-repairs</small>
                    <span>up to {health.maxRepairs ?? "—"}</span>
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <p className="gf-subtle">
              Backend unreachable — the chip above retries every 8 seconds automatically. Start the SARA
              backend (start_all.bat) and this page will recover on its own.
            </p>
          )}
        </>
      )}
    </div>
  );
}
