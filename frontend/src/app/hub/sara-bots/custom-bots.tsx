"use client";

/**
 * SARA BOTS — "My Bots" (custom worker bots).
 *
 * Everything on this screen is backed by the real custom-bot backend:
 *   • bots      — persisted definitions whose tools exist in the LIVE registry
 *   • tasks     — durable engine tasks (they keep running if this page closes)
 *   • approvals — real owner decisions required before gated steps execute
 *   • memory    — per-bot, structurally isolated files
 *   • schedules — persistent intervals that submit real tasks
 *
 * The UI never invents a status: every label, blocker and hint below comes
 * from a backend response. The factory only proposes tools that exist; the
 * server drops anything the model hallucinated and says so.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { SARA_API_URL, saraFetch } from "@/lib/api";

/* ── types (mirror the backend payloads) ─────────────────────────────────── */

interface ToolInfo {
  id: string;
  name: string;
  kind: string;
  description: string;
  params: Record<string, { type: string; description: string; required: boolean }>;
  risk: string;
  state: string;
  detail: string;
  requires: string[];
  runnable: boolean;
}

interface PlanStep {
  id: number;
  title: string;
  tool: string;
  params: Record<string, unknown>;
  depends_on: number[];
  status: string;
  output: string;
  error: string;
  run_id: string | null;
  approval_id: string | null;
  force_confirm: boolean;
  started_at: number | null;
  finished_at: number | null;
  /** agent.ask_owner: the question shown while the task is parked. */
  question?: string;
  /** agent.ask_owner: optional quick-reply choices. */
  options?: string[];
  answer?: string;
}

interface TaskEvent {
  t: number;
  event: string;
  detail?: string;
}

interface BotTask {
  id: string;
  dot_id: string;
  goal: string;
  kind: string;
  schedule_id?: string | null;
  status: string;
  plan: PlanStep[];
  current_step: number;
  events: TaskEvent[];
  outputs: Record<string, string>;
  result_text: string;
  error: string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  bot_name?: string;
  deduplicated?: boolean;
}

interface ApprovalRecord {
  id: string;
  task_id: string;
  dot_id: string;
  step_index: number;
  tool: string;
  reason: string;
  params_preview: string;
  status: string;
  created_at: number;
  decided_at: number | null;
  expires_at: number;
  bot_name?: string;
  goal?: string;
  task_status?: string;
}

interface ScheduleRecord {
  id: string;
  dot_id: string;
  goal: string;
  interval_minutes: number;
  enabled: boolean;
  created_at: number;
  last_run_at: number | null;
  next_run_at: number;
  runs: number;
  skipped: number;
  bot_name?: string;
}

interface MemoryEntry {
  id: string;
  kind: string;
  text: string;
  created_at: number;
}

interface BrowserInfo {
  alive: boolean;
  url: string | null;
  title: string | null;
  profile: string;
  created_at: number | null;
  mode: string;
  png_b64?: string;
  shot_at?: number;
  shot_error?: string;
}

interface CustomBot {
  id: string;
  name: string;
  description: string;
  goal: string;
  instructions: string;
  category: string;
  language: string;
  model: string;
  output_format: string;
  tools: string[];
  skills: string[];
  autonomy: string;
  plan_mode: string;
  direct_tool: string;
  direct_params: Record<string, unknown>;
  memory_enabled: boolean;
  hue: number;
  status: string;
  created_at: number;
  updated_at: number;
  resolved_tools: ToolInfo[];
  ready_tools: number;
  tool_count: number;
  active_tasks?: number;
  last_task?: BotTask | null;
  tasks?: BotTask[];
  schedules?: ScheduleRecord[];
  memory?: MemoryEntry[];
  approvals_pending?: ApprovalRecord[];
}

interface EngineStatus {
  engine_running: boolean;
  current_task_id: string | null;
  queued: number;
  active: number;
  llm_ready: boolean;
  llm_live: string | null;
  worker: string;
}

interface Readiness {
  bot_id: string;
  runnable: boolean;
  ready_tools: string[];
  blockers: { kind: string; detail: string }[];
  hints: string[];
  note: string;
}

interface Proposal {
  name: string;
  description: string;
  goal: string;
  instructions: string;
  category: string;
  autonomy: string;
  plan_mode: string;
  tools: string[];
  skills: string[];
  direct_tool: string;
  direct_params: Record<string, unknown>;
  output_format: string;
  memory_enabled: boolean;
  schedule: { interval_minutes: number; goal: string } | null;
  missing: string[];
  dropped: { tool: string; reason: string }[];
}

interface WizardDraft {
  name: string;
  description: string;
  goal: string;
  instructions: string;
  category: string;
  autonomy: string;
  plan_mode: string;
  direct_tool: string;
  direct_params_text: string;
  output_format: string;
  memory_enabled: boolean;
  tools: string[];
  skills: string[];
  hue: number;
}

interface WizardState {
  step: number; // 1 describe · 2 tools · 3 behaviour · 4 schedule · 5 review
  editing: string | null; // bot id when editing an existing bot
  description: string;
  proposing: boolean;
  hueTouched: boolean; // explicit colour choice (else derived from the name)
  draft: WizardDraft;
  notes: { missing: string[]; dropped: { tool: string; reason: string }[] } | null;
  scheduleOn: boolean;
  scheduleInterval: number;
  scheduleGoal: string;
  error: string;
  saving: boolean;
}

/* ── small helpers ───────────────────────────────────────────────────────── */

const CATEGORIES = ["research", "monitor", "ops", "coder", "business",
  "creative", "data", "custom"];
const AUTONOMY = ["assist", "supervised", "autonomous"] as const;
const MEMORY_KINDS = ["preference", "fact", "project", "task", "feedback"];
const ACTIVE_TASK_STATES = new Set(["queued", "planning", "running",
  "waiting_approval", "waiting_input", "paused"]);

/** Custom bot ids are minted as `<prefix>_<hex>` (the backend's own `_ID_RE`).
 *  Desktop/local bots carry dotted ids like `action.system_diagnostics` and
 *  are NOT custom bots: asking /custom/<id> about one is always a 422, so the
 *  page must never select — or keep polling — a non-custom id. */
const CUSTOM_ID_RE = /^[a-z]{2,5}_[a-z0-9]{6,40}$/;

/** Orb palette + deterministic default: the same bot name always maps to the
 *  same colour, so an orb never changes on its own — and the owner can pick
 *  an exact colour in the wizard. */
const HUE_SWATCHES = [0, 25, 45, 90, 145, 175, 200, 225, 260, 290, 320, 345];

const hueForName = (name: string): number => {
  const clean = name.trim().toLowerCase();
  if (!clean) return 200;
  let h = 0;
  for (let i = 0; i < clean.length; i++) {
    h = (h * 31 + clean.charCodeAt(i)) >>> 0;
  }
  return h % 360;
};

const orbStyle = (hue: number, size = 58) => ({
  width: size, height: size,
  background: `radial-gradient(circle at 32% 28%, hsla(${hue},95%,78%,.95), hsla(${hue},85%,45%,.95) 55%, hsla(${hue + 30},80%,20%,.98))`,
  boxShadow: `0 0 14px hsla(${hue},90%,60%,.4)`,
});

const TASK_META: Record<string, { label: string; color: string }> = {
  queued: { label: "Queued", color: "#94a3b8" },
  planning: { label: "Planning", color: "#67e8f9" },
  running: { label: "Running", color: "#22d3ee" },
  waiting_approval: { label: "Waiting approval", color: "#fbbf24" },
  waiting_input: { label: "Waiting for you", color: "#fbbf24" },
  completed: { label: "Completed", color: "#34d399" },
  failed: { label: "Failed", color: "#f87171" },
  cancelled: { label: "Cancelled", color: "#64748b" },
  blocked: { label: "Blocked", color: "#f59e0b" },
  paused: { label: "Paused", color: "#a78bfa" },
};

const STEP_META: Record<string, { label: string; color: string }> = {
  pending: { label: "Pending", color: "#94a3b8" },
  running: { label: "Running", color: "#22d3ee" },
  waiting_approval: { label: "Waiting approval", color: "#fbbf24" },
  waiting_input: { label: "Needs your reply", color: "#fbbf24" },
  completed: { label: "Completed", color: "#34d399" },
  failed: { label: "Failed", color: "#f87171" },
  skipped: { label: "Skipped", color: "#64748b" },
  cancelled: { label: "Cancelled", color: "#64748b" },
  unknown: { label: "Unknown after restart", color: "#f59e0b" },
};

const TOOL_STATE_COLOR: Record<string, string> = {
  ready: "#34d399",
  needs_approval: "#fbbf24",
  needs_configuration: "#fbbf24",
  unavailable: "#f87171",
  disabled: "#64748b",
};

const KIND_LABEL: Record<string, string> = {
  action: "Local actions",
  hub: "Hub agents",
  agency: "Agency specialists",
  llm: "LLM",
  memory: "Memory",
  chat: "Chat with you",
  browser: "Bot browser",
};

function pillStyle(color: string): React.CSSProperties {
  return { borderColor: `${color}55`, color, background: `${color}14` };
}

function fmtWhen(ts?: number | null): string {
  if (!ts) return "—";
  const diff = Date.now() / 1000 - ts;
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleString();
}

function fmtIn(ts?: number | null): string {
  if (!ts) return "—";
  const diff = ts - Date.now() / 1000;
  if (diff <= 0) return "expired";
  if (diff < 60) return `in ${Math.ceil(diff)}s`;
  if (diff < 3600) return `in ${Math.ceil(diff / 60)}m`;
  return `in ${Math.ceil(diff / 3600)}h`;
}

