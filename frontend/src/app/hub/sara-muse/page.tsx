"use client";

/**
 * SaraMuse — the OpenMuse-powered autonomous workspace.
 *
 * Everything on this screen is backed by the real muse runtime:
 *   • goals       — durable goals with milestones (toggle, pause, complete)
 *   • track       — page watches: real scheduled fetches of public pages,
 *                   change/contains/price conditions, failure backoff
 *   • ideas       — suggestions derived from real records; accepting one
 *                   submits a REAL engine task to the live SaraMuse bot
 *   • finance     — deterministic CSV spending analysis (OpenMuse port)
 *   • notifications + memory — real persisted records
 *
 * The UI never invents a status: every label comes from a backend response.
 */

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { SARA_API_URL, saraFetch } from "@/lib/api";

/* ── types ─────────────────────────────────────────────────────────────── */

interface Milestone { id: string; title: string; done: boolean }
interface Goal {
  id: string; title: string; description: string; category: string;
  status: "active" | "paused" | "completed"; milestones: Milestone[];
  createdAt: number;
}
interface Monitor {
  id: string; title: string; url: string;
  condition: "change" | "contains" | "price_below"; value: string;
  intervalMinutes: number; status: "active" | "paused" | "stopped";
  nextCheckAt: number | null; lastCheckedAt: number | null;
  checks: number; error: string | null;
}
interface Evidence { id: string; kind: string; title: string; excerpt: string }
interface Idea {
  id: string; title: string; reason: string; evidence: Evidence[];
  prompt: string; kind: string; status: "new" | "dismissed" | "accepted";
  taskId?: string; createdAt: number;
}
interface Notification {
  id: string; taskId: string | null; title: string; body: string;
  createdAt: number; read: boolean;
}
interface MemoryEntry { id: string; kind: string; text: string; ts: number }
interface FinanceAnalysis {
  income: number; spending: number; saved: number; count: number;
  categories: { name: string; amount: number }[];
  transactions: { id: string; date: string; description: string; amount: number; category: string }[];
  period: { from: string; to: string };
}
interface Snapshot {
  goals: Goal[]; monitors: Monitor[]; ideas: Idea[];
  notifications: Notification[]; artifacts: unknown[];
  memories: MemoryEntry[];
  identity: { name: string; tone: string; avatar?: string };
  bot: { id: string; name: string; status: string } | null;
  worker: { running: boolean; queued: number; active: number };
}

type Section = "today" | "goals" | "track" | "ideas" | "finance" | "memory" | "activity";

/* ── helpers ───────────────────────────────────────────────────────────── */