function fmtDuration(a?: number | null, b?: number | null): string {
  if (!a || !b) return "—";
  const ms = Math.max(0, (b - a) * 1000);
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

function dayLabel(ts: number): string {
  const d = new Date(ts * 1000);
  const now = new Date();
  const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.floor((startOfDay(now) - startOfDay(d)) / 86400000);
  if (diffDays <= 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function runLabel(task: { started_at?: number | null; finished_at?: number | null }): string {
  if (!task.started_at) return "";
  return ` · ran ${fmtDuration(task.started_at, task.finished_at ?? Date.now() / 1000)}`;
}

function totalDur(task: { started_at?: number | null; finished_at?: number | null }): string {
  if (!task.started_at) return "not started yet";
  return fmtDuration(task.started_at, task.finished_at ?? Date.now() / 1000);
}

function clampText(text: string, max = 600): string {
  return text.length > max
    ? `${text.slice(0, max)}… (+${text.length - max} chars)`
    : text;
}

function makeKey(): string {
  try {
    if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
      return crypto.randomUUID();
    }
  } catch { /* fall through */ }
  return `ui-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await saraFetch(`${SARA_API_URL}/api/sara-bots${path}`,
    { cache: "no-store", ...init });
  if (!res.ok) {
    let detail = `backend responded ${res.status}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch { /* keep the status message */ }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function patch<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function StatusPill({ label, color }: { label: string; color: string }) {
  return (
    <span className="sb-pill" style={pillStyle(color)}>
      <span className="sb-pill-dot" style={{ background: color }} />
      {label}
    </span>
  );
}

function TaskPill({ status }: { status: string }) {
  const meta = TASK_META[status] ?? { label: status, color: "#94a3b8" };
  return <StatusPill label={meta.label} color={meta.color} />;
}

function StepPill({ status }: { status: string }) {
  const meta = STEP_META[status] ?? { label: status, color: "#94a3b8" };
  return <StatusPill label={meta.label} color={meta.color} />;
}

function BotStatusPill({ status }: { status: string }) {
  const meta = BOT_STATUS_META[status] ?? { label: status, color: "#94a3b8" };
  return <StatusPill label={meta.label} color={meta.color} />;
}

const BOT_STATUS_META: Record<string, { label: string; color: string }> = {
  active: { label: "Active", color: "#34d399" },
  paused: { label: "Paused", color: "#fbbf24" },
  disabled: { label: "Disabled", color: "#64748b" },
};

const AUTONOMY_HELP: Record<string, string> = {
  assist: "Ask before every write or external step (read-only steps run freely).",
  supervised: "Run read-only steps freely; ask before external/system-changing steps.",
  autonomous: "Run every assigned tool without asking — destructive parameters and permanent actions still require approval.",
};

/* ── tool picker ─────────────────────────────────────────────────────────── */

function ToolPicker({ tools, selected, onChange }: {
  tools: ToolInfo[];
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const [search, setSearch] = useState("");
  const [showBlocked, setShowBlocked] = useState(true);

  const filtered = useMemo(() => {
    const q = search.toLowerCase().trim();
    return tools
      .filter((t) => (showBlocked || t.state === "ready"))
      .filter((t) => !q ||
        t.id.toLowerCase().includes(q) ||
        t.name.toLowerCase().includes(q) ||
        t.description.toLowerCase().includes(q))
      .sort((a, b) => {
        const ready = (a.state === "ready" ? 0 : 1) - (b.state === "ready" ? 0 : 1);
        return ready !== 0 ? ready : a.id.localeCompare(b.id);
      });
  }, [tools, search, showBlocked]);

  const groups = useMemo(() => {
    const byKind = new Map<string, ToolInfo[]>();
    for (const tool of filtered) {
      const list = byKind.get(tool.kind) ?? [];
      list.push(tool);
      byKind.set(tool.kind, list);
    }
    return [...byKind.entries()];
  }, [filtered]);

  const toggle = (id: string) => {
    onChange(selected.includes(id)
      ? selected.filter((x) => x !== id)
      : [...selected, id]);
  };

  const readyCount = tools.filter((t) => t.state === "ready").length;

  return (
    <div className="cb-picker">
      <div className="cb-picker-bar">
        <input
          className="sb-input"
          placeholder="Search tools…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search tools"
        />
        <label className="cb-checkline">
          <input type="checkbox" checked={showBlocked}
            onChange={(e) => setShowBlocked(e.target.checked)} />
          <span>show blocked tools</span>
        </label>
        <span className="cb-picker-count">
          {selected.length} selected · {readyCount}/{tools.length} ready live
        </span>
      </div>
      <p className="sb-detail-note">
        Only tools from the live registry can be assigned. A blocked tool is
        still selectable — the task will report the real blocker at run time,
        and you can fix it in the Fleet view.
      </p>
      <div className="cb-picker-list">
        {groups.length === 0 && <div className="sb-empty">No tools match.</div>}
        {groups.map(([kind, list]) => (
          <div key={kind} className="cb-picker-group">
            <div className="cb-picker-group-title">{KIND_LABEL[kind] ?? kind}</div>
            {list.map((tool) => {
              const on = selected.includes(tool.id);
              const color = TOOL_STATE_COLOR[tool.state] ?? "#94a3b8";
              return (
                <label key={tool.id} className={`cb-tool ${on ? "cb-tool-on" : ""}`}>
                  <input type="checkbox" checked={on}
                    onChange={() => toggle(tool.id)} />
                  <span className="cb-tool-body">
                    <span className="cb-tool-row">
                      <span className="cb-tool-id">{tool.id}</span>
                      <span className="cb-risk">{tool.risk}</span>
                      <span className="cb-tstate" style={{ color }}>
                        ● {tool.state}
                      </span>
                    </span>
                    <span className="cb-tool-desc">{tool.description}</span>
                    {tool.state !== "ready" && tool.detail && (
                      <span className="cb-tool-detail">{tool.detail}</span>
                    )}
                  </span>
                </label>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── task row + expanded detail ──────────────────────────────────────────── */

function TaskSteps({ task }: { task: BotTask }) {
  if (!task.plan?.length) {
    return <p className="sb-detail-note">No plan recorded yet.</p>;
  }
  return (
    <div className="cb-steps">
      {task.plan.map((step) => (
        <div key={step.id} className="cb-step-row">
          <span className="cb-step-idx">{step.id + 1}</span>
          <div className="cb-step-main">
            <div className="cb-step-head">
              <span className="cb-step-title">{step.title}</span>
              <span className="cb-step-tool">{step.tool}</span>
              <StepPill status={step.status} />
              {step.force_confirm && (
                <span className="cb-step-note" title="The replayed call carries an explicit confirm flag because you approved it.">
                  approved → replayed with confirm
                </span>
              )}
            </div>
            {step.params && Object.keys(step.params).length > 0 && (
              <pre className="cb-params">{JSON.stringify(step.params)}</pre>
            )}
            {step.output && <pre className="sb-output">{step.output}</pre>}
            {step.error && <pre className="sb-output sb-output-err">{step.error}</pre>}
            <span className="cb-step-when">
              {step.finished_at
                ? `took ${fmtDuration(step.started_at, step.finished_at)}`
                : step.started_at ? "running…" : ""}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── the bot's chat question (agent.ask_owner) ───────────────────────────── */

function ReplyBox({ task, busy, onReply }: {
  task: BotTask;
  busy: boolean;
  onReply?: (taskId: string, text: string) => void;
}) {
  const [text, setText] = useState("");
  const step = task.plan[task.current_step];
  const question = step?.question || "The bot is waiting for your reply.";
  const options = (step?.options ?? []).filter((o) => o.trim()).slice(0, 6);
  const send = (value: string) => {
    const v = value.trim();
    if (!v || busy || !onReply) return;
    onReply(task.id, v);
    setText("");
  };
  return (
    <div className="cb-reply">
      <div className="cb-reply-q">
        <span className="cb-reply-badge">The bot asks</span>
        <span className="cb-reply-text">{question}</span>
      </div>
      {options.length > 0 && (
        <div className="cb-reply-opts">
          {options.map((o) => (
            <button key={o} type="button" className="sb-btn" disabled={busy || !onReply}
              onClick={() => send(o)}>
              {o}
            </button>
          ))}
        </div>
      )}
      <div className="cb-reply-row">
        <input className="sb-input" value={text} disabled={busy || !onReply}
          placeholder={onReply ? "e.g. Reply to 1 and 2, skip 3"
            : "replying is unavailable here"}
          aria-label="Your reply"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(text);
            }
          }} />
        <button type="button" className="sb-btn sb-btn-primary"
          disabled={busy || !text.trim() || !onReply}
          onClick={() => send(text)}>
          Reply
        </button>
      </div>
      <p className="cb-reply-hint">
        Your reply becomes the bot&apos;s next step input — the task resumes
        from exactly where it paused, even if this page was closed.
      </p>
    </div>
  );
}

function TaskBody({ task, busy, onCancel, onResume, onDecideHint, onWorkflow, onReply }: {
  task: BotTask;
  busy: boolean;
  onCancel: (id: string) => void;
  onResume: (id: string) => void;
  onDecideHint: () => void;
  onWorkflow?: () => void;
  onReply?: (taskId: string, text: string) => void;
}) {
  const active = ACTIVE_TASK_STATES.has(task.status);
  return (
    <div className="cb-task-body">
      <div className="cb-task-meta">
        <span className="cb-meta">task {task.id}</span>
        <span className="cb-meta">created {fmtWhen(task.created_at)}</span>
        <span className="cb-meta">
          steps {task.plan.filter((s) => s.status === "completed").length}/{task.plan.length || "?"}
        </span>
        {task.status === "waiting_approval" && (
          <span className="cb-meta cb-warn">
            a step is waiting for your decision — open the Approvals tab
          </span>
        )}
        {task.status === "waiting_input" && (
          <span className="cb-meta cb-warn">
            the bot asked you a question — reply below to continue
          </span>
        )}
      </div>

      {task.status === "waiting_input" && (
        <ReplyBox task={task} busy={busy} onReply={onReply} />
      )}

      {task.error && task.status !== "completed" && (
        <pre className="sb-output sb-output-err">{task.error}</pre>
      )}
      <TaskSteps task={task} />

      {task.result_text && (
        <>
          <h4 className="cb-sub-title">Result</h4>
          <pre className="sb-output">{task.result_text}</pre>
        </>
      )}

      {task.events?.length > 0 && (
        <div className="cb-task-events">
          {task.events.slice(-14).map((ev, i) => (
            <span key={i} className="sb-run-event" title={ev.detail ?? ""}>
              {ev.event}
            </span>
          ))}
        </div>
      )}

      <div className="cb-task-actions">
        {onWorkflow && (
          <button type="button" className="sb-btn" onClick={onWorkflow}
            title="See the step-by-step flow of how this run actually executed.">
            View workflow
          </button>
        )}
        {task.status === "waiting_approval" && (
          <button type="button" className="sb-btn" onClick={onDecideHint}>
            Go to approval
          </button>
        )}
        {active && (
          <button type="button" className="sb-btn" disabled={busy}
            onClick={() => onCancel(task.id)}>
            Cancel task
          </button>
        )}
        {(task.status === "blocked" || task.status === "paused") && (
          <button type="button" className="sb-btn sb-btn-primary" disabled={busy}
            onClick={() => onResume(task.id)}
            title="Requeue the task; unknown side-effecting steps are re-planned, never re-run blindly.">
            Resume task
          </button>
        )}
      </div>
    </div>
  );
}

/* ── chat-style execution history ────────────────────────────────────────── */

function ChatEntry({ task, hue, open, busy, onToggle, onCancel, onResume, onDecideHint, onWorkflow, onReply }: {
  task: BotTask;
  hue: number;
  open: boolean;
  busy: boolean;
  onToggle: () => void;
  onCancel: (id: string) => void;
  onResume: (id: string) => void;
  onDecideHint: () => void;
  onWorkflow?: () => void;
  onReply?: (taskId: string, text: string) => void;
}) {
  const meta = TASK_META[task.status] ?? { label: task.status, color: "#94a3b8" };
  const active = ACTIVE_TASK_STATES.has(task.status);
  const runningStep = task.plan[task.current_step];
  const doneSteps = task.plan.filter((s) => s.status === "completed").length;
  const dur = runLabel(task);

  let output: React.ReactNode;
  if (task.status === "completed") {
    output = task.result_text ? (
      <pre className="cb-chat-out">{task.result_text}</pre>
    ) : (
      <div className="cb-chat-out cb-chat-plain">Completed — the run produced no result text.</div>
    );
  } else if (task.status === "failed") {
    output = (
      <pre className="cb-chat-out cb-chat-err">
        {task.error || "The run failed but recorded no error detail."}
      </pre>
    );
  } else if (task.status === "waiting_approval") {
    output = (
      <div className="cb-chat-out cb-chat-plain cb-chat-warn">
        Paused on step {task.current_step + 1}
        {runningStep ? ` (${runningStep.tool})` : ""} — this call needs your approval
        before it may run.
        <button type="button" className="cb-chat-link" onClick={onDecideHint}>
          Review in Approvals
        </button>
      </div>
    );
  } else if (task.status === "blocked") {
    output = (
      <div className="cb-chat-out cb-chat-plain cb-chat-warn">
        Blocked{task.error ? ` — ${task.error}` : ""}. Resuming re-plans steps whose
        side effects are unknown; nothing is re-run blindly.
      </div>
    );
  } else if (task.status === "cancelled") {
    output = (
      <div className="cb-chat-out cb-chat-plain">
        Cancelled — the engine stopped at the next safe boundary.
      </div>
    );
  } else if (active) {
    const label = task.status === "queued"
      ? "Queued — waiting for the worker to pick it up…"
      : task.status === "planning"
        ? "Planning — the live LLM is choosing steps from the assigned tools…"
        : task.status === "paused"
          ? "Paused — no new steps will start."
          : runningStep
            ? `Step ${task.current_step + 1}/${task.plan.length} · ${runningStep.tool} is running…`
            : "Running…";
    output = (
      <div className="cb-chat-out cb-chat-plain cb-chat-live">
        <span className="cb-chat-caret" />
        {label}
      </div>
    );
  } else {
    output = <div className="cb-chat-out cb-chat-plain">{meta.label}.</div>;
  }

  return (
    <div className="cb-chat-entry">
      <span className="cb-chat-orb" style={{
        background: `radial-gradient(circle at 32% 28%, hsla(${hue},95%,78%,.95), hsla(${hue},85%,45%,.95) 55%, hsla(${hue + 30},80%,20%,.98))`,
        boxShadow: `0 0 14px hsla(${hue},90%,60%,.4)`,
      }} />
      <div className="cb-chat-main">
        <div className="cb-chat-head">
          <span className="cb-chat-bot">{task.bot_name ?? "(deleted bot)"}</span>
          <span className="sb-kind-tag">{task.kind === "schedule" ? "SCHEDULED TICK" : "YOU ASKED"}</span>
          <TaskPill status={task.status} />
          <span className="cb-chat-time"
            title={new Date(task.created_at * 1000).toLocaleString()}>
            {fmtWhen(task.created_at)}{dur}
          </span>
        </div>

        <div className="cb-chat-goal">{task.goal}</div>

        {output}

        {task.plan.length > 0 && (
          <div className="cb-chat-steps" aria-label="Step progress">
            {task.plan.map((step) => {
              const sm = STEP_META[step.status] ?? { label: step.status, color: "#94a3b8" };
              return (
                <span key={step.id} className="cb-chat-step"
                  style={{ background: `${sm.color}22`, borderColor: `${sm.color}88`, color: sm.color }}
                  title={`${step.id + 1}. ${step.title} — ${sm.label}`}>
                  {step.id + 1}
                </span>
              );
            })}
            <span className="cb-meta">{doneSteps}/{task.plan.length} steps done</span>
          </div>
        )}

        <div className="cb-chat-foot">
          <span className="cb-meta">{task.id}</span>
          <button type="button" className="cb-chat-open" aria-expanded={open}
            onClick={onToggle}>
            {open ? "Hide execution" : "Open execution"}
          </button>
        </div>

        {task.status === "waiting_input" && !open && (
          <ReplyBox task={task} busy={busy} onReply={onReply} />
        )}

        {open && (
          <TaskBody task={task} busy={busy}
            onCancel={onCancel} onResume={onResume} onDecideHint={onDecideHint}
            onWorkflow={onWorkflow} onReply={onReply} />
        )}
      </div>
    </div>
  );
}

/* ── vertical chat thread (the Tasks tab) ────────────────────────────────── */

function TaskTurn({ task, hue, busy, showBot, onCancel, onResume, onDecideHint, onWorkflow, onReply }: {
  task: BotTask;
  hue: number;
  busy: boolean;
  showBot?: boolean;
  onCancel: (id: string) => void;
  onResume: (id: string) => void;
  onDecideHint: () => void;
  onWorkflow?: () => void;
  onReply?: (taskId: string, text: string) => void;
}) {
  const active = ACTIVE_TASK_STATES.has(task.status);
  const doneSteps = task.plan.filter((s) => s.status === "completed").length;
  const runningStep = task.plan[task.current_step];
  const waiting = task.status === "waiting_input" && runningStep?.tool === "agent.ask_owner"
    ? runningStep : undefined;
  return (
    <div className="cb-turn">
      <div className="cb-turn-row cb-turn-you">
        <div className="cb-bubble cb-bubble-you">
          <div className="cb-bubble-head">
            <span className="cb-bubble-who">You</span>
            {task.kind === "schedule" && <span className="sb-kind-tag">SCHEDULED TICK</span>}
            <span className="cb-bubble-when">{fmtWhen(task.created_at)}</span>
          </div>
          <div className="cb-bubble-text">{task.goal}</div>
        </div>
      </div>

      <div className="cb-turn-row">
        <span className="cb-turn-orb" style={{
          background: `radial-gradient(circle at 32% 28%, hsla(${hue},95%,78%,.95), hsla(${hue},85%,45%,.95) 55%, hsla(${hue + 30},80%,20%,.98))`,
          boxShadow: `0 0 14px hsla(${hue},90%,60%,.4)`,
        }} />
        <div className="cb-turn-col">
          <div className="cb-turn-botname">
            {showBot && <span className="cb-chat-bot">{task.bot_name ?? "(deleted bot)"}</span>}
            <TaskPill status={task.status} />
            <span className="cb-meta">
              {doneSteps}/{task.plan.length || "?"} steps{runLabel(task)}
            </span>
          </div>

          {task.plan.length === 0 && active && (
            <div className="cb-bubble cb-bubble-bot cb-bubble-live">
              <span className="cb-chat-caret" />
              <span className="cb-bubble-text">
                {task.status === "planning"
                  ? "Planning — choosing the smallest set of steps from the ready tools…"
                  : "Queued — the backend worker will pick this up…"}
              </span>
            </div>
          )}

          {task.plan.map((step) => {
            const isAsk = step.tool === "agent.ask_owner";
            const waitingHere = waiting?.id === step.id;
            return (
              <div key={step.id}
                className={`cb-bubble cb-bubble-bot${step.status === "failed" ? " cb-bubble-err" : ""}${waitingHere ? " cb-bubble-ask" : ""}`}>
                <div className="cb-bubble-head">
                  <span className="cb-step-idx">{step.id + 1}</span>
                  <span className="cb-step-title">{step.title}</span>
                  <span className="cb-step-tool">{step.tool}</span>
                  <StepPill status={step.status} />
                </div>
                {isAsk && waitingHere && step.question && (
                  <div className="cb-bubble-text cb-bubble-question">{step.question}</div>
                )}
                {step.output && !waitingHere && (
                  isAsk
                    ? <div className="cb-bubble-text cb-bubble-answer">{step.output}</div>
                    : <pre className="sb-output">{step.output}</pre>
                )}
                {step.error && <pre className="sb-output sb-output-err">{step.error}</pre>}
                {waitingHere && <ReplyBox task={task} busy={busy} onReply={onReply} />}
              </div>
            );
          })}

          {task.error && task.status !== "completed" && (
            <div className="cb-bubble cb-bubble-bot cb-bubble-warn">
              <div className="cb-bubble-text">{task.error}</div>
            </div>
          )}
          {task.status === "failed" && !task.error && (
            <div className="cb-bubble cb-bubble-bot cb-bubble-warn">
              <div className="cb-bubble-text">The run failed but recorded no error detail.</div>
            </div>
          )}

          {task.status === "waiting_approval" && (
            <div className="cb-bubble cb-bubble-bot cb-bubble-warn">
              <div className="cb-bubble-text">
                Step {task.current_step + 1}{runningStep ? ` (${runningStep.tool})` : ""} needs
                your approval before it may run.
              </div>
              <button type="button" className="cb-chat-link" onClick={onDecideHint}>
                Review in Approvals
              </button>
            </div>
          )}

          {task.status === "completed" && (
            <div className="cb-bubble cb-bubble-bot cb-bubble-result">
              <div className="cb-bubble-head"><span className="cb-bubble-who">Result</span></div>
              {task.result_text
                ? <pre className="cb-chat-out">{task.result_text}</pre>
                : <div className="cb-bubble-text">Completed — the run produced no result text.</div>}
            </div>
          )}
          {task.status === "cancelled" && (
            <div className="cb-bubble cb-bubble-bot">
              <div className="cb-bubble-text">Cancelled — the engine stopped at the next safe boundary.</div>
            </div>
          )}

          {active && !waitingHereActive(task) && (
            <div className="cb-bubble cb-bubble-bot cb-bubble-live">
              <span className="cb-chat-caret" />
              <span className="cb-bubble-text">
                {task.status === "waiting_input"
                  ? "Waiting for your reply above…"
                  : task.status === "waiting_approval"
                    ? "Waiting for your approval…"
                    : task.status === "paused"
                      ? "Paused — no new steps will start."
                      : runningStep
                        ? `Running step ${task.current_step + 1}/${task.plan.length} · ${runningStep.tool}…`
                        : "Running…"}
              </span>
            </div>
          )}

          <div className="cb-turn-actions">
            {(task.status === "blocked" || task.status === "paused") && (
              <button type="button" className="sb-btn sb-btn-primary" disabled={busy}
                onClick={() => onResume(task.id)}
                title="Requeue the task; unknown side-effecting steps are re-planned, never re-run blindly.">
                Resume task
              </button>
            )}
            {active && (
              <button type="button" className="sb-btn" disabled={busy}
                onClick={() => onCancel(task.id)}>
                Cancel task
              </button>
            )}
            {onWorkflow && (
              <button type="button" className="sb-btn sb-btn-ghost" onClick={onWorkflow}
                title="See the step-by-step flow of how this run actually executed.">
                View workflow
              </button>
            )}
            <span className="cb-meta">{task.id}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function waitingHereActive(task: BotTask): boolean {
  return task.status === "waiting_input"
    && task.plan[task.current_step]?.tool === "agent.ask_owner";
}

function TaskThread({ tasks, hue, busy, showBot, empty, onCancel, onResume, onDecideHint, onWorkflow, onReply }: {
  tasks: BotTask[];
  hue: number | ((task: BotTask) => number);
  busy: boolean;
  showBot?: boolean;
  empty: React.ReactNode;
  onCancel: (id: string) => void;
  onResume: (id: string) => void;
  onDecideHint: () => void;
  onWorkflow?: (task: BotTask) => void;
  onReply?: (taskId: string, text: string) => void;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const prevTailRef = useRef("");
  const ordered = useMemo(
    () => [...tasks].reverse().slice(-24), [tasks]);
  const tail = ordered[ordered.length - 1];
  const tailKey = tail
    ? `${tail.id}:${tail.status}:${tail.current_step}:${tail.plan.filter((s) => s.status === "completed").length}`
    : "none";

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const isNewTask = tailKey.split(":", 1)[0] !== prevTailRef.current.split(":", 1)[0];
    prevTailRef.current = tailKey;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (isNewTask || nearBottom) el.scrollTop = el.scrollHeight;
  }, [tailKey]);

  if (tasks.length === 0) {
    return <div className="cb-thread cb-thread-empty">{empty}</div>;
  }
  return (
    <div className="cb-thread" ref={scrollRef}>
      {tasks.length > 24 && (
        <p className="cb-thread-more">
          … {tasks.length - 24} older task(s) not shown here — open History for the full log.
        </p>
      )}
      {ordered.map((task) => (
        <TaskTurn key={task.id} task={task} busy={busy} showBot={showBot}
          hue={typeof hue === "function" ? hue(task) : hue}
          onCancel={onCancel} onResume={onResume} onDecideHint={onDecideHint}
          onWorkflow={onWorkflow ? () => onWorkflow(task) : undefined}
          onReply={onReply} />
      ))}
    </div>
  );
}

/* ── workflow view — how the bot really executed a run ───────────────────── */

function flowEndNote(task: BotTask): string {
  if (task.status === "queued" || task.status === "planning" || task.status === "running") {
    return "Still running — this flow updates live while the worker proceeds.";
  }
  if (task.status === "waiting_approval") {
    return "Paused — a step is waiting for the owner's decision before it may run.";
  }
  if (task.status === "blocked") {
    return "Blocked — see the step above for the real blocker; resuming re-plans unknown side effects.";
  }
  if (task.status === "cancelled") {
    return "Cancelled — the engine stopped at the next safe boundary.";
  }
  return "Completed with no result text recorded.";
}

function WorkflowFlow({ task, planMode }: { task: BotTask; planMode?: string }) {
  const doneSteps = task.plan.filter((s) => s.status === "completed").length;
  const failedSteps = task.plan.filter((s) => s.status === "failed").length;
  const gated = task.plan.filter((s) => s.approval_id || s.force_confirm).length;

  if (!task.plan.length) {
    const why = task.status === "queued"
      ? "the worker will plan it when the task starts"
      : task.status === "planning"
        ? "the live LLM is planning the steps right now"
        : task.status === "failed"
          ? (task.error || "the run failed before a plan was recorded")
          : "no plan has been recorded for this task";
    return (
      <div className="cb-flow-empty">
        Workflow not available yet — {why}. This page refreshes every few seconds.
      </div>
    );
  }

  return (
    <div className="cb-flow">
      <div className="cb-flow-kpis">
        <span className="cb-meta">
          plan: {planMode === "direct" ? "deterministic direct call" : "LLM-planned"} · {task.plan.length} step(s)
        </span>
        <span className="cb-meta">
          {doneSteps}/{task.plan.length} completed{failedSteps ? ` · ${failedSteps} failed` : ""}
        </span>
        {gated > 0 && <span className="cb-meta cb-warn">{gated} step(s) owner-approved</span>}
        <span className="cb-meta">took {totalDur(task)}</span>
      </div>

      <div className="cb-flow-node cb-flow-start">
        <span className="cb-flow-badge">START</span>
        <span className="cb-flow-text">
          {task.kind === "schedule" ? "A scheduled tick submitted this goal" : "You submitted this goal"}
        </span>
        <span className="cb-meta">{fmtWhen(task.created_at)}</span>
      </div>

      {task.plan.map((step, i) => {
        const prev = task.plan[i - 1];
        const usesPrev = prev ? step.depends_on.includes(prev.id) : false;
        return (
          <React.Fragment key={step.id}>
            <div className="cb-flow-arrow" aria-hidden="true">
              ↓{usesPrev ? <span className="cb-flow-arrow-label">needs step {prev!.id + 1}</span> : null}
            </div>
            <div className={`cb-flow-node cb-flow-step cb-flow-${step.status}`}>
              <div className="cb-flow-head">
                <span className="cb-flow-idx">{step.id + 1}</span>
                <span className="cb-flow-title">{step.title}</span>
                <span className="cb-step-tool">{step.tool}</span>
                <StepPill status={step.status} />
              </div>
              <div className="cb-flow-meta">
                {step.depends_on.length > 0 && (
                  <span className="cb-meta">
                    starts after {step.depends_on.map((d) => `step ${d + 1}`).join(", ")}
                  </span>
                )}
                {step.approval_id && (
                  <span className="cb-meta cb-warn">owner approval {step.approval_id}</span>
                )}
                {step.force_confirm && (
                  <span className="cb-meta cb-warn">replayed with explicit confirm</span>
                )}
                {step.run_id && <span className="cb-meta">runner run {step.run_id}</span>}
                <span className="cb-meta">
                  {step.finished_at
                    ? `took ${fmtDuration(step.started_at, step.finished_at)}`
                    : step.started_at ? "running…" : "pending"}
                </span>
              </div>
              {step.params && Object.keys(step.params).length > 0 && (
                <details className="cb-flow-details">
                  <summary>tool call params</summary>
                  <pre className="cb-params">{JSON.stringify(step.params, null, 1)}</pre>
                </details>
              )}
              {step.output && <pre className="sb-output">{clampText(step.output)}</pre>}
              {step.error && <pre className="sb-output sb-output-err">{clampText(step.error)}</pre>}
            </div>
          </React.Fragment>
        );
      })}

      <div className="cb-flow-arrow" aria-hidden="true">↓</div>
      <div className={`cb-flow-node cb-flow-end cb-flow-${task.status}`}>
        <span className="cb-flow-badge">
          {(TASK_META[task.status] ?? { label: task.status }).label.toUpperCase()}
        </span>
        {task.result_text ? (
          <pre className="sb-output">{task.result_text}</pre>
        ) : task.error ? (
          <pre className="sb-output sb-output-err">{task.error}</pre>
        ) : (
          <span className="cb-flow-text">{flowEndNote(task)}</span>
        )}
      </div>

      {task.events?.length > 0 && (
        <div className="cb-task-events">
          {task.events.slice(-20).map((ev, i) => (
            <span key={i} className="sb-run-event" title={ev.detail ?? ""}>
              {ev.event}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── the bot's own browser (backend-owned, persistent per-bot profile) ───── */

function BrowserPanel({ botId }: { botId: string }) {
  const [info, setInfo] = useState<BrowserInfo | null>(null);
  const [err, setErr] = useState("");
  const [localBusy, setLocalBusy] = useState(false);

  const load = useCallback(async (withShot: boolean) => {
    try {
      const res = await api<BrowserInfo>(
        `/custom/${botId}/browser${withShot ? "?shot=1" : ""}`);
      setInfo((prev) => {
        if (!withShot && !res.png_b64 && prev?.png_b64) {
          return { ...res, png_b64: prev.png_b64, shot_at: prev.shot_at };
        }
        return res;
      });
      setErr("");
    } catch (exc) {
      setErr(exc instanceof Error ? exc.message : String(exc));
    }
  }, [botId]);

  useEffect(() => {
    let alive = true;
    queueMicrotask(() => {
      if (alive) void load(false);
    });
    const timer = setInterval(() => {
      if (alive && !document.hidden) void load(false);
    }, 6000);
    return () => { alive = false; clearInterval(timer); };
  }, [load]);

  const snapshot = useCallback(async () => {
    setLocalBusy(true);
    await load(true);
    setLocalBusy(false);
  }, [load]);

  const closeSession = useCallback(async () => {
    setLocalBusy(true);
    try {
      await post(`/custom/${botId}/browser/close`);
      await load(false);
    } catch (exc) {
      setErr(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLocalBusy(false);
    }
  }, [botId, load]);

  return (
    <div className="cb-panel">
      <p className="sb-detail-note">
        This bot owns its browser: a real Chromium session the backend keeps
        for it, with its own saved profile — sign into a site once in that
        window and the bot stays signed in on future runs. Closing the window
        is harmless (the next action reopens the same profile); closing this
        web page changes nothing.
      </p>

      {err && <pre className="sb-output sb-output-err">{err}</pre>}

      <div className="cb-browser-status">
        <StatusPill
          label={info ? (info.alive ? "Session live" : "No live session") : "Checking…"}
          color={info?.alive ? "#34d399" : "#94a3b8"} />
        <span className="cb-meta">mode: {info?.mode ?? "—"}</span>
        {info?.created_at && (
          <span className="cb-meta">up since {fmtWhen(info.created_at)}</span>
        )}
        {info?.alive && info.url && (
          <span className="cb-meta cb-browser-url" title={info.url}>
            {info.title ? `${info.title} — ` : ""}{info.url}
          </span>
        )}
      </div>

      <div className="cb-task-actions">
        <button type="button" className="sb-btn" disabled={localBusy}
          onClick={() => void snapshot()}>
          Refresh view
        </button>
        {info?.alive && (
          <button type="button" className="sb-btn" disabled={localBusy}
            onClick={() => void closeSession()}>
            Close session
          </button>
        )}
        <span className="cb-meta">profile: {info?.profile ?? "…"}</span>
      </div>

      {info?.shot_error && (
        <span className="cb-meta cb-warn">snapshot unavailable: {info.shot_error}</span>
      )}
      {info?.png_b64 ? (
        // eslint-disable-next-line @next/next/no-img-element -- data: URL straight from the bot's backend session; next/image cannot optimize data URLs
        <img className="cb-browser-shot" alt="The bot's current browser view"
          src={`data:image/png;base64,${info.png_b64}`} />
      ) : (
        <p className="sb-detail-note">
          No snapshot yet — press “Refresh view”, or give the bot a task with
          a browser step and its live view appears here.
        </p>
      )}
    </div>
  );
}

/* ── the main component ──────────────────────────────────────────────────── */

export default function CustomBots({ initialBotId }: { initialBotId?: string } = {}) {
  const router = useRouter();
  const [bots, setBots] = useState<CustomBot[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [engine, setEngine] = useState<EngineStatus | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRecord[]>([]);
  const [allTasks, setAllTasks] = useState<BotTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState<{ kind: "info" | "error" | "ok"; text: string } | null>(null);
  const [tab, setTab] = useState<"bots" | "approvals" | "tasks" | "history">("bots");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CustomBot | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [detailTab, setDetailTab] = useState<"tasks" | "history" | "workflow" | "browser" | "memory" | "schedule" | "settings">("tasks");
  const [goalText, setGoalText] = useState("");
  const [taskFilterBot, setTaskFilterBot] = useState("");
  const [histFilterBot, setHistFilterBot] = useState("");
  const [histFilterStatus, setHistFilterStatus] = useState("");
  const [histFilterKind, setHistFilterKind] = useState("");
  const [histOpenId, setHistOpenId] = useState<string | null>(null);
  const [wfTaskId, setWfTaskId] = useState<string | null>(null);
  const [memoryKind, setMemoryKind] = useState("fact");
  const [memoryText, setMemoryText] = useState("");
  const [schedInterval, setSchedInterval] = useState(60);
  const [schedGoal, setSchedGoal] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [busy, setBusy] = useState(false);
  const [wizard, setWizard] = useState<WizardState | null>(null);

  const busyRef = useRef(false);
  const selectedRef = useRef<string | null>(null);
  const submitKeyRef = useRef<string | null>(null);
  selectedRef.current = selectedId;

  const say = useCallback((kind: "info" | "error" | "ok", text: string) => {
    setNotice({ kind, text });
  }, []);

  /* ── data loading ──────────────────────────────────────────────────────── */

  const refreshAll = useCallback(async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    try {
      const [customBody, approvalBody, toolBody] = await Promise.all([
        api<{ bots: CustomBot[]; engine: EngineStatus }>("/custom"),
        api<{ approvals: ApprovalRecord[] }>("/approvals?status=pending"),
        api<{ tools: ToolInfo[] }>("/tools"),
      ]);
      setBots(customBody.bots ?? []);
      setEngine(customBody.engine ?? null);
      setApprovals(approvalBody.approvals ?? []);
      setTools(toolBody.tools ?? []);
      setLoadError("");
      try {
        const taskBody = await api<{ tasks: BotTask[] }>("/tasks?limit=100");
        setAllTasks(taskBody.tasks ?? []);
      } catch { /* tasks view degrades, bots stay usable */ }
      const sid = selectedRef.current;
      if (sid && CUSTOM_ID_RE.test(sid)) {
        try {
          const d = await api<CustomBot>(`/custom/${sid}`);
          setDetail(d);
          if (d.status === "active" || d.status === "paused") {
            const r = await api<Readiness>(`/custom/${sid}/readiness`);
            setReadiness(r);
          }
        } catch {
          /* bot may have been deleted elsewhere — the next list refresh clears it */
        }
      }
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
      busyRef.current = false;
    }
  }, []);

  useEffect(() => {
    let alive = true;
    void refreshAll();
    const id = setInterval(() => {
      if (alive && !document.hidden) void refreshAll();
    }, 4000);
    return () => { alive = false; clearInterval(id); };
  }, [refreshAll]);

  const selectBot = useCallback((botId: string) => {
    setSelectedId(botId);
    setDetail(null);
    setReadiness(null);
    setDetailTab("tasks");
    setConfirmDelete(false);
    setConfirmClear(false);
    setGoalText("");
    void refreshAll();
  }, [refreshAll]);

  // When opened as a dedicated page (/hub/sara-bots/[id]), auto-select that bot.
  useEffect(() => {
    if (!initialBotId) return;
    if (!CUSTOM_ID_RE.test(initialBotId)) {
      // Desktop/local bots ("action.system_diagnostics") live in their own
      // workspace, not in the custom-bot store — and polling /custom/<id> for
      // one is a guaranteed 422 every few seconds. Say so plainly instead.
      say("info",
        `“${initialBotId}” is not a custom bot — it runs from its own workspace. ` +
        `This page manages custom bots (ids like bot_ab12cd34); the bot registry lists them all.`);
      return;
    }
    selectBot(initialBotId);
    // Also fetch the full bot detail directly for the dedicated page
    api<CustomBot>(`/custom/${initialBotId}`)
      .then((d) => {
        setDetail(d);
      })
      .catch((err) => console.error('[CustomBots] detail fetch failed:', err));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialBotId]);

  /* ── bot actions ───────────────────────────────────────────────────────── */

  const withBusy = useCallback(async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      say("error", err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [say]);

  const pauseResume = useCallback((bot: CustomBot) => withBusy(async () => {
    const action = bot.status === "active" ? "pause" : "resume";
    await post(`/custom/${bot.id}/${action}`);
    say("ok", action === "pause"
      ? `${bot.name} paused — queued tasks wait; the worker will not pick up new ones.`
      : `${bot.name} resumed.`);
    await refreshAll();
  }), [withBusy, say, refreshAll]);

  const duplicateBot = useCallback((bot: CustomBot) => withBusy(async () => {
    const clone = await post<CustomBot>(`/custom/${bot.id}/duplicate`, { name: "" });
    say("ok", `Created “${clone.name}” (memory is not copied).`);
    await refreshAll();
    selectBot(clone.id);
  }), [withBusy, say, refreshAll, selectBot]);

  const removeBot = useCallback((bot: CustomBot) => withBusy(async () => {
    const res = await api<{ cancelled_tasks: number; deleted_schedules: number }>(
      `/custom/${bot.id}`, { method: "DELETE" });
    say("info", `Deleted ${bot.name} — cancelled ${res.cancelled_tasks} task(s), removed ${res.deleted_schedules} schedule(s).`);
    setSelectedId(null);
    setDetail(null);
    setReadiness(null);
    await refreshAll();
  }), [withBusy, say, refreshAll]);

  const submitGoal = useCallback((bot: CustomBot) => withBusy(async () => {
    const goal = goalText.trim();
    if (!goal) return;
    if (!submitKeyRef.current) submitKeyRef.current = makeKey();
    const task = await post<BotTask>(`/custom/${bot.id}/tasks`, {
      goal, idempotency_key: submitKeyRef.current,
    });
    submitKeyRef.current = null;
    setGoalText("");
    say("ok", task.deduplicated
      ? `Already submitted — this is the existing task ${task.id}.`
      : `Task ${task.id} queued. It runs in the backend worker even if you close this page.`);
    await refreshAll();
  }), [goalText, withBusy, say, refreshAll]);

  const cancelTask = useCallback((taskId: string) => withBusy(async () => {
    await post(`/tasks/${taskId}/cancel`);
    say("info", "Cancellation recorded — the engine stops at the next safe boundary.");
    await refreshAll();
  }), [withBusy, say, refreshAll]);

  const resumeTask = useCallback((taskId: string) => withBusy(async () => {
    await post(`/tasks/${taskId}/resume`);
    say("ok", "Task requeued. Steps whose side effects are unknown will be re-planned, never re-run blindly.");
    await refreshAll();
  }), [withBusy, say, refreshAll]);

  const replyTask = useCallback((taskId: string, text: string) => withBusy(async () => {
    await post(`/tasks/${taskId}/reply`, { text });
    say("ok", "Reply delivered — the bot continues from exactly where it paused.");
    await refreshAll();
  }), [withBusy, say, refreshAll]);

  const decide = useCallback((approval: ApprovalRecord, decision: "approve" | "deny") =>
    withBusy(async () => {
      await post(`/approvals/${approval.id}/decide`, { decision });
      say(decision === "approve" ? "ok" : "info",
        decision === "approve"
          ? `Approved “${approval.tool}” for ${approval.bot_name} — the task resumes with an explicit confirm on that step.`
          : `Denied “${approval.tool}” — the task continues without it and records the denial.`);
      await refreshAll();
    }), [withBusy, say, refreshAll]);

  const openWorkflow = useCallback((task: BotTask) => {
    selectedRef.current = task.dot_id;
    setSelectedId(task.dot_id);
    setDetail(null);
    setConfirmDelete(false);
    setConfirmClear(false);
    setWfTaskId(task.id);
    setDetailTab("workflow");
    setTab("bots");
    void refreshAll();
  }, [refreshAll]);

  const addMemory = useCallback((bot: CustomBot) => withBusy(async () => {
    const text = memoryText.trim();
    if (!text) return;
    await post(`/custom/${bot.id}/memory`, { kind: memoryKind, text });
    setMemoryText("");
    say("ok", "Note saved to this bot's private memory.");
    await refreshAll();
  }), [memoryText, memoryKind, withBusy, say, refreshAll]);

  const deleteMemory = useCallback((bot: CustomBot, memId: string) => withBusy(async () => {
    await api(`/custom/${bot.id}/memory/${memId}`, { method: "DELETE" });
    await refreshAll();
  }), [withBusy, refreshAll]);

  const clearMemory = useCallback((bot: CustomBot) => withBusy(async () => {
    await post(`/custom/${bot.id}/memory/clear`);
    setConfirmClear(false);
    say("info", `Memory wiped for ${bot.name}.`);
    await refreshAll();
  }), [withBusy, say, refreshAll]);

  const createSchedule = useCallback((bot: CustomBot) => withBusy(async () => {
    const goal = (schedGoal.trim() || bot.goal || bot.description).trim();
    if (!goal) { say("error", "A schedule needs a goal."); return; }
    await post("/schedules", {
      dot_id: bot.id, goal, interval_minutes: Number(schedInterval) || 60,
      enabled: true,
    });
    setSchedGoal("");
    say("ok", `Schedule created — every ${schedInterval} minute(s).`);
    await refreshAll();
  }), [schedGoal, schedInterval, withBusy, say, refreshAll]);

  const toggleSchedule = useCallback((scheduleId: string) => withBusy(async () => {
    await post(`/schedules/${scheduleId}/toggle`);
    await refreshAll();
  }), [withBusy, refreshAll]);

  const deleteSchedule = useCallback((scheduleId: string) => withBusy(async () => {
    await api(`/schedules/${scheduleId}`, { method: "DELETE" });
    await refreshAll();
  }), [withBusy, refreshAll]);

  /* ── wizard ────────────────────────────────────────────────────────────── */

  const emptyDraft = (): WizardDraft => ({
    name: "", description: "", goal: "", instructions: "",
    category: "custom", autonomy: "supervised", plan_mode: "auto",
    direct_tool: "", direct_params_text: "", output_format: "",
    memory_enabled: true, tools: [], skills: [], hue: 200,
  });

  const openWizard = useCallback(() => {
    setWizard({
      step: 1, editing: null, description: "", proposing: false,
      hueTouched: false,
      draft: emptyDraft(), notes: null, scheduleOn: false,
      scheduleInterval: 60, scheduleGoal: "", error: "", saving: false,
    });
  }, []);

  const openEdit = useCallback((bot: CustomBot) => {
    setWizard({
      step: 2, editing: bot.id, description: bot.description, proposing: false,
      hueTouched: true,
      draft: {
        name: bot.name, description: bot.description, goal: bot.goal,
        instructions: bot.instructions, category: bot.category,
        autonomy: bot.autonomy, plan_mode: bot.plan_mode,
        direct_tool: bot.direct_tool,
        direct_params_text: bot.direct_params && Object.keys(bot.direct_params).length
          ? JSON.stringify(bot.direct_params, null, 2) : "",
        output_format: bot.output_format,
        memory_enabled: bot.memory_enabled, tools: [...bot.tools],
         skills: [...(bot.skills ?? [])],
        hue: bot.hue ?? 200,
      },
      notes: null, scheduleOn: false, scheduleInterval: 60, scheduleGoal: "",
      error: "", saving: false,
    });
  }, []);

  const wizardSteps = wizard?.editing ? [2, 3, 5] : [1, 2, 3, 4, 5];
  const wizardPos = wizard ? wizardSteps.indexOf(wizard.step) : 0;

  const setDraft = useCallback((fields: Partial<WizardDraft>) => {
    setWizard((w) => w ? { ...w, draft: { ...w.draft, ...fields }, error: "" } : w);
  }, []);

  const pickHue = useCallback((hue: number) => {
    setWizard((w) => w ? {
      ...w, hueTouched: true, error: "",
      draft: { ...w.draft, hue: Math.max(0, Math.min(360, Math.round(hue))) },
    } : w);
  }, []);

  const propose = useCallback(async () => {
    if (!wizard) return;
    const description = wizard.description.trim();
    if (description.length < 3) {
      setWizard((w) => w ? { ...w, error: "Describe the bot in a few more words." } : w);
      return;
    }
    setWizard((w) => w ? { ...w, proposing: true, error: "" } : w);
    try {
      const body = await post<{ ok: boolean; proposal: Proposal; ready_tools: number }>(
        "/factory/propose", { description });
      const p = body.proposal;
      setWizard((w) => w ? {
        ...w, proposing: false, step: 2,
        draft: {
          name: p.name, description: p.description, goal: p.goal,
          instructions: p.instructions, category: p.category,
          autonomy: p.autonomy, plan_mode: p.plan_mode,
          direct_tool: p.direct_tool || "",
          direct_params_text: p.direct_params && Object.keys(p.direct_params).length
            ? JSON.stringify(p.direct_params, null, 2) : "",
          output_format: p.output_format,
          memory_enabled: p.memory_enabled, tools: p.tools,
           skills: p.skills ?? [],
          hue: hueForName(p.name),
        },
        notes: { missing: p.missing ?? [], dropped: p.dropped ?? [] },
        scheduleOn: Boolean(p.schedule),
        scheduleInterval: p.schedule?.interval_minutes ?? 60,
        scheduleGoal: p.schedule?.goal ?? p.goal,
      } : w);
    } catch (err) {
      setWizard((w) => w ? {
        ...w, proposing: false,
        error: err instanceof Error ? err.message : String(err),
      } : w);
    }
  }, [wizard]);

  const wizardCanContinue = useMemo(() => {
    if (!wizard) return false;
    const d = wizard.draft;
    switch (wizard.step) {
      case 1: return wizard.description.trim().length >= 3;
      case 2: return d.tools.length >= 1;
      case 3: {
        if (d.name.trim().length < 2) return false;
        if (d.plan_mode === "direct") {
          if (!d.direct_tool || !d.tools.includes(d.direct_tool)) return false;
          if (d.direct_params_text.trim()) {
            try { JSON.parse(d.direct_params_text); } catch { return false; }
          }
        }
        return true;
      }
      case 4:
        return !wizard.scheduleOn || (
          Number(wizard.scheduleInterval) >= 5 &&
          Number(wizard.scheduleInterval) <= 10080 &&
          (wizard.scheduleGoal.trim() || d.goal.trim()).length > 0);
      default:
        return true;
    }
  }, [wizard]);

  const saveWizard = useCallback(async () => {
    if (!wizard) return;
    const d = wizard.draft;
    let directParams: Record<string, unknown> = {};
    if (d.plan_mode === "direct" && d.direct_params_text.trim()) {
      try {
        const parsed = JSON.parse(d.direct_params_text);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          directParams = parsed as Record<string, unknown>;
        } else {
          setWizard((w) => w ? { ...w, error: "Direct params must be a JSON object." } : w);
          return;
        }
      } catch {
        setWizard((w) => w ? { ...w, error: "Direct params are not valid JSON." } : w);
        return;
      }
    }
    const payload = {
      name: d.name, description: d.description, goal: d.goal,
      instructions: d.instructions, category: d.category,
      autonomy: d.autonomy, plan_mode: d.plan_mode,
      direct_tool: d.plan_mode === "direct" ? d.direct_tool : "",
      direct_params: d.plan_mode === "direct" ? directParams : {},
      output_format: d.output_format,
      memory_enabled: d.memory_enabled,
      tools: d.tools,
       skills: d.skills,
      hue: d.hue,
    };
    setWizard((w) => w ? { ...w, saving: true, error: "" } : w);
    try {
      if (wizard.editing) {
        const updated = await patch<CustomBot>(`/custom/${wizard.editing}`, payload);
        say("ok", `${updated.name} updated.`);
        setWizard(null);
        await refreshAll();
      } else {
        const bot = await post<CustomBot>("/custom", payload);
        if (wizard.scheduleOn) {
          const goal = (wizard.scheduleGoal.trim() || d.goal).trim();
          if (goal) {
            try {
              await post("/schedules", {
                dot_id: bot.id, goal,
                interval_minutes: Number(wizard.scheduleInterval) || 60,
                enabled: true,
              });
            } catch (err) {
              say("error", `Bot created, but the schedule failed: ${err instanceof Error ? err.message : String(err)}. Add it from the bot's Schedules tab.`);
            }
          }
        }
        say("ok", `${bot.name} created — give it a goal from its detail panel.`);
        setWizard(null);
        await refreshAll();
        selectBot(bot.id);
      }
    } catch (err) {
      setWizard((w) => w ? {
        ...w, saving: false,
        error: err instanceof Error ? err.message : String(err),
      } : w);
    }
  }, [wizard, say, refreshAll, selectBot]);

  /* ── derived ───────────────────────────────────────────────────────────── */

  const selected = useMemo(() => {
    const fromList = bots.find((b) => b.id === selectedId) ?? null;
    if (detail && detail.id === selectedId) {
      // the list entry lacks tasks / memory / schedules — the detail carries them
      return fromList ? { ...fromList, ...detail } : detail;
    }
    return fromList ?? detail;
  }, [bots, selectedId, detail]);

  const activeTaskCount = useMemo(
    () => allTasks.filter((t) => ACTIVE_TASK_STATES.has(t.status)).length,
    [allTasks]);

  const filteredTasks = useMemo(() => {
    if (!taskFilterBot) return allTasks;
    return allTasks.filter((t) => t.dot_id === taskFilterBot);
  }, [allTasks, taskFilterBot]);

  const botById = useMemo(() => new Map(bots.map((b) => [b.id, b])), [bots]);

  const historyTasks = useMemo(() => {
    let list = allTasks;
    if (histFilterBot) list = list.filter((t) => t.dot_id === histFilterBot);
    if (histFilterStatus === "active") {
      list = list.filter((t) => ACTIVE_TASK_STATES.has(t.status));
    } else if (histFilterStatus) {
      list = list.filter((t) => t.status === histFilterStatus);
    }
    if (histFilterKind) list = list.filter((t) => t.kind === histFilterKind);
    return list;
  }, [allTasks, histFilterBot, histFilterStatus, histFilterKind]);

  const historyGroups = useMemo(() => {
    const groups: { label: string; tasks: BotTask[] }[] = [];
    for (const task of historyTasks) {
      const label = dayLabel(task.created_at);
      const last = groups[groups.length - 1];
      if (last && last.label === label) last.tasks.push(task);
      else groups.push({ label, tasks: [task] });
    }
    return groups;
  }, [historyTasks]);

  // per-bot chat log for the selected bot's detail panel
  const detailHistoryGroups = useMemo(() => {
    const raw = selected?.tasks ?? [];
    const list = raw
      .map((t) => allTasks.find((x) => x.id === t.id) ?? t)
      .sort((a, b) => b.created_at - a.created_at);
    const groups: { label: string; tasks: BotTask[] }[] = [];
    for (const task of list) {
      const label = dayLabel(task.created_at);
      const last = groups[groups.length - 1];
      if (last && last.label === label) last.tasks.push(task);
      else groups.push({ label, tasks: [task] });
    }
    return groups;
  }, [selected, allTasks]);

  // the run shown in the Workflow tab (defaults to an active run, else the newest)
  const wfTask = useMemo(() => {
    const list = selected?.tasks ?? [];
    if (list.length === 0) return null;
    const explicit = wfTaskId ? list.find((t) => t.id === wfTaskId) : null;
    const pick = explicit ?? list.find((t) => ACTIVE_TASK_STATES.has(t.status)) ?? list[0];
    return allTasks.find((x) => x.id === pick.id) ?? pick;
  }, [selected, allTasks, wfTaskId]);

  /* ── render ────────────────────────────────────────────────────────────── */

  return (
    <div className="cb-root">
      <style>{CB_CSS}</style>

      {notice && (
        <div className={`sb-banner sb-banner-${notice.kind}`} role="status">
          {notice.text}
          <button className="sb-banner-x" aria-label="Dismiss" onClick={() => setNotice(null)}>×</button>
        </div>
      )}
      {loadError && (
        <div className="sb-banner sb-banner-error" role="alert">
          Backend unreachable: {loadError} — start it with <code>python run_backend.py</code>.
        </div>
      )}

      <section className="sb-toolbar">
        <div className="sb-counts">
          {engine && (
            <span className="cb-engine" title={engine.worker}>
              <span className="cb-engine-dot"
                style={{ background: engine.engine_running ? "#34d399" : "#f87171" }} />
              worker {engine.engine_running ? "live" : "stopped"}
              {engine.current_task_id ? ` · running ${engine.current_task_id}` : ""}
              {` · queued ${engine.queued}`}
              {` · LLM ${engine.llm_ready ? engine.llm_live ?? "ready" : "unavailable"}`}
            </span>
          )}
          <span className="sb-count-total">{bots.length} bots · {activeTaskCount} active tasks</span>
        </div>
        <div className="sb-toolbar-right">
          <div className="sb-kind-switch" role="tablist" aria-label="My Bots section">
            <button role="tab" aria-selected={tab === "bots"}
              className={`sb-kind-btn ${tab === "bots" ? "sb-kind-on" : ""}`}
              onClick={() => setTab("bots")}>
              My Bots
            </button>
            <button role="tab" aria-selected={tab === "approvals"}
              className={`sb-kind-btn ${tab === "approvals" ? "sb-kind-on" : ""}`}
              onClick={() => setTab("approvals")}>
              Approvals{approvals.length ? ` (${approvals.length})` : ""}
            </button>
            <button role="tab" aria-selected={tab === "tasks"}
              className={`sb-kind-btn ${tab === "tasks" ? "sb-kind-on" : ""}`}
              onClick={() => setTab("tasks")}>
              Tasks
            </button>
            <button role="tab" aria-selected={tab === "history"}
              className={`sb-kind-btn ${tab === "history" ? "sb-kind-on" : ""}`}
              onClick={() => setTab("history")}>
              History
            </button>
          </div>
          <a className="sb-btn sb-btn-ghost" href="/hub/sara-bots/skills">Skills →</a>
          <button className="sb-btn sb-btn-primary" onClick={openWizard}>New bot</button>
        </div>
      </section>

      {/* ── bots view ── */}
      {tab === "bots" && (
        <>
          {loading && bots.length === 0 ? (
            <div className="sb-empty">Loading your bots…</div>
          ) : bots.length === 0 ? (
            <div className="sb-empty">
              No custom bots yet. Create one with “New bot” —
              or describe what you need and the factory will design it from real tools.
              <div className="cb-mt">
                <button className="sb-btn sb-btn-primary" onClick={openWizard}>Create your first bot</button>
              </div>
            </div>
          ) : (!initialBotId ? (
            <section className="sb-grid" aria-label="Custom bots">
              {bots.map((bot) => (
                <button key={bot.id}
                  className={`sb-card ${selectedId === bot.id ? "sb-card-on" : ""}`}
                  onClick={() => router.push(`/hub/sara-bots/${bot.id}`)}
                  aria-pressed={selectedId === bot.id}>
                  <div className="sb-card-orb">
                    <span className="cb-dot" style={{
                      background: `radial-gradient(circle at 32% 28%, hsla(${bot.hue},95%,78%,.95), hsla(${bot.hue},85%,45%,.95) 55%, hsla(${bot.hue + 30},80%,20%,.98))`,
                      boxShadow: `0 0 22px hsla(${bot.hue},90%,60%,.45)`,
                    }} />
                  </div>
                  <div className="sb-card-body">
                    <div className="sb-card-name-row">
                      <span className="sb-card-name">{bot.name}</span>
                      <span className="sb-kind-tag">{bot.category.toUpperCase()}</span>
                    </div>
                    <div className="sb-card-desc">{bot.description || bot.goal}</div>
                    <div className="sb-card-foot">
                      <BotStatusPill status={bot.status} />
                      <span className="cb-meta">{bot.autonomy}</span>
                      <span className="cb-meta">{bot.ready_tools}/{bot.tool_count} tools ready</span>
                      {bot.active_tasks ? (
                        <span className="cb-meta cb-warn">{bot.active_tasks} active</span>
                      ) : null}
                      {bot.last_task && !bot.active_tasks ? (
                        <span className="cb-meta">last: {bot.last_task.status}</span>
                      ) : null}
                    </div>
                  </div>
                </button>
              ))}
            </section>
          ) : null)}

          {selected && (
            <section className="sb-detail" aria-label={`Details for ${selected.name}`}>
              <div className="sb-detail-head">
                <span className="cb-dot cb-dot-lg" style={{
                  background: `radial-gradient(circle at 32% 28%, hsla(${selected.hue},95%,78%,.95), hsla(${selected.hue},85%,45%,.95) 55%, hsla(${selected.hue + 30},80%,20%,.98))`,
                  boxShadow: `0 0 26px hsla(${selected.hue},90%,60%,.5)`,
                }} />
                <div className="sb-detail-title">
                  <h2>{selected.name}</h2>
                  <div className="sb-detail-meta">
                    <button disabled={busy} onClick={() => pauseResume(selected)}
                      title={selected.status === "active" ? "Click to pause this bot" : "Click to resume this bot"}
                      style={{
                        display: "inline-flex", alignItems: "center", gap: 6,
                        padding: "5px 14px", borderRadius: 999, border: "1px solid",
                        borderColor: selected.status === "active" ? "rgba(52,211,153,0.5)" : "rgba(251,191,36,0.5)",
                        background: selected.status === "active" ? "rgba(52,211,153,0.12)" : "rgba(251,191,36,0.12)",
                        color: selected.status === "active" ? "#34d399" : "#fbbf24",
                        fontSize: 11, fontWeight: 700, cursor: busy ? "not-allowed" : "pointer",
                        transition: "all .15s", opacity: busy ? 0.5 : 1,
                      }}>
                      <span style={{ width: 7, height: 7, borderRadius: "50%",
                        background: selected.status === "active" ? "#34d399" : "#fbbf24",
                        boxShadow: selected.status === "active" ? "0 0 6px #34d399" : "0 0 6px #fbbf24" }} />
                      {selected.status === "active" ? "Active — click to pause" : "Paused — click to resume"}
                    </button>
                    <span className="sb-kind-tag">{selected.category.toUpperCase()}</span>
                    <span className="sb-detail-cat">{selected.autonomy} · {selected.plan_mode === "direct" ? `direct: ${selected.direct_tool}` : "LLM planning"}{selected.memory_enabled ? " · memory on" : " · memory off"}</span>
                  </div>
                </div>
                <div className="sb-detail-actions">
                  <button className="sb-btn" disabled={busy} onClick={() => duplicateBot(selected)}>Duplicate</button>
                  <button className="sb-btn sb-btn-ghost" disabled={busy} onClick={() => openEdit(selected)}>
                    Edit configuration
                  </button>
                  <button className={`sb-btn ${confirmDelete ? "sb-btn-danger" : ""}`}
                    disabled={busy}
                    onClick={() => {
                      if (!confirmDelete) { setConfirmDelete(true); return; }
                      void removeBot(selected);
                    }}>
                    {confirmDelete ? "Click again to delete" : "Delete"}
                  </button>
                </div>
              </div>

              <p className="sb-detail-desc">{selected.description || selected.goal}</p>

              <div className="cb-stat-row">
                <div className="cb-stat">
                  <span className="cb-stat-v">{selected.ready_tools}</span>
                  <span className="cb-stat-k">/{selected.tool_count} tools ready</span>
                </div>
                <div className="cb-stat">
                  <span className="cb-stat-v">{selected.tasks?.length ?? 0}</span>
                  <span className="cb-stat-k">tasks</span>
                </div>
                <div className="cb-stat">
                  <span className="cb-stat-v">{selected.schedules?.length ?? 0}</span>
                  <span className="cb-stat-k">schedules</span>
                </div>
                <div className="cb-stat">
                  <span className="cb-stat-v">{selected.memory?.length ?? 0}</span>
                  <span className="cb-stat-k">memories</span>
                </div>
              </div>

              {(selected.goal || selected.output_format) && (
                <div className="cb-goal-card">
                  <div className="cb-goal-row">
                    <span className="cb-goal-k">Default goal</span>
                    <span className="cb-goal-v">{selected.goal || "No default goal set."}</span>
                  </div>
                  {selected.output_format && (
                    <div className="cb-goal-row">
                      <span className="cb-goal-k">Expected output</span>
                      <span className="cb-goal-v">{selected.output_format}</span>
                    </div>
                  )}
                </div>
              )}

              {readiness && !readiness.runnable && (
                <div className="cb-ready cb-ready-blocked">
                  <strong>Not runnable right now:</strong>
                  <ul>{readiness.blockers.map((b, i) => <li key={i}>{b.detail}</li>)}</ul>
                  {readiness.hints.length > 0 && (
                    <details>
                      <summary>{readiness.hints.length} thing(s) you can fix</summary>
                      <ul>{readiness.hints.map((h, i) => <li key={i}>{h}</li>)}</ul>
                    </details>
                  )}
                </div>
              )}

              <div className="cb-detail-grid">
                <nav className="cb-detail-nav" role="tablist" aria-label="Bot detail">
                  {(["tasks", "history", "workflow", "browser", "memory", "schedule", "settings"] as const).map((t) => {
                    const count = t === "tasks" || t === "history" ? selected.tasks?.length
                      : t === "memory" ? selected.memory?.length
                      : t === "schedule" ? selected.schedules?.length
                      : undefined;
                    const label = t === "tasks" ? "Tasks"
                      : t === "history" ? "History"
                      : t === "workflow" ? "Workflow"
                      : t === "browser" ? "Browser"
                      : t === "memory" ? "Memory"
                      : t === "schedule" ? "Schedules"
                      : "Settings";
                    return (
                      <button key={t} role="tab" aria-selected={detailTab === t}
                        className={`cb-nav-btn ${detailTab === t ? "cb-nav-on" : ""}`}
                        onClick={() => setDetailTab(t)}>
                        <span>{label}</span>
                        {count ? <span className="cb-nav-count">{count}</span> : null}
                      </button>
                    );
                  })}
                </nav>

                <div className="cb-detail-content">
              {detailTab === "tasks" && (
                <div className="cb-panel">
                  <TaskThread tasks={selected.tasks ?? []} hue={selected.hue} busy={busy}
                    empty={<p className="sb-detail-note">
                      No tasks yet — send {selected.name} its first goal below. It plans its own
                      steps, asks you here whenever a decision is yours, and keeps running even
                      if you close this page.
                    </p>}
                    onCancel={cancelTask} onResume={resumeTask} onReply={replyTask}
                    onDecideHint={() => setTab("approvals")}
                    onWorkflow={(t) => { setWfTaskId(t.id); setDetailTab("workflow"); }} />
                  <form className="cb-composer" onSubmit={(e) => { e.preventDefault(); submitGoal(selected); }}>
                    <textarea className="sb-input sb-textarea cb-composer-input" rows={2}
                      placeholder={`Message ${selected.name} — describe the outcome you want…`}
                      value={goalText}
                      onChange={(e) => setGoalText(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          submitGoal(selected);
                        }
                      }}
                      aria-label="Task goal" />
                    <div className="cb-composer-side">
                      <div className="cb-composer-tools">
                        <button type="button" className="cb-icon-btn" title="Dictate the task (microphone)"
                          onClick={() => {
                            const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
                            if (!SR) { alert("Speech recognition isn't supported in this browser."); return; }
                            const rec = new SR();
                            rec.lang = "en-US";
                            rec.interimResults = false;
                            rec.onresult = (e: any) => {
                              const t = Array.from(e.results).map((r: any) => r[0].transcript).join(" ");
                              setGoalText((g) => (g ? g + " " : "") + t);
                            };
                            rec.onerror = () => {};
                            rec.start();
                          }}>
                          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
                        </button>
                        <label className="cb-icon-btn" title="Attach a file (PDF/resume) for this task">
                          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                          <input type="file" hidden onChange={async (e) => {
                            const f = e.target.files?.[0];
                            if (!f) return;
                            try {
                              const fd = new FormData();
                              fd.append("file", f);
                              const r = await saraFetch(`${SARA_API_URL}/api/sara-bots/custom/attachments`, {
                                method: "POST",
                                body: fd,
                              });
                              if (!r.ok) throw new Error(await r.text());
                              const d = await r.json();
                              setGoalText((g) => (g ? g + "\n" : "") + `[attachment: ${d.path}] ${d.filename}`);
                            } catch (err) {
                              alert("Upload failed: " + String(err));
                            } finally {
                              e.target.value = "";
                            }
                          }} />
                        </label>
                      </div>
                      <button type="submit" className="sb-btn sb-btn-primary"
                        disabled={busy || !goalText.trim() || selected.status !== "active"}>
                        Send
                      </button>
                      {selected.status !== "active" && (
                        <span className="sb-detail-warn">The bot is {selected.status} — resume it first.</span>
                      )}
                      <span className="cb-composer-hint">
                        Enter to send · Shift+Enter for a new line. Tasks are durable: the
                        backend worker finishes them even if you close this page.
                      </span>
                    </div>
                  </form>
                </div>
              )}

              {detailTab === "history" && (
                <div className="cb-panel">
                  <div className="cb-tasks-head">
                    <p className="sb-detail-note">
                      Every execution of this bot, newest first — the same chat-style
                      log as the section History, scoped to this bot (most recent 20).
                      Entries update live while runs are active.
                    </p>
                    <button type="button" className="sb-btn"
                      onClick={() => { setHistFilterBot(selected.id); setTab("history"); }}>
                      Open in full History
                    </button>
                  </div>
                  {detailHistoryGroups.length === 0 ? (
                    <p className="sb-detail-note">
                      Nothing has run for this bot yet — give it a goal from the Tasks tab.
                    </p>
                  ) : (
                    <div className="cb-chat-list">
                      {detailHistoryGroups.map((group) => (
                        <div key={group.label} className="cb-chat-group">
                          <div className="cb-chat-day">{group.label}</div>
                          {group.tasks.map((task) => (
                            <ChatEntry key={task.id} task={task} hue={selected.hue}
                              open={histOpenId === task.id}
                              busy={busy}
                              onToggle={() => setHistOpenId(histOpenId === task.id ? null : task.id)}
                              onCancel={cancelTask}
                              onResume={resumeTask}
                              onReply={replyTask}
                              onDecideHint={() => { setTab("approvals"); }}
                              onWorkflow={() => {
                                setWfTaskId(task.id);
                                setDetailTab("workflow");
                              }} />
                          ))}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {detailTab === "workflow" && (
                <div className="cb-panel">
                  {(!selected.tasks || selected.tasks.length === 0) ? (
                    <p className="sb-detail-note">
                      No executions recorded for this bot yet — run it from the Tasks tab
                      and its real workflow will show up here.
                    </p>
                  ) : (
                    <>
                      <div className="cb-tasks-head">
                        <p className="sb-detail-note">
                          How this bot really worked: every step it executed, the tools it
                          called, the data that flowed and where it stopped. Statuses and
                          outputs below are the real runner records for the selected run.
                        </p>
                        <select className="sb-input" value={wfTask?.id ?? ""}
                          onChange={(e) => setWfTaskId(e.target.value)}
                          aria-label="Choose a run to inspect">
                          {selected.tasks.map((t) => (
                            <option key={t.id} value={t.id}>
                              {t.status} · {fmtWhen(t.created_at)} · {t.goal.slice(0, 60)}
                            </option>
                          ))}
                        </select>
                      </div>
                      {wfTask && <WorkflowFlow task={wfTask} planMode={selected.plan_mode} />}
                    </>
                  )}
                </div>
              )}

              {detailTab === "browser" && <BrowserPanel botId={selected.id} />}

              {detailTab === "memory" && (
                <div className="cb-panel">
                  <p className="sb-detail-note">
                    Memory is stored in a separate file per bot — no other bot can read it.
                    {selected.memory_enabled
                      ? " It is included in task planning context."
                      : " Memory is currently excluded from planning (enable it in Settings)."}
                  </p>
                  <form className="cb-memory-form"
                    onSubmit={(e) => { e.preventDefault(); addMemory(selected); }}>
                    <select className="sb-input" value={memoryKind}
                      onChange={(e) => setMemoryKind(e.target.value)}
                      aria-label="Memory kind">
                      {MEMORY_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
                    </select>
                    <input className="sb-input cb-grow" placeholder="Add a durable note…"
                      value={memoryText} maxLength={500}
                      onChange={(e) => setMemoryText(e.target.value)} />
                    <button type="submit" className="sb-btn sb-btn-primary"
                      disabled={busy || !memoryText.trim()}>Remember</button>
                  </form>

                  {(!selected.memory || selected.memory.length === 0) ? (
                    <p className="sb-detail-note">Memory is empty.</p>
                  ) : (
                    <ul className="cb-memory-list">
                      {[...selected.memory].reverse().map((entry) => (
                        <li key={entry.id} className="cb-memory-entry">
                          <span className="cb-risk">{entry.kind}</span>
                          <span className="cb-memory-text">{entry.text}</span>
                          <span className="cb-meta">{fmtWhen(entry.created_at)}</span>
                          <button className="cb-x-small" title="Forget this entry"
                            disabled={busy}
                            onClick={() => deleteMemory(selected, entry.id)}>×</button>
                        </li>
                      ))}
                    </ul>
                  )}

                  {selected.memory && selected.memory.length > 0 && (
                    <div className="cb-task-actions">
                      <button className={`sb-btn ${confirmClear ? "sb-btn-danger" : ""}`}
                        disabled={busy}
                        onClick={() => {
                          if (!confirmClear) { setConfirmClear(true); return; }
                          void clearMemory(selected);
                        }}>
                        {confirmClear ? "Click again to wipe all memory" : "Clear all memory"}
                      </button>
                    </div>
                  )}
                </div>
              )}

              {detailTab === "schedule" && (
                <div className="cb-panel">
                  <p className="sb-detail-note">
                    Schedules submit normal tasks through the same engine — same approvals,
                    same audit trail. While a run from a schedule is active, the next tick
                    is skipped (no overlapping runs).
                  </p>
                  <form className="cb-memory-form"
                    onSubmit={(e) => { e.preventDefault(); createSchedule(selected); }}>
                    <input className="sb-input cb-interval" type="number"
                      min={5} max={10080} value={schedInterval}
                      onChange={(e) => setSchedInterval(Number(e.target.value))}
                      aria-label="Interval in minutes" />
                    <span className="cb-meta">minutes</span>
                    <input className="sb-input cb-grow"
                      placeholder={selected.goal || "Goal for every run…"}
                      value={schedGoal}
                      onChange={(e) => setSchedGoal(e.target.value)} />
                    <button type="submit" className="sb-btn sb-btn-primary" disabled={busy}>
                      Add schedule
                    </button>
                  </form>

                  {(!selected.schedules || selected.schedules.length === 0) ? (
                    <p className="sb-detail-note">No schedules for this bot.</p>
                  ) : (
                    <ul className="cb-task-list">
                      {selected.schedules.map((schedule) => (
                        <li key={schedule.id} className="cb-sched-row">
                          <span className={`cb-sched-state ${schedule.enabled ? "cb-on" : ""}`}>
                            {schedule.enabled ? "●" : "○"}
                          </span>
                          <span className="cb-task-goal">{schedule.goal}</span>
                          <span className="cb-meta">every {schedule.interval_minutes} min</span>
                          <span className="cb-meta">next {fmtIn(schedule.next_run_at)}</span>
                          <span className="cb-meta">{schedule.runs} runs · {schedule.skipped} skipped</span>
                          <button className="sb-btn" disabled={busy}
                            onClick={() => toggleSchedule(schedule.id)}>
                            {schedule.enabled ? "Disable" : "Enable"}
                          </button>
                          <button className="sb-btn sb-btn-ghost" disabled={busy}
                            onClick={() => deleteSchedule(schedule.id)}>
                            Delete
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {detailTab === "settings" && (
                <div className="cb-panel">
                  <dl className="cb-kv">
                    <div><dt>Bot id</dt><dd><code>{selected.id}</code></dd></div>
                    <div><dt>Plan mode</dt><dd>{selected.plan_mode === "direct" ? `direct call to ${selected.direct_tool}` : "LLM plans the steps from assigned tools"}</dd></div>
                    <div><dt>Autonomy</dt><dd>{selected.autonomy} — {AUTONOMY_HELP[selected.autonomy] ?? ""}</dd></div>
                    <div><dt>Assigned tools</dt><dd>
                      {(selected.resolved_tools ?? []).map((t) => (
                        <span key={t.id} className="cb-tool-chip">
                          <span className="cb-tstate" style={{ color: TOOL_STATE_COLOR[t.state] ?? "#94a3b8" }}>●</span>
                          {t.id}
                        </span>
                      ))}
                    </dd></div>
                    <div><dt>Direct params</dt><dd><code>{JSON.stringify(selected.direct_params ?? {})}</code></dd></div>
                    <div><dt>Language / model</dt><dd>{selected.language}{selected.model ? ` · ${selected.model}` : " · default model"}</dd></div>
                    <div><dt>Created</dt><dd>{fmtWhen(selected.created_at)} · updated {fmtWhen(selected.updated_at)}</dd></div>
                  </dl>
                  <div className="cb-task-actions">
                    <button className="sb-btn sb-btn-primary" disabled={busy}
                      onClick={() => openEdit(selected)}>
                      Edit configuration
                    </button>
                    <button className="sb-btn" disabled={busy} onClick={() => pauseResume(selected)}>
                      {selected.status === "active" ? "Pause bot" : "Resume bot"}
                    </button>
                    <button className="sb-btn" disabled={busy} onClick={() => duplicateBot(selected)}>
                      Duplicate
                    </button>
                  </div>
                  <div className="cb-danger">
                    <div className="cb-danger-title">Delete this bot</div>
                    <p className="sb-detail-note">
                      Deletes the definition, cancels active tasks and removes its schedules.
                      Task history stays for audit. This cannot be undone.
                    </p>
                    <button className={`sb-btn ${confirmDelete ? "sb-btn-danger" : ""}`}
                      disabled={busy}
                      onClick={() => {
                        if (!confirmDelete) { setConfirmDelete(true); return; }
                        void removeBot(selected);
                      }}>
                      {confirmDelete ? "Click again to delete" : "Delete bot"}
                    </button>
                  </div>
                </div>
              )}
                </div>
              </div>
            </section>
          )}
        </>
      )}

      {/* ── approvals inbox ── */}
      {tab === "approvals" && (
        <section className="cb-panel cb-panel-wide" aria-label="Pending approvals">
          <h3 className="cb-sub-title">Pending approvals</h3>
          <p className="sb-detail-note">
            A step is paused until you decide. Approving replays exactly the shown
            call with an explicit confirm; denying skips it and the task continues.
            Requests expire after their TTL — an expired request never executes.
          </p>
          {approvals.length === 0 ? (
            <div className="sb-empty">Nothing waiting for you.</div>
          ) : (
            <ul className="cb-approval-list">
              {approvals.map((a) => (
                <li key={a.id} className="cb-approval">
                  <div className="cb-approval-head">
                    <span className="cb-approval-bot">{a.bot_name ?? "(deleted bot)"}</span>
                    <span className="sb-kind-tag">STEP {a.step_index + 1}</span>
                    <span className="cb-tool-id">{a.tool}</span>
                    <span className="cb-approval-ttl" title="After this moment the request is expired and cannot be approved.">
                      expires {fmtIn(a.expires_at)}
                    </span>
                  </div>
                  {a.goal && <div className="cb-approval-goal">Goal: {a.goal}</div>}
                  <div className="cb-approval-reason">{a.reason}</div>
                  <pre className="cb-params">payload: {a.params_preview || "{}"}</pre>
                  <div className="cb-approval-foot">
                    <button className="sb-btn sb-btn-primary" disabled={busy}
                      onClick={() => decide(a, "approve")}>Approve</button>
                    <button className="sb-btn" disabled={busy}
                      onClick={() => decide(a, "deny")}>Deny</button>
                    <span className="cb-meta">task status: {a.task_status ?? "?"}</span>
                    <span className="cb-meta">requested {fmtWhen(a.created_at)}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {/* ── tasks view ── */}
      {tab === "tasks" && (
        <section className="cb-panel cb-panel-wide" aria-label="All tasks">
          <div className="cb-tasks-head">
            <h3 className="cb-sub-title">All tasks</h3>
            <select className="sb-input" value={taskFilterBot}
              onChange={(e) => setTaskFilterBot(e.target.value)}
              aria-label="Filter tasks by bot">
              <option value="">All bots</option>
              {bots.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
          </div>
          {filteredTasks.length === 0 ? (
            <div className="sb-empty">No tasks yet. Submit a goal from a bot&apos;s detail panel.</div>
          ) : (
            <TaskThread tasks={filteredTasks} hue={(t) => botById.get(t.dot_id)?.hue ?? 190}
              busy={busy} showBot empty={null}
              onCancel={cancelTask} onResume={resumeTask} onReply={replyTask}
              onDecideHint={() => setTab("approvals")}
              onWorkflow={(t) => { if (botById.has(t.dot_id)) openWorkflow(t); }} />
          )}
        </section>
      )}

      {/* ── execution history — chat-style log ── */}
      {tab === "history" && (
        <section className="cb-panel cb-panel-wide" aria-label="Execution history">
          <div className="cb-tasks-head">
            <h3 className="cb-sub-title">Execution history</h3>
            <select className="sb-input" value={histFilterBot}
              onChange={(e) => setHistFilterBot(e.target.value)}
              aria-label="Filter history by bot">
              <option value="">All bots</option>
              {bots.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
            <select className="sb-input" value={histFilterStatus}
              onChange={(e) => setHistFilterStatus(e.target.value)}
              aria-label="Filter history by status">
              <option value="">All statuses</option>
              <option value="active">Active (queued → running)</option>
              <option value="completed">Completed</option>
              <option value="failed">Failed</option>
              <option value="waiting_approval">Waiting approval</option>
              <option value="blocked">Blocked</option>
              <option value="cancelled">Cancelled</option>
            </select>
            <select className="sb-input" value={histFilterKind}
              onChange={(e) => setHistFilterKind(e.target.value)}
              aria-label="Filter history by source">
              <option value="">All sources</option>
              <option value="user">You asked</option>
              <option value="schedule">Scheduled ticks</option>
            </select>
          </div>
          <p className="sb-detail-note">
            Every execution, newest first — like a chat with your bots: what was asked,
            how far the worker got, and exactly what it produced. Entries come straight
            from the task store (the newest 100) and update live while runs are active.
          </p>
          {historyTasks.length === 0 ? (
            <div className="sb-empty">
              Nothing has run yet with these filters. Submit a goal from a
              bot&apos;s detail panel — the run appears here the moment it is queued.
            </div>
          ) : (
            <div className="cb-chat-list">
              {historyGroups.map((group) => (
                <div key={group.label} className="cb-chat-group">
                  <div className="cb-chat-day">{group.label}</div>
                  {group.tasks.map((task) => (
                    <ChatEntry key={task.id} task={task}
                      hue={botById.get(task.dot_id)?.hue ?? 210}
                      open={histOpenId === task.id}
                      busy={busy}
                      onToggle={() => setHistOpenId(histOpenId === task.id ? null : task.id)}
                      onCancel={cancelTask}
                      onResume={resumeTask}
                      onReply={replyTask}
                      onDecideHint={() => setTab("approvals")}
                      onWorkflow={botById.has(task.dot_id)
                        ? () => openWorkflow(task)
                        : undefined} />
                  ))}
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {/* ── wizard overlay ── */}
      {wizard && (
        <div className="cb-overlay" role="dialog" aria-modal="true"
          aria-label={wizard.editing ? "Edit bot" : "Create a bot"}>
          <div className="cb-wizard">
            <div className="cb-wizard-head">
              <div>
                <div className="cb-wizard-step">
                  Step {wizardPos + 1} of {wizardSteps.length}
                  {wizard.editing ? " · editing" : ""}
                </div>
                <h2 className="cb-wizard-title">
                  {wizard.step === 1 ? "Describe your bot"
                    : wizard.step === 2 ? "Choose its tools"
                    : wizard.step === 3 ? "Set its behaviour"
                    : wizard.step === 4 ? "Add a schedule (optional)"
                    : wizard.editing ? "Review changes" : "Review & create"}
                </h2>
              </div>
              <button className="cb-x" aria-label="Close"
                onClick={() => setWizard(null)}>×</button>
            </div>

            {wizard.error && (
              <div className="sb-banner sb-banner-error cb-wizard-error" role="alert">
                {wizard.error}
              </div>
            )}

            <div className="cb-wizard-body">
              {wizard.step === 1 && (
                <div className="cb-wiz-pane">
                  <p className="sb-detail-note">
                    Say what the bot should achieve. The factory asks the live LLM
                    chain for a design, then the server keeps only tools that
                    actually exist — anything it cannot cover is reported, never faked.
                  </p>
                  <textarea className="sb-input sb-textarea" rows={5}
                    placeholder="e.g. Every morning, check my system diagnostics and write a short health note into memory…"
                    value={wizard.description}
                    onChange={(e) => setWizard((w) => w ? { ...w, description: e.target.value, error: "" } : w)}
                    aria-label="Bot description" />
                  <div className="cb-wizard-actions">
                    <button className="sb-btn sb-btn-primary" disabled={wizard.proposing}
                      onClick={propose}>
                      {wizard.proposing ? "Designing…" : "Ask the factory"}
                    </button>
                    <button className="sb-btn" disabled={wizard.proposing}
                      onClick={() => setWizard((w) => w ? { ...w, step: 2, error: "" } : w)}>
                      Skip — I&apos;ll configure it myself
                    </button>
                  </div>
                </div>
              )}

              {wizard.step === 2 && (
                <div className="cb-wiz-pane">
                  {wizard.notes && (wizard.notes.dropped.length > 0 || wizard.notes.missing.length > 0) && (
                    <div className="sb-banner sb-banner-info cb-wizard-error" role="status">
                      {wizard.notes.dropped.length > 0 && (
                        <div>
                          The model asked for tools that are not usable right now; they were dropped:
                          <ul>{wizard.notes.dropped.map((d, i) => (
                            <li key={i}><code>{d.tool}</code> — {d.reason}</li>))}
                          </ul>
                        </div>
                      )}
                      {wizard.notes.missing.length > 0 && (
                        <div>Not covered by any ready tool: {wizard.notes.missing.join("; ")}</div>
                      )}
                    </div>
                  )}
                  <ToolPicker tools={tools} selected={wizard.draft.tools}
                    onChange={(ids) => setDraft({ tools: ids })} />
                </div>
              )}

              {wizard.step === 3 && (
                <div className="cb-wiz-pane cb-form-grid">
                  <label className="cb-field">
                    <span className="cb-field-label">Name *</span>
                    <input className="sb-input" value={wizard.draft.name} maxLength={60}
                      onChange={(e) => {
                        const name = e.target.value;
                        setWizard((w) => w ? {
                          ...w, error: "",
                          draft: {
                            ...w.draft, name,
                            hue: w.hueTouched ? w.draft.hue : hueForName(name),
                          },
                        } : w);
                      }} />
                  </label>
                  <label className="cb-field">
                    <span className="cb-field-label">Category</span>
                    <select className="sb-input" value={wizard.draft.category}
                      onChange={(e) => setDraft({ category: e.target.value })}>
                      {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </label>
                  <label className="cb-field cb-span2">
                    <span className="cb-field-label">Description</span>
                    <input className="sb-input" value={wizard.draft.description} maxLength={400}
                      onChange={(e) => setDraft({ description: e.target.value })} />
                  </label>
                  <div className="cb-field cb-span2">
                    <span className="cb-field-label">Orb colour</span>
                    <div className="cb-hue-row">
                      <span className="cb-dot cb-hue-preview" style={orbStyle(wizard.draft.hue, 34)} />
                      <div className="cb-hue-swatches">
                        {HUE_SWATCHES.map((h) => (
                          <button key={h} type="button" aria-label={`orb hue ${h}`}
                            className={`cb-hue-swatch ${wizard.draft.hue === h ? "cb-hue-swatch-on" : ""}`}
                            style={{ background: `hsla(${h},85%,55%,.9)` }}
                            onClick={() => pickHue(h)} />
                        ))}
                      </div>
                      <input type="range" min={0} max={360} step={1}
                        className="cb-hue-slider" value={wizard.draft.hue}
                        aria-label="fine orb hue"
                        onChange={(e) => pickHue(Number(e.target.value))} />
                      <span className="cb-meta">{wizard.draft.hue}°</span>
                    </div>
                    <span className="cb-radio-help">The same bot always shows exactly this colour — pick a preset or slide to fine-tune. Editing the name only recolours it until you pick one yourself.</span>
                  </div>

                  <label className="cb-field cb-span2">
                    <span className="cb-field-label">Default goal</span>
                    <textarea className="sb-input sb-textarea" rows={2}
                      value={wizard.draft.goal} maxLength={2000}
                      onChange={(e) => setDraft({ goal: e.target.value })} />
                  </label>
                  <label className="cb-field cb-span2">
                    <span className="cb-field-label">Behaviour instructions</span>
                    <textarea className="sb-input sb-textarea" rows={4}
                      value={wizard.draft.instructions} maxLength={8000}
                      placeholder="How should it work? What must it never do?"
                      onChange={(e) => setDraft({ instructions: e.target.value })} />
                  </label>
                  <label className="cb-field cb-span2">
                    <span className="cb-field-label">Expected output format</span>
                    <input className="sb-input" value={wizard.draft.output_format} maxLength={200}
                      placeholder="e.g. a short markdown report"
                      onChange={(e) => setDraft({ output_format: e.target.value })} />
                  </label>

                  <div className="cb-field cb-span2">
                    <span className="cb-field-label">Autonomy</span>
                    <div className="cb-radio-col">
                      {AUTONOMY.map((mode) => (
                        <label key={mode} className={`cb-radio ${wizard.draft.autonomy === mode ? "cb-radio-on" : ""}`}>
                          <input type="radio" name="autonomy" checked={wizard.draft.autonomy === mode}
                            onChange={() => setDraft({ autonomy: mode })} />
                          <span>
                            <strong>{mode}</strong>
                            <span className="cb-radio-help">{AUTONOMY_HELP[mode]}</span>
                          </span>
                        </label>
                      ))}
                    </div>
                  </div>

                  <div className="cb-field cb-span2">
                    <span className="cb-field-label">Plan mode</span>
                    <div className="cb-radio-col">
                      <label className={`cb-radio ${wizard.draft.plan_mode === "auto" ? "cb-radio-on" : ""}`}>
                        <input type="radio" name="plan_mode" checked={wizard.draft.plan_mode === "auto"}
                          onChange={() => setDraft({ plan_mode: "auto" })} />
                        <span>
                          <strong>auto</strong>
                          <span className="cb-radio-help">The live LLM plans the steps using only the assigned tools.</span>
                        </span>
                      </label>
                      <label className={`cb-radio ${wizard.draft.plan_mode === "direct" ? "cb-radio-on" : ""}`}>
                        <input type="radio" name="plan_mode" checked={wizard.draft.plan_mode === "direct"}
                          onChange={() => setDraft({ plan_mode: "direct" })} />
                        <span>
                          <strong>direct</strong>
                          <span className="cb-radio-help">One deterministic tool call — no LLM in the loop.</span>
                        </span>
                      </label>
                    </div>
                    {wizard.draft.plan_mode === "direct" && (
                      <div className="cb-direct">
                        <label className="cb-field">
                          <span className="cb-field-label">Direct tool (must be assigned)</span>
                          <select className="sb-input" value={wizard.draft.direct_tool}
                            onChange={(e) => setDraft({ direct_tool: e.target.value })}>
                            <option value="">— pick a tool —</option>
                            {wizard.draft.tools.map((id) => <option key={id} value={id}>{id}</option>)}
                          </select>
                        </label>
                        <label className="cb-field">
                          <span className="cb-field-label">Params (JSON object, supports {"{{goal}}"})</span>
                          <textarea className="sb-input sb-textarea" rows={3}
                            placeholder='{"action": "battery"}'
                            value={wizard.draft.direct_params_text}
                            onChange={(e) => setDraft({ direct_params_text: e.target.value })} />
                        </label>
                      </div>
                    )}
                  </div>

                  <label className="cb-checkline cb-span2">
                    <input type="checkbox" checked={wizard.draft.memory_enabled}
                      onChange={(e) => setDraft({ memory_enabled: e.target.checked })} />
                    <span>Keep scoped memory for this bot (one private file, never shared)</span>
                  </label>
                </div>
              )}

              {wizard.step === 4 && (
                <div className="cb-wiz-pane cb-form-grid">
                  <label className="cb-checkline cb-span2">
                    <input type="checkbox" checked={wizard.scheduleOn}
                      onChange={(e) => setWizard((w) => w ? { ...w, scheduleOn: e.target.checked } : w)} />
                    <span>Run this bot on a schedule (submits a normal task each tick)</span>
                  </label>
                  {wizard.scheduleOn && (
                    <>
                      <label className="cb-field">
                        <span className="cb-field-label">Every (minutes, 5–10080)</span>
                        <input className="sb-input" type="number" min={5} max={10080}
                          value={wizard.scheduleInterval}
                          onChange={(e) => setWizard((w) => w ? { ...w, scheduleInterval: Number(e.target.value) } : w)} />
                      </label>
                      <label className="cb-field cb-span2">
                        <span className="cb-field-label">Goal for each run</span>
                        <textarea className="sb-input sb-textarea" rows={2}
                          value={wizard.scheduleGoal}
                          placeholder={wizard.draft.goal || "Goal…"}
                          onChange={(e) => setWizard((w) => w ? { ...w, scheduleGoal: e.target.value } : w)} />
                      </label>
                    </>
                  )}
                </div>
              )}

              {wizard.step === 5 && (
                <div className="cb-wiz-pane">
                  <dl className="cb-kv">
                    <div><dt>Name</dt><dd>{wizard.draft.name || "—"}</dd></div>
                    <div><dt>Orb</dt><dd className="cb-hue-review">
                      <span className="cb-dot cb-dot-sm" style={orbStyle(wizard.draft.hue, 16)} />
                      <span>hue {wizard.draft.hue}</span>
                    </dd></div>
                    <div><dt>Description</dt><dd>{wizard.draft.description || "—"}</dd></div>
                    <div><dt>Goal</dt><dd>{wizard.draft.goal || "—"}</dd></div>
                    <div><dt>Tools</dt><dd>{wizard.draft.tools.join(", ") || "—"}</dd></div>
                    <div><dt>Autonomy</dt><dd>{wizard.draft.autonomy}</dd></div>
                    <div><dt>Plan mode</dt><dd>
                      {wizard.draft.plan_mode === "direct"
                        ? `direct → ${wizard.draft.direct_tool} ${wizard.draft.direct_params_text ? wizard.draft.direct_params_text : ""}`
                        : "auto (LLM plans per task)"}
                    </dd></div>
                    <div><dt>Memory</dt><dd>{wizard.draft.memory_enabled ? "on (private per bot)" : "off"}</dd></div>
                    {!wizard.editing && (
                      <div><dt>Schedule</dt><dd>
                        {wizard.scheduleOn
                          ? `every ${wizard.scheduleInterval} min — ${wizard.scheduleGoal || wizard.draft.goal}`
                          : "none"}
                      </dd></div>
                    )}
                  </dl>
                  <p className="sb-detail-note">
                    Natural-language instructions never grant permissions — the tool
                    allowlist, autonomy setting and the runner&apos;s deterministic
                    gates decide what can actually run.
                  </p>
                </div>
              )}
            </div>

            <div className="cb-wizard-foot">
              <button className="sb-btn" disabled={wizardPos === 0 || wizard.saving}
                onClick={() => setWizard((w) => w ? { ...w, step: wizardSteps[Math.max(0, wizardPos - 1)], error: "" } : w)}>
                Back
              </button>
              {wizardPos < wizardSteps.length - 1 ? (
                <button className="sb-btn sb-btn-primary" disabled={!wizardCanContinue || wizard.proposing}
                  onClick={() => setWizard((w) => w ? { ...w, step: wizardSteps[wizardPos + 1], error: "" } : w)}>
                  Continue
                </button>
              ) : (
                <button className="sb-btn sb-btn-primary" disabled={!wizardCanContinue || wizard.saving}
                  onClick={saveWizard}>
                  {wizard.saving ? "Saving…" : wizard.editing ? "Save changes" : "Create bot"}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── styles (extends the sb-* language from the Fleet page) ──────────────── */

const CB_CSS = `
.cb-root{max-width:1500px;margin:0 auto}
.cb-mt{margin-top:12px}
.cb-dot{width:58px;height:58px;border-radius:50%;display:inline-block;flex-shrink:0}
.cb-dot-lg{width:52px;height:52px}
.cb-dot-sm{width:16px;height:16px;vertical-align:middle}
.cb-hue-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.cb-hue-preview{width:34px;height:34px}
.cb-hue-swatches{display:flex;gap:6px;flex-wrap:wrap}
.cb-hue-swatch{width:20px;height:20px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0;transition:transform .12s ease,border-color .12s ease}
.cb-hue-swatch:hover{transform:scale(1.18)}
.cb-hue-swatch-on{border-color:#e2e8f0;transform:scale(1.15)}
.cb-hue-slider{width:150px;accent-color:#22d3ee}
.cb-hue-review{display:flex;align-items:center;gap:8px}
.cb-engine{display:inline-flex;align-items:center;gap:7px;font-family:ui-monospace,monospace;font-size:10.5px;color:#94a3b8;border:1px solid rgba(255,255,255,.1);border-radius:999px;padding:5px 12px}
.cb-engine-dot{width:7px;height:7px;border-radius:50%;display:inline-block}
.cb-meta{font-family:ui-monospace,monospace;font-size:10px;color:#64748b;letter-spacing:.04em}
.cb-warn{color:#fbbf24}
.cb-sub-title{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#64748b;margin:18px 0 8px}

.sb-btn-danger{background:rgba(248,113,113,.18);border-color:rgba(248,113,113,.5);color:#fecaca}
.sb-btn-danger:hover{background:rgba(248,113,113,.28)}

.cb-panel{margin-top:16px}
.cb-panel-wide{max-width:1500px;margin:22px auto 0;padding:22px 24px;border:1px solid rgba(255,255,255,.08);border-radius:18px;background:rgba(2,6,23,.35)}

.cb-ready{margin-top:16px;padding:12px 16px;border-radius:12px;font-size:12.5px;line-height:1.7}
.cb-ready-blocked{border:1px solid rgba(248,113,113,.35);background:rgba(248,113,113,.07);color:#fecaca}
.cb-ready ul{margin:8px 0 0;padding-left:20px}

/* vertical detail layout: sidebar nav + content column */
/* hero: stat strip + goal card */
.cb-stat-row{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 4px}
.cb-stat{display:flex;align-items:baseline;gap:6px;padding:8px 14px;border:1px solid rgba(148,163,184,.16);border-radius:12px;background:rgba(2,6,23,.45)}
.cb-stat-v{font-size:18px;font-weight:800;color:#67e8f9;letter-spacing:-.02em}
.cb-stat-k{font-size:11px;color:#94a3b8}
.cb-goal-card{margin:14px 0 4px;border:1px solid rgba(103,232,249,.18);border-radius:14px;background:rgba(2,6,23,.5);overflow:hidden}
.cb-goal-row{display:flex;gap:14px;padding:11px 16px;font-size:12.5px;line-height:1.65;border-bottom:1px solid rgba(148,163,184,.08)}
.cb-goal-row:last-child{border-bottom:none}
.cb-goal-k{flex:0 0 128px;color:#64748b;font-family:ui-monospace,monospace;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;padding-top:2px}
.cb-goal-v{flex:1;color:#cbd5e1}

.sb-detail{max-width:1500px;margin:26px auto 0;padding:28px 30px;border:1px solid rgba(103,232,249,.22);border-radius:22px;background:linear-gradient(180deg,rgba(8,15,26,.9),rgba(4,8,16,.95));box-shadow:0 32px 80px rgba(0,0,0,.6),0 0 50px -20px rgba(103,232,249,.35)}

/* sidebar nav + content column */
.cb-detail-grid{display:grid;grid-template-columns:216px minmax(0,1fr);gap:18px;margin-top:18px;align-items:start}
.cb-detail-nav{display:flex;flex-direction:column;gap:3px;border:1px solid rgba(148,163,184,.12);border-radius:14px;padding:6px;background:rgba(2,6,23,.5);position:sticky;top:12px;box-shadow:0 2px 16px rgba(0,0,0,.25)}
.cb-nav-btn{display:flex;align-items:center;justify-content:space-between;gap:10px;text-align:left;background:none;border:1px solid transparent;border-radius:10px;padding:10px 14px;color:#94a3b8;font-size:12.5px;font-weight:500;cursor:pointer;font-family:inherit;transition:all .12s}
.cb-nav-btn:hover{background:rgba(103,232,249,.08);color:#e2e8f0;border-color:rgba(103,232,249,.15)}
.cb-nav-btn.cb-nav-on{background:rgba(34,211,238,.12);border-color:rgba(103,232,249,.35);color:#67e8f9;font-weight:700;box-shadow:0 0 0 1px rgba(103,232,249,.15)}
.cb-nav-count{font-family:ui-monospace,monospace;font-size:10px;color:#67e8f9;opacity:.85;background:rgba(103,232,249,.15);padding:1px 6px;border-radius:999px}

/* chat composer (bottom of the tasks thread) */
.cb-composer{margin-top:14px;display:flex;gap:12px;align-items:flex-end;border:1px solid rgba(103,232,249,.3);border-radius:16px;padding:14px;background:linear-gradient(180deg,rgba(2,6,23,.7),rgba(2,6,23,.45));box-shadow:0 4px 24px rgba(0,0,0,.3),inset 0 1px 0 rgba(255,255,255,.04)}
.cb-composer-input{flex:1;resize:vertical;min-height:48px;padding:12px 14px;border:1px solid rgba(103,232,249,.25);border-radius:12px;background:rgba(2,6,23,.8);color:#e2e8f0;font-size:13.5px;line-height:1.5;transition:border-color .15s,box-shadow .15s}
.cb-composer-input:focus{outline:none;border-color:#67e8f9;box-shadow:0 0 0 3px rgba(103,232,249,.15)}
.cb-composer-side{display:flex;flex-direction:column;gap:8px;align-items:flex-end;flex-shrink:0}
.cb-composer-tools{display:flex;gap:6px;align-items:center}
.cb-icon-btn{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:10px;border:1px solid rgba(148,163,184,.25);background:rgba(15,23,42,.6);color:#cbd5e1;cursor:pointer;transition:all .15s}
.cb-icon-btn:hover{background:rgba(34,211,238,.12);border-color:rgba(34,211,238,.45);color:#67e8f9;transform:translateY(-1px)}
.cb-icon-btn:active{transform:translateY(0)}
.cb-icon-btn svg{display:block}
.cb-composer-hint{font-size:10px;color:#64748b;max-width:280px;text-align:right;line-height:1.5;margin-top:4px}

@media (max-width:900px){.cb-detail-grid{grid-template-columns:1fr}.cb-detail-nav{flex-direction:row;flex-wrap:wrap;position:static}.cb-nav-btn{flex:1 1 auto}.cb-thread{max-height:56vh}}
.cb-detail-content{min-width:0}

/* the tasks chat thread (vertical, chat-like) */
.cb-thread{display:flex;flex-direction:column;gap:22px;max-height:min(62vh,640px);overflow-y:auto;padding:6px 4px 10px;scroll-behavior:smooth}
.cb-thread-empty{max-height:none}
.cb-thread-more{margin:0;font-family:ui-monospace,monospace;font-size:10px;color:#64748b;text-align:center}
.cb-turn{display:flex;flex-direction:column;gap:10px}
.cb-turn-row{display:flex;gap:12px;align-items:flex-start}
.cb-turn-you{justify-content:flex-end}
.cb-turn-orb{width:26px;height:26px;border-radius:50%;flex-shrink:0;margin-top:2px;display:inline-block}
.cb-turn-col{flex:1;min-width:0;display:flex;flex-direction:column;gap:8px;align-items:flex-start}
.cb-turn-botname{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.cb-bubble{border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:10px 14px;max-width:min(680px,92%);background:rgba(255,255,255,.02)}
.cb-bubble-you{background:rgba(34,211,238,.08);border-color:rgba(103,232,249,.35)}
.cb-bubble-bot{background:rgba(255,255,255,.025)}
.cb-bubble-head{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.cb-bubble-who{font-family:ui-monospace,monospace;font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:#67e8f9}
.cb-bubble-when{margin-left:auto;font-family:ui-monospace,monospace;font-size:10px;color:#64748b}
.cb-bubble-text{margin-top:6px;font-size:12.5px;color:#e2e8f0;line-height:1.65;word-break:break-word;white-space:pre-wrap}
.cb-bubble-ask{border-color:rgba(251,191,36,.4)}
.cb-bubble-question{color:#fde68a;font-weight:600}
.cb-bubble-answer{color:#a7f3d0}
.cb-bubble-err{border-color:rgba(248,113,113,.4)}
.cb-bubble-warn{border-color:rgba(251,191,36,.4);background:rgba(251,191,36,.05)}
.cb-bubble-result{border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.05)}
.cb-bubble-live{display:inline-flex;gap:9px;align-items:center;border-color:rgba(34,211,238,.35);color:#67e8f9}
.cb-bubble-live .cb-bubble-text{margin-top:0}
.cb-turn-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding-top:2px}
.cb-bubble .sb-output{margin-top:8px}
.cb-bubble .cb-reply{margin:10px 0 0}

@media (max-width:640px){.cb-composer{flex-direction:column;align-items:stretch}.cb-composer-side{align-items:stretch}}

/* task lists */
.cb-task-list{list-style:none;margin:14px 0 0;padding:0;display:flex;flex-direction:column;gap:8px}
.cb-task-row{display:flex;gap:12px;align-items:center;width:100%;text-align:left;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:10px 14px;cursor:pointer;color:inherit;font-size:12px}
.cb-task-row:hover{border-color:rgba(103,232,249,.3)}
.cb-task-goal{flex:1;color:#cbd5e1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cb-task-bot{font-family:ui-monospace,monospace;font-size:10px;color:#67e8f9;flex-shrink:0}
.cb-task-body{margin:8px 0 4px 18px;padding:14px 16px;border-left:2px solid rgba(103,232,249,.3);background:rgba(2,6,23,.45);border-radius:0 12px 12px 0}
.cb-task-meta{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.cb-task-events{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px;padding:0}
.cb-task-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:14px}

.cb-steps{display:flex;flex-direction:column;gap:10px;margin:8px 0}
.cb-step-row{display:flex;gap:10px}
.cb-step-idx{width:22px;height:22px;border-radius:50%;border:1px solid rgba(103,232,249,.4);color:#67e8f9;font-family:ui-monospace,monospace;font-size:10.5px;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:2px}
.cb-step-main{min-width:0;flex:1}
.cb-step-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.cb-step-title{font-size:12.5px;font-weight:700;color:#e2e8f0}
.cb-step-tool{font-family:ui-monospace,monospace;font-size:10px;color:#67e8f9;background:rgba(34,211,238,.08);border:1px solid rgba(34,211,238,.2);border-radius:6px;padding:1px 7px}
.cb-step-note{font-size:10.5px;color:#fbbf24}
.cb-step-when{font-family:ui-monospace,monospace;font-size:10px;color:#64748b}
.cb-params{margin:8px 0 0;padding:8px 10px;border-radius:8px;background:rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.06);font-family:ui-monospace,monospace;font-size:10.5px;color:#cbd5e1;white-space:pre-wrap;word-break:break-word;max-height:160px;overflow:auto}

/* approvals */
.cb-approval-list{list-style:none;margin:14px 0 0;padding:0;display:flex;flex-direction:column;gap:12px}
.cb-approval{border:1px solid rgba(251,191,36,.3);background:rgba(251,191,36,.05);border-radius:14px;padding:14px 16px}
.cb-approval-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.cb-approval-bot{font-weight:800;font-size:13px;color:#fde68a}
.cb-tool-id{font-family:ui-monospace,monospace;font-size:11px;color:#67e8f9}
.cb-approval-ttl{margin-left:auto;font-family:ui-monospace,monospace;font-size:10.5px;color:#fbbf24}
.cb-approval-goal{margin-top:8px;font-size:12.5px;color:#cbd5e1}
.cb-approval-reason{margin-top:6px;font-size:12px;color:#fbbf24;font-weight:600}
.cb-approval-foot{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}

/* memory + schedules */
.cb-memory-form{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
.cb-memory-form .sb-input{width:auto}
.cb-grow{flex:1;min-width:220px}
.cb-interval{width:110px}
.cb-memory-list{list-style:none;margin:14px 0 0;padding:0;display:flex;flex-direction:column;gap:6px}
.cb-memory-entry{display:flex;gap:10px;align-items:center;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:10px;padding:8px 12px;font-size:12px}
.cb-memory-text{flex:1;color:#cbd5e1}
.cb-x-small{background:none;border:none;color:#64748b;font-size:15px;cursor:pointer;line-height:1;padding:2px 6px}
.cb-x-small:hover{color:#f87171}
.cb-sched-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:10px;padding:10px 14px;font-size:12px}
.cb-sched-state{color:#64748b;font-size:14px}
.cb-sched-state.cb-on{color:#34d399}

/* settings */
.cb-kv{margin:8px 0 0;display:flex;flex-direction:column;gap:8px}
.cb-kv div{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px}
.cb-kv dt{width:150px;color:#64748b;flex-shrink:0}
.cb-kv dd{margin:0;color:#cbd5e1;line-height:1.7;flex:1;min-width:200px}
.cb-kv code{background:rgba(0,0,0,.4);padding:1px 6px;border-radius:6px;font-size:11px}
.cb-tool-chip{display:inline-flex;gap:6px;align-items:center;font-family:ui-monospace,monospace;font-size:10.5px;color:#94a3b8;border:1px solid rgba(255,255,255,.1);border-radius:999px;padding:2px 10px;margin:0 6px 6px 0}
.cb-danger{margin-top:22px;padding:14px 16px;border:1px solid rgba(248,113,113,.3);border-radius:12px;background:rgba(248,113,113,.05)}
.cb-danger-title{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#f87171;margin-bottom:6px}

/* tool picker */
.cb-picker-list{max-height:46vh;overflow:auto;margin-top:8px;padding-right:4px;display:flex;flex-direction:column;gap:14px}
.cb-picker-bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.cb-picker-count{font-family:ui-monospace,monospace;font-size:10.5px;color:#64748b;margin-left:auto}
.cb-picker-group-title{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:#67e8f9;margin-bottom:6px}
.cb-tool{display:flex;gap:10px;align-items:flex-start;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:9px 12px;cursor:pointer;margin-bottom:6px;background:rgba(255,255,255,.02)}
.cb-tool-on{border-color:rgba(103,232,249,.5);background:rgba(34,211,238,.07)}
.cb-tool-body{display:flex;flex-direction:column;gap:3px;min-width:0}
.cb-tool-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.cb-tool-row .cb-tool-id{font-family:ui-monospace,monospace;font-size:11.5px;color:#e2e8f0;font-weight:700}
.cb-risk{font-family:ui-monospace,monospace;font-size:9px;letter-spacing:.12em;text-transform:uppercase;border:1px solid rgba(255,255,255,.14);border-radius:6px;padding:1px 6px;color:#94a3b8}
.cb-tstate{font-family:ui-monospace,monospace;font-size:10px}
.cb-tool-desc{font-size:11.5px;color:#94a3b8;line-height:1.55}
.cb-tool-detail{font-size:11px;color:#fbbf24}

/* wizard */
.cb-overlay{position:fixed;inset:0;z-index:80;background:rgba(2,6,23,.78);backdrop-filter:blur(6px);display:flex;align-items:flex-start;justify-content:center;padding:4vh 16px;overflow:auto}
.cb-wizard{width:min(860px,100%);border:1px solid rgba(103,232,249,.25);border-radius:20px;background:linear-gradient(180deg,rgba(8,15,26,.98),rgba(4,8,16,.99));box-shadow:0 30px 90px rgba(0,0,0,.7);padding:22px 26px 20px;color:#e2e8f0}
.cb-wizard-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.cb-wizard-step{font-family:ui-monospace,monospace;font-size:10px;letter-spacing:.18em;color:#67e8f9;text-transform:uppercase}
.cb-wizard-title{margin:6px 0 0;font-size:19px;font-weight:800;letter-spacing:-.02em}
.cb-x{background:none;border:none;color:#94a3b8;font-size:22px;cursor:pointer;line-height:1}
.cb-x:hover{color:#fff}
.cb-wizard-error{margin:14px 0 0;max-width:none}
.cb-wizard-body{margin-top:16px}
.cb-wiz-pane{display:flex;flex-direction:column;gap:12px}
.cb-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px}
.cb-span2{grid-column:1 / -1}
.cb-field{display:flex;flex-direction:column;gap:6px}
.cb-field-label{font-size:11.5px;font-weight:700;color:#e2e8f0}
.cb-checkline{display:flex;gap:10px;align-items:center;font-size:12.5px;color:#cbd5e1}
.cb-radio-col{display:flex;flex-direction:column;gap:8px}
.cb-radio{display:flex;gap:10px;align-items:flex-start;border:1px solid rgba(255,255,255,.1);border-radius:10px;padding:9px 12px;cursor:pointer;background:rgba(255,255,255,.02)}
.cb-radio-on{border-color:rgba(103,232,249,.5);background:rgba(34,211,238,.07)}
.cb-radio span{display:flex;flex-direction:column;gap:2px;font-size:12.5px}
.cb-radio-help{font-size:11.5px;color:#94a3b8;line-height:1.5}
.cb-direct{display:flex;flex-direction:column;gap:12px;margin-top:10px;padding:12px 14px;border:1px dashed rgba(103,232,249,.3);border-radius:12px}
.cb-wizard-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}
.cb-wizard-foot{display:flex;justify-content:space-between;gap:12px;margin-top:20px;padding-top:16px;border-top:1px solid rgba(255,255,255,.08)}

/* history — chat-style execution log */
.cb-tasks-head{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.cb-tasks-head .sb-sub-title{margin-right:auto}
.cb-chat-list{display:flex;flex-direction:column;gap:20px;margin-top:14px}
.cb-chat-group{display:flex;flex-direction:column;gap:10px}
.cb-chat-day{font-family:ui-monospace,monospace;font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:#67e8f9;opacity:.75;padding:2px 0}
.cb-chat-entry{display:flex;gap:12px;align-items:flex-start}
.cb-chat-orb{width:26px;height:26px;border-radius:50%;flex-shrink:0;margin-top:2px;display:inline-block}
.cb-chat-main{flex:1;min-width:0;border:1px solid rgba(255,255,255,.07);border-radius:14px;background:rgba(255,255,255,.02);padding:12px 14px}
.cb-chat-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.cb-chat-bot{font-weight:800;font-size:12.5px;color:#e2e8f0}
.cb-chat-time{margin-left:auto;font-family:ui-monospace,monospace;font-size:10px;color:#64748b}
.cb-chat-goal{margin-top:9px;padding:8px 12px;border-left:3px solid rgba(103,232,249,.55);background:rgba(34,211,238,.06);border-radius:0 10px 10px 0;font-size:12.5px;color:#e2e8f0;line-height:1.6;word-break:break-word}
.cb-chat-out{margin:9px 0 0;padding:9px 12px;border-radius:10px;background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.06);font-family:ui-monospace,monospace;font-size:11.5px;color:#cbd5e1;white-space:pre-wrap;word-break:break-word;max-height:240px;overflow:auto}
.cb-chat-plain{font-family:inherit;font-size:12px;color:#94a3b8;line-height:1.6}
.cb-chat-err{border-color:rgba(248,113,113,.35);color:#fecaca}
.cb-chat-warn{border-color:rgba(251,191,36,.35);color:#fde68a}
.cb-chat-live{display:flex;align-items:center;gap:8px;color:#67e8f9}
.cb-chat-caret{width:7px;height:7px;border-radius:50%;background:#22d3ee;animation:cb-pulse 1.4s ease-in-out infinite;flex-shrink:0}
@keyframes cb-pulse{0%,100%{opacity:.35}50%{opacity:1}}
.cb-chat-link{background:none;border:none;padding:0;margin-left:6px;color:#67e8f9;text-decoration:underline;cursor:pointer;font-size:inherit;font-family:inherit}
.cb-chat-steps{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:10px}
.cb-chat-step{width:20px;height:20px;border-radius:6px;border:1px solid;display:inline-flex;align-items:center;justify-content:center;font-family:ui-monospace,monospace;font-size:10px}
.cb-chat-foot{display:flex;gap:12px;align-items:center;margin-top:10px}
.cb-chat-foot .cb-meta{margin-right:auto}
.cb-chat-open{background:none;border:1px solid rgba(103,232,249,.3);border-radius:999px;color:#67e8f9;font-size:11px;padding:4px 12px;cursor:pointer}
.cb-chat-open:hover{background:rgba(34,211,238,.08)}

/* workflow — how the run really executed */
.cb-flow{display:flex;flex-direction:column;margin-top:14px}
.cb-flow-kpis{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:12px}
.cb-flow-node{border:1px solid rgba(255,255,255,.09);border-radius:14px;background:rgba(255,255,255,.025);padding:12px 14px}
.cb-flow-start{border-color:rgba(103,232,249,.25);background:rgba(34,211,238,.04)}
.cb-flow-completed{border-color:rgba(52,211,153,.4)}
.cb-flow-failed{border-color:rgba(248,113,113,.45)}
.cb-flow-running{border-color:rgba(34,211,238,.5)}
.cb-flow-waiting_approval{border-color:rgba(251,191,36,.45)}
.cb-flow-blocked{border-color:rgba(245,158,11,.45)}
.cb-flow-cancelled{border-color:rgba(100,116,139,.4)}
.cb-flow-arrow{text-align:center;color:#475569;font-size:14px;line-height:1;padding:6px 0;display:flex;align-items:center;justify-content:center;gap:8px}
.cb-flow-arrow-label{color:#64748b;font-size:10px;font-family:ui-monospace,monospace;letter-spacing:.08em}
.cb-flow-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.cb-flow-idx{width:24px;height:24px;border-radius:50%;border:1px solid rgba(103,232,249,.4);color:#67e8f9;font-family:ui-monospace,monospace;font-size:11px;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0}
.cb-flow-title{font-size:12.5px;font-weight:700;color:#e2e8f0}
.cb-flow-meta{display:flex;gap:12px;flex-wrap:wrap;margin-top:6px}
.cb-flow-badge{font-family:ui-monospace,monospace;font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:#67e8f9;border:1px solid rgba(103,232,249,.35);border-radius:6px;padding:2px 8px;margin-right:8px}
.cb-flow-text{font-size:12px;color:#94a3b8}
.cb-flow-details{margin-top:8px}
.cb-flow-details summary{cursor:pointer;font-size:11px;color:#67e8f9}
.cb-flow-empty{margin-top:12px;padding:12px 14px;border:1px dashed rgba(255,255,255,.15);border-radius:12px;font-size:12px;color:#94a3b8;line-height:1.6}

/* the bot's chat question (agent.ask_owner) */
.cb-reply{margin:10px 0 4px;border:1px solid rgba(251,191,36,.35);background:rgba(251,191,36,.05);border-radius:14px;padding:12px 14px}
.cb-reply-q{display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap}
.cb-reply-badge{font-family:ui-monospace,monospace;font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:#fbbf24;border:1px solid rgba(251,191,36,.4);border-radius:6px;padding:2px 8px;flex-shrink:0}
.cb-reply-text{font-size:12.5px;color:#e2e8f0;line-height:1.55}
.cb-reply-opts{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.cb-reply-row{display:flex;gap:8px;margin-top:10px;align-items:center}
.cb-reply-row .sb-input{flex:1}
.cb-reply-hint{margin:8px 0 0;font-size:10.5px;color:#64748b;line-height:1.5}

/* the bot's own browser panel */
.cb-browser-status{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:10px 0}
.cb-browser-url{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cb-browser-shot{display:block;width:100%;margin-top:12px;border:1px solid rgba(255,255,255,.12);border-radius:12px;background:#0b1220}

@media (max-width:640px){.cb-form-grid{grid-template-columns:1fr}.cb-kv dt{width:100%}}
`;