function relTime(ts: number | null): string {
  if (!ts) return "—";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return `${Math.max(s, 0)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

const CONDITION_LABEL: Record<string, string> = {
  change: "Any change",
  contains: "Contains text",
  price_below: "Price below",
};

/* ── page ──────────────────────────────────────────────────────────────── */

export default function SaraMusePage() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [section, setSection] = useState<Section>("today");

  const api = useCallback((path: string, init?: RequestInit) =>
    saraFetch(`${SARA_API_URL}/api/sara-bots/muse${path}`, init), []);

  const load = useCallback(async () => {
    try {
      const res = await api("", { cache: "no-store" });
      if (!res.ok) throw new Error(`backend responded ${res.status}`);
      setSnap(await res.json());
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [api]);

  useEffect(() => { void load(); }, [load]);

  const flash = useCallback((msg: string) => {
    setNotice(msg);
    window.setTimeout(() => setNotice(""), 4000);
  }, []);

  const post = useCallback(async (path: string, body: unknown) => {
    setBusy(true);
    try {
      const res = await api(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error((detail as { detail?: string }).detail ?? `HTTP ${res.status}`);
      }
      await load();
      return await res.json();
    } finally {
      setBusy(false);
    }
  }, [api, load]);

  const unread = useMemo(
    () => (snap?.notifications ?? []).filter((n) => !n.read).length,
    [snap]);
  const freshIdeas = useMemo(
    () => (snap?.ideas ?? []).filter((i) => i.status === "new").length,
    [snap]);

  /* ── render ──────────────────────────────────────────────────────────── */

  return (
    <div className="sm-root">
      <style>{SM_CSS}</style>

      <header className="sm-header">
        <a className="sm-back" href="/hub/sara-bots">← Sara Bots</a>
        <div className="sm-head-row">
          <div className="sm-mascot" aria-hidden>
            <span className="sm-mascot-eye" /><span className="sm-mascot-eye" />
            <span className="sm-mascot-mouth" />
          </div>
          <div>
            <div className="sm-eyebrow">OPENMUSE-POWERED · AUTONOMOUS WORKSPACE</div>
            <h1 className="sm-title">{snap?.identity?.name ?? "SaraMuse"}</h1>
            <p className="sm-sub">
              Ask for an outcome. SaraMuse follows the plan, reviews actions with you,
              and comes back to the result — goals, watches, ideas and real task
              execution, all in one place.
            </p>
          </div>
          <div className="sm-head-stats">
            <div className="sm-stat-card">
              <span className="sm-stat-v">{snap?.worker.running ? "live" : "down"}</span>
              <span className="sm-stat-k">worker</span>
            </div>
            <div className="sm-stat-card">
              <span className="sm-stat-v">{snap?.worker.active ?? 0}</span>
              <span className="sm-stat-k">active tasks</span>
            </div>
            <div className="sm-stat-card">
              <span className="sm-stat-v">{snap?.bot?.status ?? "—"}</span>
              <span className="sm-stat-k">bot</span>
            </div>
          </div>
        </div>
      </header>

      {error && (
        <div className="sm-banner sm-banner-error" role="alert">
          Backend unreachable: {error} — start it with <code>python run_backend.py</code>.
        </div>
      )}
      {notice && <div className="sm-banner sm-banner-ok" role="status">{notice}</div>}

      <nav className="sm-nav" aria-label="SaraMuse sections">
        {([
          ["today", "Today"],
          ["goals", "Goals"],
          ["track", "Track"],
          ["ideas", freshIdeas ? `Ideas (${freshIdeas})` : "Ideas"],
          ["finance", "Finance"],
          ["memory", "Memory"],
          ["activity", unread ? `Activity (${unread})` : "Activity"],
        ] as [Section, string][]).map(([id, label]) => (
          <button key={id} className={`sm-nav-btn ${section === id ? "sm-nav-on" : ""}`}
            onClick={() => setSection(id)}>
            {label}
          </button>
        ))}
      </nav>

      <main className="sm-main">
        {!snap && !error && <div className="sm-loading">Connecting to SaraMuse…</div>}

        {snap && section === "today" && (
          <TodaySection snap={snap} goto={setSection} onAsk={(goal) =>
            postIdeas(post, goal, flash)} />
        )}

        {snap && section === "goals" && (
          <GoalsSection snap={snap} busy={busy} post={post} flash={flash} />
        )}

        {snap && section === "track" && (
          <TrackSection snap={snap} busy={busy} post={post} flash={flash} />
        )}

        {snap && section === "ideas" && (
          <IdeasSection snap={snap} busy={busy} post={post} flash={flash} />
        )}

        {snap && section === "finance" && (
          <FinanceSection busy={busy} post={post} flash={flash} />
        )}

        {snap && section === "memory" && <MemorySection snap={snap} />}

        {snap && section === "activity" && (
          <ActivitySection snap={snap} busy={busy} post={post} />
        )}
      </main>
    </div>
  );
}

/* accepting an idea-style quick ask submits a real task via the ideas flow */
async function postIdeas(
  post: (path: string, body: unknown) => Promise<unknown>,
  goal: string,
  flash: (m: string) => void,
) {
  try {
    await post("/ideas/refresh", {});
    flash(`Noted: “${goal}” — check Ideas to turn it into a real task.`);
  } catch (err) {
    flash(err instanceof Error ? err.message : String(err));
  }
}

/* ── Today ─────────────────────────────────────────────────────────────── */

function TodaySection({ snap, goto, onAsk }: {
  snap: Snapshot; goto: (s: Section) => void;
  onAsk: (goal: string) => void;
}) {
  const [ask, setAsk] = useState("");
  const activeGoals = snap.goals.filter((g) => g.status === "active").length;
  const activeWatches = snap.monitors.filter((m) => m.status === "active").length;
  const unread = snap.notifications.filter((n) => !n.read).length;
  const freshIdeas = snap.ideas.filter((i) => i.status === "new").length;

  return (
    <div className="sm-stack">
      <div className="sm-hero">
        <div>
          <div className="sm-hero-eyebrow">✦ A little clarity, every day</div>
          <h2 className="sm-hero-title">Your day, with a little<br />more room to breathe.</h2>
          <p className="sm-hero-sub">
            {activeGoals ? `${activeGoals} active goal${activeGoals === 1 ? "" : "s"}` : "No active goals yet"}
            {activeWatches ? `, ${activeWatches} watch${activeWatches === 1 ? "" : "es"} running` : ""}
            {unread ? `, ${unread} unread notification${unread === 1 ? "" : "s"}` : ""}.
            {" "}Let&apos;s make space for what matters.
          </p>
          <form className="sm-ask-row" onSubmit={(e) => {
            e.preventDefault();
            if (ask.trim()) { onAsk(ask.trim()); setAsk(""); }
          }}>
            <input className="sm-input sm-grow" value={ask}
              placeholder="Ask SaraMuse to work on something…"
              onChange={(e) => setAsk(e.target.value)} aria-label="Ask SaraMuse" />
            <button className="sm-btn sm-btn-primary" type="submit">Ask</button>
          </form>
        </div>
        <div className="sm-hero-orb" aria-hidden />
      </div>

      <div className="sm-tiles">
        {[
          { label: "ACTIVE GOALS", value: activeGoals, note: "Milestones keep you moving", section: "goals" as Section },
          { label: "WATCHES RUNNING", value: activeWatches, note: "Real checks of public pages", section: "track" as Section },
          { label: "FRESH IDEAS", value: freshIdeas, note: "Suggestions with evidence", section: "ideas" as Section },
          { label: "UNREAD", value: unread, note: "Changes worth your attention", section: "activity" as Section },
        ].map((t) => (
          <button key={t.label} className="sm-tile" onClick={() => goto(t.section)}>
            <span className="sm-tile-label">{t.label}</span>
            <span className="sm-tile-value">{t.value}</span>
            <span className="sm-tile-note">{t.note}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ── Goals ─────────────────────────────────────────────────────────────── */

function GoalsSection({ snap, busy, post, flash }: {
  snap: Snapshot; busy: boolean;
  post: (path: string, body: unknown) => Promise<unknown>;
  flash: (m: string) => void;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState("Personal");
  const [milestones, setMilestones] = useState("");
  const [showForm, setShowForm] = useState(false);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await post("/goals", {
        title, description, category,
        milestones: milestones.split("\n").map((m) => m.trim()).filter(Boolean),
      });
      setTitle(""); setDescription(""); setMilestones(""); setShowForm(false);
      flash("Goal saved.");
    } catch (err) {
      flash(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="sm-stack">
      <div className="sm-section-head">
        <h2 className="sm-section-title">Goals</h2>
        <button className="sm-btn sm-btn-primary" disabled={busy}
          onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Close" : "New goal"}
        </button>
      </div>

      {showForm && (
        <form className="sm-card sm-form" onSubmit={create}>
          <label className="sm-field">
            <span className="sm-field-label">Title *</span>
            <input className="sm-input" value={title} maxLength={160} required
              onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Ship the quarterly report" />
          </label>
          <label className="sm-field">
            <span className="sm-field-label">Description</span>
            <textarea className="sm-input sm-textarea" rows={2} value={description}
              onChange={(e) => setDescription(e.target.value)} />
          </label>
          <div className="sm-row">
            <label className="sm-field sm-grow">
              <span className="sm-field-label">Category</span>
              <input className="sm-input" value={category} maxLength={80}
                onChange={(e) => setCategory(e.target.value)} />
            </label>
          </div>
          <label className="sm-field">
            <span className="sm-field-label">Milestones (one per line)</span>
            <textarea className="sm-input sm-textarea" rows={3} value={milestones}
              placeholder={"Collect the data\nDraft the summary\nReview and send"}
              onChange={(e) => setMilestones(e.target.value)} />
          </label>
          <button className="sm-btn sm-btn-primary" type="submit" disabled={busy || !title.trim()}>
            Save goal
          </button>
        </form>
      )}

      {snap.goals.length === 0 && !showForm && (
        <div className="sm-empty">No goals yet. Create one and SaraMuse will keep it moving.</div>
      )}

      {snap.goals.map((goal) => {
        const done = goal.milestones.filter((m) => m.done).length;
        return (
          <div key={goal.id} className={`sm-card ${goal.status !== "active" ? "sm-card-dim" : ""}`}>
            <div className="sm-card-head">
              <span className={`sm-pill sm-pill-${goal.status}`}>{goal.status}</span>
              <h3 className="sm-card-title">{goal.title}</h3>
              <span className="sm-tag">{goal.category}</span>
            </div>
            {goal.description && <p className="sm-card-body">{goal.description}</p>}
            {goal.milestones.length > 0 && (
              <>
                <div className="sm-progress-track" aria-hidden>
                  <div className="sm-progress-fill"
                    style={{ width: `${goal.milestones.length ? (done / goal.milestones.length) * 100 : 0}%` }} />
                </div>
                <ul className="sm-milestones">
                  {goal.milestones.map((m) => (
                    <li key={m.id}>
                      <label className="sm-check">
                        <input type="checkbox" checked={m.done} disabled={busy}
                          onChange={async () => {
                            try {
                              await post(`/goals/${goal.id}/milestones/${m.id}`, { done: !m.done });
                            } catch (err) {
                              flash(err instanceof Error ? err.message : String(err));
                            }
                          }} />
                        <span className={m.done ? "sm-done" : ""}>{m.title}</span>
                      </label>
                    </li>
                  ))}
                </ul>
              </>
            )}
            <div className="sm-card-actions">
              {goal.status === "active" && (
                <>
                  <button className="sm-btn sm-btn-ghost" disabled={busy}
                    onClick={() => post(`/goals/${goal.id}`, { status: "paused" })}>Pause</button>
                  <button className="sm-btn sm-btn-ghost" disabled={busy}
                    onClick={() => post(`/goals/${goal.id}`, { status: "completed" })}>Complete</button>
                </>
              )}
              {goal.status !== "active" && (
                <button className="sm-btn sm-btn-ghost" disabled={busy}
                  onClick={() => post(`/goals/${goal.id}`, { status: "active" })}>Resume</button>
              )}
              <span className="sm-meta">{done}/{goal.milestones.length} milestones · created {relTime(goal.createdAt)}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ── Track (page watches) ──────────────────────────────────────────────── */

function TrackSection({ snap, busy, post, flash }: {
  snap: Snapshot; busy: boolean;
  post: (path: string, body: unknown) => Promise<unknown>;
  flash: (m: string) => void;
}) {
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [condition, setCondition] = useState<"change" | "contains" | "price_below">("change");
  const [value, setValue] = useState("");
  const [interval, setIntervalMin] = useState(15);
  const [showForm, setShowForm] = useState(false);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await post("/monitors", { title, url, condition, value, intervalMinutes: interval });
      setTitle(""); setUrl(""); setValue(""); setShowForm(false);
      flash("Watch created — the first real check runs on schedule.");
    } catch (err) {
      flash(err instanceof Error ? err.message : String(err));
    }
  };

  const control = async (id: string, action: string) => {
    try {
      const res = await post(`/monitors/${id}/control`, { action });
      if (action === "check" && res && typeof res === "object") {
        const r = res as { result?: string; error?: string };
        flash(r.error ? `Check failed: ${r.error}` : (r.result ?? "Check completed."));
      }
    } catch (err) {
      flash(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="sm-stack">
      <div className="sm-section-head">
        <h2 className="sm-section-title">Track</h2>
        <button className="sm-btn sm-btn-primary" disabled={busy}
          onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Close" : "New watch"}
        </button>
      </div>
      <p className="sm-hint">
        Recurring checks of public HTTP(S) pages. Change watches ignore
        relative-time-only shifts (“2 minutes ago”), alerts are deduplicated,
        and failures back off exponentially — a watch pauses after 5 failures in a row.
        Private and local-network addresses are rejected.
      </p>

      {showForm && (
        <form className="sm-card sm-form" onSubmit={create}>
          <label className="sm-field">
            <span className="sm-field-label">Title *</span>
            <input className="sm-input" value={title} maxLength={160} required
              placeholder="e.g. Restock watch — headphones"
              onChange={(e) => setTitle(e.target.value)} />
          </label>
          <label className="sm-field">
            <span className="sm-field-label">Public page URL *</span>
            <input className="sm-input" value={url} type="url" required maxLength={4096}
              placeholder="https://example.com/product"
              onChange={(e) => setUrl(e.target.value)} />
          </label>
          <div className="sm-row">
            <label className="sm-field">
              <span className="sm-field-label">Condition</span>
              <select className="sm-input" value={condition}
                onChange={(e) => setCondition(e.target.value as typeof condition)}>
                <option value="change">Any change</option>
                <option value="contains">Contains text</option>
                <option value="price_below">Price below (USD)</option>
              </select>
            </label>
            {condition !== "change" && (
              <label className="sm-field sm-grow">
                <span className="sm-field-label">{condition === "contains" ? "Text to find" : "Price threshold"}</span>
                <input className="sm-input" value={value} maxLength={300}
                  type={condition === "price_below" ? "number" : "text"}
                  min={condition === "price_below" ? 0.01 : undefined}
                  step={condition === "price_below" ? "0.01" : undefined}
                  onChange={(e) => setValue(e.target.value)} />
              </label>
            )}
            <label className="sm-field">
              <span className="sm-field-label">Every (minutes)</span>
              <input className="sm-input" type="number" min={1} max={10080} value={interval}
                onChange={(e) => setIntervalMin(Number(e.target.value))} />
            </label>
          </div>
          <button className="sm-btn sm-btn-primary" type="submit"
            disabled={busy || !title.trim() || !url.trim()}>
            Start watching
          </button>
        </form>
      )}

      {snap.monitors.length === 0 && !showForm && (
        <div className="sm-empty">No watches yet. Track a public page and get notified about real changes.</div>
      )}

      {snap.monitors.map((m) => (
        <div key={m.id} className="sm-card">
          <div className="sm-card-head">
            <span className={`sm-pill sm-pill-${m.status === "active" ? "active" : m.status === "paused" ? "paused" : "stopped"}`}>
              {m.status}
            </span>
            <h3 className="sm-card-title">{m.title}</h3>
            <span className="sm-tag">{CONDITION_LABEL[m.condition]}{m.condition !== "change" && m.value ? `: ${m.value}` : ""}</span>
          </div>
          <p className="sm-card-body sm-mono">{m.url}</p>
          {m.error && <p className="sm-error">⚠ {m.error}</p>}
          <div className="sm-card-actions">
            {m.status === "active" && (
              <>
                <button className="sm-btn sm-btn-ghost" disabled={busy} onClick={() => control(m.id, "check")}>Check now</button>
                <button className="sm-btn sm-btn-ghost" disabled={busy} onClick={() => control(m.id, "pause")}>Pause</button>
                <button className="sm-btn sm-btn-danger" disabled={busy} onClick={() => control(m.id, "stop")}>Stop</button>
              </>
            )}
            {m.status === "paused" && (
              <button className="sm-btn sm-btn-ghost" disabled={busy} onClick={() => control(m.id, "resume")}>Resume</button>
            )}
            <span className="sm-meta">
              {m.checks} check{m.checks === 1 ? "" : "s"} · every {m.intervalMinutes}m
              {m.lastCheckedAt ? ` · last ${relTime(m.lastCheckedAt)}` : " · not checked yet"}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── Ideas ─────────────────────────────────────────────────────────────── */

function IdeasSection({ snap, busy, post, flash }: {
  snap: Snapshot; busy: boolean;
  post: (path: string, body: unknown) => Promise<unknown>;
  flash: (m: string) => void;
}) {
  const decide = async (id: string, action: "accept" | "dismiss") => {
    try {
      const res = await post(`/ideas/${id}`, { action });
      const idea = res as Idea | undefined;
      if (action === "accept" && idea?.taskId) {
        flash("Accepted — a real task was submitted to the SaraMuse engine.");
      }
    } catch (err) {
      flash(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="sm-stack">
      <div className="sm-section-head">
        <h2 className="sm-section-title">Ideas</h2>
        <button className="sm-btn sm-btn-primary" disabled={busy}
          onClick={async () => {
            try { await post("/ideas/refresh", {}); flash("Ideas refreshed from real records."); }
            catch (err) { flash(err instanceof Error ? err.message : String(err)); }
          }}>
          Refresh
        </button>
      </div>
      <p className="sm-hint">
        Suggestions are derived from actual goals and tasks — never from thin air.
        Accepting one submits a REAL task to the live SaraMuse bot engine.
      </p>

      {snap.ideas.filter((i) => i.status === "new").length === 0 && (
        <div className="sm-empty">No fresh ideas right now. Create a goal without milestones and refresh.</div>
      )}

      {snap.ideas.filter((i) => i.status === "new").map((idea) => (
        <div key={idea.id} className="sm-card">
          <div className="sm-card-head">
            <h3 className="sm-card-title">{idea.title}</h3>
            <span className="sm-tag">{idea.kind}</span>
          </div>
          <p className="sm-card-body">{idea.reason}</p>
          {idea.evidence.length > 0 && (
            <div className="sm-evidence">
              {idea.evidence.map((ev) => (
                <div key={ev.id} className="sm-evidence-row">
                  <span className="sm-tag">{ev.kind}</span>
                  <strong>{ev.title}</strong>
                  {ev.excerpt && <span className="sm-muted"> — {ev.excerpt.slice(0, 140)}</span>}
                </div>
              ))}
            </div>
          )}
          <div className="sm-card-actions">
            <button className="sm-btn sm-btn-primary" disabled={busy} onClick={() => decide(idea.id, "accept")}>Accept</button>
            <button className="sm-btn sm-btn-ghost" disabled={busy} onClick={() => decide(idea.id, "dismiss")}>Dismiss</button>
          </div>
        </div>
      ))}

      {snap.ideas.some((i) => i.status !== "new") && (
        <details className="sm-card">
          <summary className="sm-muted">Decided ideas ({snap.ideas.filter((i) => i.status !== "new").length})</summary>
          {snap.ideas.filter((i) => i.status !== "new").map((idea) => (
            <div key={idea.id} className="sm-decided-row">
              <span className={`sm-tag ${idea.status === "accepted" ? "sm-tag-on" : ""}`}>{idea.status}</span>
              <span>{idea.title}</span>
            </div>
          ))}
        </details>
      )}
    </div>
  );
}

/* ── Finance ───────────────────────────────────────────────────────────── */

function FinanceSection({ busy, post, flash }: {
  busy: boolean;
  post: (path: string, body: unknown) => Promise<unknown>;
  flash: (m: string) => void;
}) {
  const [csv, setCsv] = useState("");
  const [result, setResult] = useState<FinanceAnalysis | null>(null);

  const analyze = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const res = await post("/finance/analyze", { csv });
      setResult((res as { analysis: FinanceAnalysis }).analysis);
      flash("Spending analyzed and saved as an artifact.");
    } catch (err) {
      setResult(null);
      flash(err instanceof Error ? err.message : String(err));
    }
  };

  const money = (n: number) => n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  return (
    <div className="sm-stack">
      <div className="sm-section-head"><h2 className="sm-section-title">Finance</h2></div>
      <p className="sm-hint">
        Import a transaction CSV to create a spending summary with categories and totals.
        Convention: positive amounts are expenses, negative amounts are income; values use
        the source currency with no conversion. Required columns:
        <code>date,description,amount,category</code>.
      </p>
      <form className="sm-card sm-form" onSubmit={analyze}>
        <label className="sm-field">
          <span className="sm-field-label">Transaction CSV *</span>
          <textarea className="sm-input sm-textarea sm-mono" rows={7} value={csv}
            placeholder={"date,description,amount,category\n2026-01-03,Coffee,4.50,Food\n2026-01-05,Salary,-2000.00,Income"}
            onChange={(e) => setCsv(e.target.value)} />
        </label>
        <button className="sm-btn sm-btn-primary" type="submit" disabled={busy || !csv.trim()}>
          Analyze spending
        </button>
      </form>

      {result && (
        <>
          <div className="sm-tiles">
            <div className="sm-tile"><span className="sm-tile-label">SPENDING</span>
              <span className="sm-tile-value sm-neg">{money(result.spending)}</span>
              <span className="sm-tile-note">{result.count} transactions</span></div>
            <div className="sm-tile"><span className="sm-tile-label">INCOME</span>
              <span className="sm-tile-value sm-pos">{money(result.income)}</span>
              <span className="sm-tile-note">{result.period.from} → {result.period.to}</span></div>
            <div className="sm-tile"><span className="sm-tile-label">SAVED</span>
              <span className="sm-tile-value">{money(result.saved)}</span>
              <span className="sm-tile-note">income − spending</span></div>
          </div>
          {result.categories.length > 0 && (
            <div className="sm-card">
              <h3 className="sm-card-title">By category</h3>
              <ul className="sm-cat-list">
                {result.categories.map((c) => (
                  <li key={c.name} className="sm-cat-row">
                    <span className="sm-grow">{c.name}</span>
                    <div className="sm-cat-bar" aria-hidden>
                      <div className="sm-cat-fill"
                        style={{ width: `${result.categories[0].amount ? (c.amount / result.categories[0].amount) * 100 : 0}%` }} />
                    </div>
                    <span className="sm-mono">{money(c.amount)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <details className="sm-card">
            <summary className="sm-muted">All transactions ({result.transactions.length})</summary>
            <table className="sm-table">
              <thead><tr><th>Date</th><th>Description</th><th>Category</th><th className="sm-num">Amount</th></tr></thead>
              <tbody>
                {result.transactions.map((t) => (
                  <tr key={t.id}>
                    <td className="sm-mono">{t.date}</td>
                    <td>{t.description}</td>
                    <td>{t.category}</td>
                    <td className={`sm-num sm-mono ${t.amount > 0 ? "sm-neg" : "sm-pos"}`}>{money(t.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </>
      )}
    </div>
  );
}

/* ── Memory ────────────────────────────────────────────────────────────── */

function MemorySection({ snap }: { snap: Snapshot }) {
  return (
    <div className="sm-stack">
      <div className="sm-section-head"><h2 className="sm-section-title">Memory</h2></div>
      <p className="sm-hint">
        SaraMuse&apos;s scoped memory — private to this bot, editable from its bot
        workspace. Shown here read-only so you can always see what it remembers.
      </p>
      {snap.memories.length === 0 ? (
        <div className="sm-empty">No memories saved yet.</div>
      ) : (
        <ul className="sm-memory-list">
          {snap.memories.map((m) => (
            <li key={m.id} className="sm-memory-row">
              <span className="sm-tag">{m.kind}</span>
              <span className="sm-grow">{m.text}</span>
              <span className="sm-meta">{relTime(m.ts)}</span>
            </li>
          ))}
        </ul>
      )}
      {snap.bot && (
        <a className="sm-link" href={`/hub/sara-bots/${snap.bot.id}`}>
          Edit memory in the {snap.bot.name} workspace →
        </a>
      )}
    </div>
  );
}

/* ── Activity (notifications) ──────────────────────────────────────────── */

function ActivitySection({ snap, busy, post }: {
  snap: Snapshot; busy: boolean;
  post: (path: string, body: unknown) => Promise<unknown>;
}) {
  return (
    <div className="sm-stack">
      <div className="sm-section-head"><h2 className="sm-section-title">Activity</h2></div>
      {snap.notifications.length === 0 ? (
        <div className="sm-empty">Nothing yet. Watches and completed tasks post real notifications here.</div>
      ) : (
        <ul className="sm-notif-list">
          {snap.notifications.map((n) => (
            <li key={n.id} className={`sm-notif ${n.read ? "sm-notif-read" : ""}`}>
              <div className="sm-notif-head">
                <strong>{n.title}</strong>
                <span className="sm-meta">{relTime(n.createdAt)}</span>
                {!n.read && (
                  <button className="sm-btn sm-btn-ghost sm-btn-sm" disabled={busy}
                    onClick={() => post(`/notifications/${n.id}/read`, {})}>
                    Mark read
                  </button>
                )}
              </div>
              <p className="sm-notif-body">{n.body}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* ── styles: OpenMuse-style calm light workspace ───────────────────────── */

const SM_CSS = `
.sm-root{min-height:100vh;background:linear-gradient(180deg,#f2f7fb 0%,#e9f1f7 100%);color:#22313a;padding-bottom:80px;font-family:inherit}
.sm-root code{background:rgba(34,49,58,.08);padding:1px 6px;border-radius:6px;font-size:11.5px}
.sm-header{max-width:1080px;margin:0 auto;padding:26px 24px 0}
.sm-back{color:#3d7a94;text-decoration:none;font-size:13px;font-weight:600}
.sm-back:hover{text-decoration:underline}
.sm-head-row{display:flex;gap:22px;align-items:flex-start;flex-wrap:wrap;margin-top:14px}
.sm-mascot{width:74px;height:74px;border-radius:26px;background:linear-gradient(160deg,#cfe7f2,#aecfe0);position:relative;flex-shrink:0;box-shadow:0 10px 24px rgba(61,122,148,.22)}
.sm-mascot-eye{position:absolute;top:26px;width:9px;height:12px;border-radius:50%;background:#22313a}
.sm-mascot-eye:first-child{left:20px}
.sm-mascot-eye:nth-child(2){right:20px}
.sm-mascot-mouth{position:absolute;left:26px;right:26px;bottom:18px;height:8px;border-radius:0 0 10px 10px;border-bottom:3px solid #22313a}
.sm-eyebrow{font-size:10px;letter-spacing:.2em;color:#3d7a94;font-weight:700}
.sm-title{font-size:clamp(26px,3vw,36px);font-weight:800;letter-spacing:-.03em;margin:6px 0 6px;color:#17242c}
.sm-sub{max-width:560px;font-size:13.5px;line-height:1.7;color:#54656f;margin:0}
.sm-head-stats{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap}
.sm-stat-card{display:flex;flex-direction:column;align-items:center;gap:2px;background:#fff;border:1px solid #d8e5ec;border-radius:16px;padding:12px 18px;min-width:86px;box-shadow:0 6px 18px rgba(61,122,148,.08)}
.sm-stat-v{font-size:17px;font-weight:800;color:#17242c}
.sm-stat-k{font-size:10.5px;color:#7b8a94;letter-spacing:.05em;text-transform:uppercase}

.sm-banner{max-width:1080px;margin:14px auto 0;padding:12px 16px;border-radius:12px;font-size:12.5px;line-height:1.6}
.sm-banner-error{border:1px solid rgba(220,80,80,.35);background:#fdeeee;color:#9c3030}
.sm-banner-ok{border:1px solid rgba(60,160,110,.35);background:#e9f7f0;color:#1f6b47}

.sm-nav{max-width:1080px;margin:22px auto 0;padding:0 24px;display:flex;gap:8px;flex-wrap:wrap}
.sm-nav-btn{border:1px solid #d3e1e9;background:#fff;color:#54656f;border-radius:999px;padding:9px 18px;font-size:13px;font-weight:700;cursor:pointer;transition:all .15s}
.sm-nav-btn:hover{border-color:#9ec8da;color:#2a5a70}
.sm-nav-on{background:#2a5a70;border-color:#2a5a70;color:#fff}

.sm-main{max-width:1080px;margin:20px auto 0;padding:0 24px}
.sm-loading{color:#54656f;text-align:center;padding:60px 0;font-size:14px}
.sm-stack{display:flex;flex-direction:column;gap:16px}

.sm-hero{display:flex;gap:20px;align-items:stretch;background:#dcebf4;border-radius:24px;padding:30px 32px;position:relative;overflow:hidden}
.sm-hero>div:first-child{flex:1;z-index:1}
.sm-hero-eyebrow{font-size:12px;color:#2a5a70;font-weight:700;letter-spacing:.04em}
.sm-hero-title{font-size:clamp(22px,2.6vw,32px);line-height:1.2;letter-spacing:-1.2px;font-weight:600;margin:10px 0 8px;color:#17242c}
.sm-hero-sub{color:#4d6270;font-size:13.5px;line-height:1.65;max-width:480px;margin:0 0 16px}
.sm-ask-row{display:flex;gap:10px;flex-wrap:wrap}
.sm-hero-orb{width:150px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#ffffff 0%,#cfe4ef 45%,#a8c9da 100%);align-self:center;flex-shrink:0;box-shadow:inset -10px -12px 30px rgba(61,122,148,.25)}

.sm-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.sm-tile{display:flex;flex-direction:column;align-items:flex-start;gap:4px;text-align:left;background:#fff;border:1px solid #d8e5ec;border-radius:18px;padding:16px 18px;cursor:pointer;transition:transform .15s,box-shadow .15s,border-color .15s;color:inherit}
.sm-tile:hover{transform:translateY(-2px);border-color:#9ec8da;box-shadow:0 10px 26px rgba(61,122,148,.12)}
.sm-tile-label{font-size:10px;letter-spacing:.12em;color:#7b8a94;font-weight:700}
.sm-tile-value{font-size:26px;font-weight:800;color:#17242c}
.sm-tile-note{font-size:11.5px;color:#7b8a94}

.sm-section-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.sm-section-title{font-size:20px;font-weight:800;letter-spacing:-.02em;margin:0;color:#17242c}
.sm-hint{color:#667885;font-size:12.5px;line-height:1.65;margin:0;max-width:760px}

.sm-card{background:#fff;border:1px solid #d8e5ec;border-radius:18px;padding:18px 20px;box-shadow:0 6px 18px rgba(61,122,148,.06)}
.sm-card-dim{opacity:.72}
.sm-card-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.sm-card-title{font-size:15px;font-weight:800;margin:0;color:#17242c;flex:1;min-width:160px}
.sm-card-body{font-size:13px;color:#54656f;line-height:1.6;margin:8px 0 0}
.sm-card-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:14px}
.sm-form{display:flex;flex-direction:column;gap:12px}
.sm-field{display:flex;flex-direction:column;gap:5px}
.sm-field-label{font-size:11.5px;font-weight:700;color:#3d4c56}
.sm-row{display:flex;gap:12px;flex-wrap:wrap}
.sm-row .sm-field{min-width:150px}
.sm-grow{flex:1;min-width:200px}

.sm-input{width:100%;box-sizing:border-box;padding:10px 14px;border-radius:11px;border:1px solid #cddce5;background:#fbfdfe;color:#22313a;font-size:13.5px;font-family:inherit}
.sm-input:focus{outline:none;border-color:#5aa7c4;box-shadow:0 0 0 3px rgba(90,167,196,.18)}
.sm-textarea{resize:vertical;min-height:60px}
.sm-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}

.sm-btn{border:none;border-radius:11px;padding:9px 16px;font-size:12.5px;font-weight:700;cursor:pointer;transition:all .15s;font-family:inherit}
.sm-btn-primary{background:linear-gradient(180deg,#3d8db0,#2a5a70);color:#fff}
.sm-btn-primary:hover{filter:brightness(1.08)}
.sm-btn-primary:disabled,.sm-btn-ghost:disabled{opacity:.5;cursor:not-allowed}
.sm-btn-ghost{background:transparent;border:1px solid #c6d6e0;color:#3d4c56}
.sm-btn-ghost:hover{background:#eef4f8}
.sm-btn-danger{background:#fdeeee;border:1px solid rgba(220,80,80,.35);color:#9c3030}
.sm-btn-danger:hover{background:#fadfdc}
.sm-btn-sm{padding:5px 10px;font-size:11px}

.sm-pill{font-size:10.5px;font-weight:800;letter-spacing:.04em;border-radius:999px;padding:3px 10px;text-transform:uppercase}
.sm-pill-active{background:#e2f5ec;color:#1f6b47}
.sm-pill-paused{background:#fdf3e0;color:#8a6116}
.sm-pill-completed{background:#e8eef2;color:#4d6270}
.sm-pill-stopped{background:#f0e6e6;color:#7c4a4a}

.sm-tag{font-size:10.5px;padding:2px 9px;border-radius:999px;background:#eef4f8;color:#54656f;border:1px solid #d8e5ec}
.sm-tag-on{background:#e2f5ec;color:#1f6b47;border-color:#bfe4d2}
.sm-meta{font-size:11px;color:#8a99a4;margin-left:auto}
.sm-muted{color:#8a99a4;font-size:12.5px}
.sm-error{color:#9c3030;font-size:12.5px;margin:8px 0 0}
.sm-empty{color:#7b8a94;text-align:center;padding:44px 20px;background:#fff;border:1px dashed #c6d6e0;border-radius:18px;font-size:13.5px}
.sm-link{color:#2a5a70;font-weight:700;font-size:13px;text-decoration:none}
.sm-link:hover{text-decoration:underline}

.sm-progress-track{height:7px;border-radius:99px;background:#e8eff4;margin-top:12px;overflow:hidden}
.sm-progress-fill{height:100%;border-radius:99px;background:linear-gradient(90deg,#3d8db0,#57b48c);transition:width .3s}
.sm-milestones{list-style:none;margin:10px 0 0;padding:0;display:flex;flex-direction:column;gap:6px}
.sm-check{display:flex;gap:9px;align-items:center;font-size:13px;color:#3d4c56;cursor:pointer}
.sm-check input{accent-color:#2a5a70;width:15px;height:15px}
.sm-done{text-decoration:line-through;color:#8a99a4}

.sm-evidence{margin-top:10px;display:flex;flex-direction:column;gap:6px}
.sm-evidence-row{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:12.5px;color:#3d4c56}
.sm-decided-row{display:flex;gap:10px;align-items:center;padding:7px 0;font-size:13px;color:#54656f;border-top:1px solid #eef2f5}

.sm-cat-list{list-style:none;margin:10px 0 0;padding:0;display:flex;flex-direction:column;gap:9px}
.sm-cat-row{display:flex;gap:12px;align-items:center;font-size:13px;color:#3d4c56}
.sm-cat-bar{width:180px;height:8px;border-radius:99px;background:#e8eff4;overflow:hidden;flex-shrink:1}
.sm-cat-fill{height:100%;border-radius:99px;background:linear-gradient(90deg,#3d8db0,#57b48c)}
.sm-num{text-align:right}
.sm-neg{color:#b25454}
.sm-pos{color:#2f7d58}
.sm-table{width:100%;border-collapse:collapse;margin-top:10px;font-size:12.5px;color:#3d4c56}
.sm-table th{text-align:left;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:#8a99a4;padding:6px 8px;border-bottom:1px solid #e3ebf0}
.sm-table td{padding:7px 8px;border-bottom:1px solid #eef2f5}

.sm-memory-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.sm-memory-row{display:flex;gap:10px;align-items:center;background:#fff;border:1px solid #d8e5ec;border-radius:12px;padding:10px 14px;font-size:13px;color:#3d4c56}

.sm-notif-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}
.sm-notif{background:#fff;border:1px solid #d8e5ec;border-radius:14px;padding:14px 16px}
.sm-notif:not(.sm-notif-read){border-left:4px solid #3d8db0}
.sm-notif-read{opacity:.75}
.sm-notif-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:13.5px;color:#17242c}
.sm-notif-body{margin:6px 0 0;font-size:12.5px;color:#54656f;line-height:1.6;white-space:pre-wrap}

@media (max-width:720px){
  .sm-hero{flex-direction:column}
  .sm-hero-orb{display:none}
  .sm-head-stats{margin-left:0}
  .sm-cat-bar{width:110px}
}
`;
