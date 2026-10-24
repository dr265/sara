"use client";

import { Suspense } from "react";

/**
 * SARA SALES COMMAND CENTER — WhatsApp-style conversational control interface.
 *
 * This is the main natural-language interface for the SARA Autonomous AI Sales Agent.
 * It connects to the real backend at /api/sales/* and provides:
 *   • Persistent conversation history with execution state
 *   • Real-time task progress from actual backend work
 *   • Natural-language commands in English, Hindi, Hinglish
 *   • Lead discovery, qualification, campaign creation, sending
 *   • Product listing drafts, approvals, automation controls
 *   • Integration health, status reports, emergency stop
 *
 * Everything is grounded in persisted records — no simulated progress.
 */

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { SARA_API_URL, saraFetch } from "@/lib/api";

/* ── types (mirror backend payloads) ──────────────────────────────────────── */

interface SalesMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  ts: number;
  intent?: string;
  task_id?: string;
  status?: string;
  parser?: string;
}

interface SalesConversation {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  messages: SalesMessage[];
  context: Record<string, unknown>;
}

interface SalesTask {
  id: string;
  kind: string;
  title: string;
  params: Record<string, unknown>;
  conversation_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | "awaiting_approval";
  plan: Array<{ title: string; status: string; detail: string }>;
  result: string;
  error: string;
  created_at: number;
  updated_at: number;
  refs: Record<string, string>;
}

interface SalesLead {
  id: string;
  name: string;
  domain: string;
  website: string;
  phone: string;
  email: string;
  industry: string;
  region: string;
  source_url: string;
  discovered_at: number;
  profile: Record<string, unknown>;
  stage: string;
  score: number;
  score_reasons: string[];
  product_id: string;
}

interface SalesCampaign {
  id: string;
  product_id: string;
  ea_campaign_id: string;
  status: "preview" | "approved" | "running" | "completed";
  recipients: number;
  sent: number;
  failed: number;
  created_at: number;
}

interface SalesListing {
  id: string;
  product_id: string;
  body: string;
  status: "draft" | "published";
  created_at: number;
}

interface DashboardData {
  worker: {
    running: boolean;
    mode: string;
    emergency_stop: boolean;
    queued: number;
    active: number;
    failed: number;
  };
  integrations: Record<string, { connected: boolean; detail: string; provider?: string }>;
  leads: { total: number; by_stage: Record<string, number> };
  campaigns: { total: number; running: number; sent: number };
  tasks: { queued: number; running: number; failed: number; completed: number };
  approvals_pending: number;
  audit: Array<{ ts: number; event: string; detail: string }>;
  report: string;
}

interface Product {
  id: string;
  name: string;
  description: string;
  target_audience: string[];
  industries: string[];
  verified_capabilities: string[];
  problems_solved: string[];
  integration_requirements: string;
  operational_status: string;
  route: string;
  pricing: string;
  demo: string;
}

interface IntentResponse {
  intent: string;
  parser: string;
  task_id: string;
  status: string;
  reply: string;
}

/* ── constants ────────────────────────────────────────────────────────────── */

const LEAD_STAGES = [
  "new", "researching", "qualified", "ready_for_outreach",
  "contacted", "replied", "interested", "meeting_requested",
  "proposal_sent", "converted", "not_interested", "suppressed",
];

const STAGE_META: Record<string, { label: string; color: string; bg: string }> = {
  new: { label: "New", color: "#9ca3af", bg: "rgba(156,163,175,0.1)" },
  researching: { label: "Researching", color: "#22d3ee", bg: "rgba(34,211,238,0.12)" },
  qualified: { label: "Qualified", color: "#34d399", bg: "rgba(52,211,153,0.12)" },
  ready_for_outreach: { label: "Ready for Outreach", color: "#a78bfa", bg: "rgba(167,139,250,0.14)" },
  contacted: { label: "Contacted", color: "#fb923c", bg: "rgba(251,146,60,0.12)" },
  replied: { label: "Replied", color: "#60a5fa", bg: "rgba(96,165,250,0.12)" },
  interested: { label: "Interested", color: "#fbbf24", bg: "rgba(251,191,36,0.14)" },
  meeting_requested: { label: "Meeting Requested", color: "#f472b6", bg: "rgba(244,114,182,0.14)" },
  proposal_sent: { label: "Proposal Sent", color: "#c084fc", bg: "rgba(192,132,252,0.14)" },
  converted: { label: "Converted", color: "#22c55e", bg: "rgba(34,197,94,0.14)" },
  not_interested: { label: "Not Interested", color: "#ef4444", bg: "rgba(239,68,68,0.1)" },
  suppressed: { label: "Suppressed", color: "#6b7280", bg: "rgba(107,114,128,0.1)" },
};

const TASK_STATUS_META: Record<string, { label: string; color: string }> = {
  queued: { label: "Queued", color: "#9ca3af" },
  running: { label: "Running", color: "#22d3ee" },
  completed: { label: "Completed", color: "#34d399" },
  failed: { label: "Failed", color: "#f87171" },
  cancelled: { label: "Cancelled", color: "#9ca3af" },
  awaiting_approval: { label: "Awaiting Approval", color: "#fbbf24" },
};

const TERMINAL_TASK_STATES = new Set(["completed", "failed", "cancelled"]);

/* ── CSS-in-JS (scoped to this page) ──────────────────────────────────────── */

const CSS = `
/* Layout */
.ss-root { display: grid; grid-template-columns: var(--ss-sidebar-w, 360px) 1fr var(--ss-panel-w, 380px); height: 100vh; min-height: 100vh; background: #0b0f14; color: #e8edf2; font-family: inherit; transition: grid-template-columns 0.25s ease; }

/* Full-screen chat mode: sidebar + panel collapsed */
.ss-root.full-chat { grid-template-columns: 0 1fr 0; }
.ss-root.full-chat .ss-sidebar,
.ss-root.full-chat .ss-panel { display: none; }
.ss-root.full-chat .ss-main { border-radius: 0; }

@media (max-width: 1200px) { .ss-root { grid-template-columns: var(--ss-sidebar-w, 360px) 1fr; } }
@media (max-width: 1024px) { 
  .ss-root { grid-template-columns: 1fr; }
  .ss-root .ss-sidebar { position: fixed; left: 0; top: 0; bottom: 0; z-index: 50; width: 360px; max-width: 100vw; transform: translateX(var(--ss-sidebar-x, -100%)); transition: transform 0.2s ease; }
  .ss-root .ss-sidebar.open { transform: translateX(0); }
  .ss-root .ss-panel { position: fixed; right: 0; top: 0; bottom: 0; z-index: 40; width: 380px; max-width: 100vw; transform: translateX(var(--ss-panel-x, 100%)); transition: transform 0.2s ease; }
  .ss-root .ss-panel.open { transform: translateX(0); }
}

/* Sidebar - Conversation List */
.ss-sidebar { border-right: 1px solid #1e293b; display: flex; flex-direction: column; background: #0f172a; overflow: hidden; min-width: 0; }
.ss-sidebar-header { padding: 16px; border-bottom: 1px solid #1e293b; display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-shrink: 0; }
.ss-sidebar-title { font-size: 14px; font-weight: 600; color: #e8edf2; }
.ss-sidebar-subtitle { font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; }
.ss-new-conv { padding: 12px 16px; border-bottom: 1px solid #1e293b; flex-shrink: 0; }
.ss-new-btn { width: 100%; display: flex; align-items: center; gap: 10px; padding: 10px 12px; background: #1e293b; border: 1px solid #334155; border-radius: 8px; color: #e8edf2; font-size: 13px; font-weight: 500; cursor: pointer; transition: all 0.15s; }
.ss-new-btn:hover { background: #334155; border-color: #475569; }
.ss-conv-list { flex: 1; overflow-y: auto; padding: 8px; min-height: 0; }
.ss-conv-item { position: relative; display: flex; align-items: flex-start; gap: 10px; padding: 10px 12px; border-radius: 8px; cursor: pointer; transition: background 0.1s; }
.ss-conv-item:hover { background: #1e293b; }
.ss-conv-item.active { background: #1e3a5f; border: 1px solid #3b82f6; }
.ss-conv-avatar { width: 36px; height: 36px; border-radius: 50%; background: linear-gradient(135deg, #3b82f6, #8b5cf6); display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 13px; color: white; flex-shrink: 0; }
.ss-conv-content { flex: 1; min-width: 0; }
.ss-conv-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 4px; }
.ss-conv-name { font-size: 13px; font-weight: 500; color: #e8edf2; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ss-conv-time { font-size: 10px; color: #64748b; white-space: nowrap; }
.ss-conv-preview { font-size: 12px; color: #94a3b8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ss-conv-unread { width: 8px; height: 8px; border-radius: 50%; background: #3b82f6; flex-shrink: 0; margin-top: 3px; }
.ss-conv-actions { opacity: 0; display: flex; gap: 4px; padding: 4px; transition: opacity 0.15s; position: absolute; right: 8px; top: 50%; transform: translateY(-50%); }
.ss-conv-item:hover .ss-conv-actions { opacity: 1; }
.ss-conv-action-btn { width: 24px; height: 24px; border-radius: 4px; background: #1e293b; border: 1px solid #334155; color: #94a3b8; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: all 0.15s; font-size: 11px; }
.ss-conv-action-btn:hover { background: #334155; color: #e8edf2; border-color: #475569; }
.ss-conv-action-btn.delete:hover { background: #7f1d1d; border-color: #ef4444; color: #fca5a5; }
.ss-empty-conv { padding: 24px 16px; text-align: center; color: #64748b; font-size: 13px; }

/* Main Chat Area */
.ss-main { display: flex; flex-direction: column; background: #0b0f14; overflow: hidden; border-radius: 0; }
.ss-main-header { padding: 14px 20px; border-bottom: 1px solid #1e293b; background: #0f172a; display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-shrink: 0; }
.ss-main-title-area { display: flex; align-items: center; gap: 12px; flex: 1; min-width: 0; }
.ss-main-avatar { width: 36px; height: 36px; border-radius: 50%; background: linear-gradient(135deg, #3b82f6, #8b5cf6); display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 13px; color: white; flex-shrink: 0; }
.ss-main-title { font-size: 14px; font-weight: 600; color: #e8edf2; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ss-main-status { font-size: 11px; color: #64748b; }
.ss-main-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.ss-icon-btn { width: 36px; height: 36px; border-radius: 8px; background: #1e293b; border: 1px solid #334155; color: #94a3b8; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: all 0.15s; }
.ss-icon-btn:hover { background: #334155; color: #e8edf2; border-color: #475569; }
.ss-icon-btn.primary { background: linear-gradient(135deg, #3b82f6, #8b5cf6); border-color: transparent; color: white; }
.ss-icon-btn.primary:hover { box-shadow: 0 4px 16px rgba(59,130,246,0.4); }
.ss-status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.ss-status-dot.connected { background: #22c55e; box-shadow: 0 0 8px #22c55e80; }
.ss-status-dot.disconnected { background: #ef4444; }
.ss-status-dot.pending { background: #fbbf24; }

/* Messages Area */
.ss-messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px; min-height: 0; }
.ss-message { display: flex; gap: 10px; max-width: 85%; animation: ss-fade-in 0.2s ease; }
@keyframes ss-fade-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
.ss-message.user { align-self: flex-end; flex-direction: row-reverse; }
.ss-message.assistant { align-self: flex-start; }
.ss-message-avatar { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 11px; color: white; flex-shrink: 0; margin-top: 2px; }
.ss-message.user .ss-message-avatar { background: linear-gradient(135deg, #3b82f6, #8b5cf6); }
.ss-message.assistant .ss-message-avatar { background: linear-gradient(135deg, #8b5cf6, #ec4899); }
.ss-message-bubble { padding: 12px 16px; border-radius: 16px; font-size: 13.5px; line-height: 1.55; position: relative; }
.ss-message.user .ss-message-bubble { background: #1e3a5f; border: 1px solid #3b82f640; color: #e8edf2; border-bottom-right-radius: 4px; }
.ss-message.assistant .ss-message-bubble { background: #1e293b; border: 1px solid #334155; color: #e8edf2; border-bottom-left-radius: 4px; }
.ss-message-text { white-space: pre-wrap; word-wrap: break-word; }
.ss-message-meta { display: flex; align-items: center; gap: 8px; margin-top: 6px; font-size: 10px; color: #64748b; }
.ss-message-time { white-space: nowrap; }
.ss-status-badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 999px; font-size: 10px; font-weight: 500; }
.ss-status-badge.running { background: rgba(34,211,238,0.15); color: #22d3ee; }
.ss-status-badge.completed { background: rgba(52,211,153,0.15); color: #34d399; }
.ss-status-badge.failed { background: rgba(248,113,113,0.15); color: #f87171; }
.ss-status-badge.awaiting_approval { background: rgba(251,191,36,0.15); color: #fbbf24; }
.ss-status-badge.needs_input { background: rgba(167,139,250,0.15); color: #a78bfa; }
.ss-task-progress { font-size: 11px; color: #94a3b8; margin-top: 8px; padding: 8px 12px; background: #0f172a; border-radius: 8px; border: 1px solid #1e293b; }
.ss-task-step { display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 11px; }
.ss-task-step-icon { width: 14px; height: 14px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 8px; flex-shrink: 0; }
.ss-task-step.pending .ss-task-step-icon { background: #334155; color: #64748b; }
.ss-task-step.running .ss-task-step-icon { background: #22d3ee; color: #0b0f14; animation: ss-pulse 1.5s infinite; }
@keyframes ss-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
.ss-task-step.succeeded .ss-task-step-icon { background: #34d399; color: #0b0f14; }
.ss-task-step.failed .ss-task-step-icon { background: #f87171; color: white; }
.ss-task-step-label { color: #cbd5e1; }
.ss-task-step.pending .ss-task-step-label { color: #64748b; }
.ss-task-step.running .ss-task-step-label { color: #22d3ee; font-weight: 500; }
.ss-task-step.succeeded .ss-task-step-label { color: #34d399; }
.ss-task-step.failed .ss-task-step-label { color: #f87171; }

/* Composer */
.ss-composer { padding: 16px 20px; border-top: 1px solid #1e293b; background: #0f172a; flex-shrink: 0; }
.ss-composer-row { display: flex; align-items: flex-end; gap: 10px; }
.ss-composer-input-wrap { flex: 1; position: relative; }
.ss-composer-input { width: 100%; min-height: 48px; max-height: 160px; padding: 12px 48px 12px 16px; background: #0b0f14; border: 1px solid #334155; border-radius: 12px; color: #e8edf2; font-size: 13.5px; line-height: 1.5; resize: none; outline: none; transition: border-color 0.15s; font-family: inherit; }
.ss-composer-input:focus { border-color: #3b82f6; }
.ss-composer-input::placeholder { color: #64748b; }
.ss-composer-send { width: 44px; height: 44px; border-radius: 10px; background: linear-gradient(135deg, #3b82f6, #8b5cf6); border: none; color: white; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: transform 0.1s, box-shadow 0.15s; flex-shrink: 0; }
.ss-composer-send:hover { transform: scale(1.02); box-shadow: 0 4px 16px rgba(59,130,246,0.4); }
.ss-composer-send:active { transform: scale(0.98); }
.ss-composer-send:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.ss-composer-hint { font-size: 11px; color: #64748b; margin-top: 6px; padding-left: 4px; }

/* Right Panel - Task Details */
.ss-panel { border-left: 1px solid #1e293b; background: #0f172a; display: flex; flex-direction: column; overflow: hidden; min-width: 0; flex-shrink: 0; }
.ss-panel-header { padding: 14px 16px; border-bottom: 1px solid #1e293b; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
.ss-panel-title { font-size: 13px; font-weight: 600; color: #e8edf2; }
.ss-panel-close { width: 32px; height: 32px; border-radius: 8px; background: transparent; border: 1px solid #334155; color: #94a3b8; display: flex; align-items: center; justify-content: center; cursor: pointer; }
.ss-panel-close:hover { background: #1e293b; color: #e8edf2; }
.ss-panel-content { flex: 1; overflow-y: auto; padding: 16px; min-height: 0; }
.ss-panel-empty { text-align: center; color: #64748b; padding: 40px 20px; }
.ss-panel-section { margin-bottom: 20px; }
.ss-panel-section-title { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #64748b; margin-bottom: 10px; }
.ss-panel-card { background: #111827; border: 1px solid #1e293b; border-radius: 10px; padding: 14px; margin-bottom: 10px; }
.ss-panel-card-title { font-size: 12px; font-weight: 600; color: #e8edf2; margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
.ss-panel-card-meta { font-size: 11px; color: #94a3b8; margin-bottom: 10px; }
.ss-panel-progress { margin-top: 10px; }
.ss-kv { display: flex; justify-content: space-between; font-size: 12px; padding: 4px 0; }
.ss-kv-key { color: #94a3b8; }
.ss-kv-value { color: #e8edf2; font-weight: 500; }
.ss-panel-list { display: flex; flex-direction: column; gap: 6px; }
.ss-panel-list-item { display: flex; align-items: center; justify-content: space-between; padding: 8px 10px; background: #111827; border: 1px solid #1e293b; border-radius: 8px; font-size: 12px; }
.ss-panel-list-item-name { color: #e8edf2; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 180px; }
.ss-panel-list-item-stage { padding: 2px 8px; border-radius: 999px; font-size: 10px; font-weight: 500; }
.ss-panel-list-item-score { font-size: 11px; color: #fbbf24; font-weight: 600; }

/* Scrollbars */
.ss-messages::-webkit-scrollbar, .ss-conv-list::-webkit-scrollbar, .ss-panel-content::-webkit-scrollbar { width: 6px; }
.ss-messages::-webkit-scrollbar-track, .ss-conv-list::-webkit-scrollbar-track, .ss-panel-content::-webkit-scrollbar-track { background: transparent; }
.ss-messages::-webkit-scrollbar-thumb, .ss-conv-list::-webkit-scrollbar-thumb, .ss-panel-content::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
.ss-messages::-webkit-scrollbar-thumb:hover, .ss-conv-list::-webkit-scrollbar-thumb:hover, .ss-panel-content::-webkit-scrollbar-thumb:hover { background: #475569; }

/* Code blocks in messages */
.ss-message-bubble pre { background: #0b0f14; border: 1px solid #1e293b; border-radius: 8px; padding: 12px; font-size: 11.5px; line-height: 1.55; overflow-x: auto; margin-top: 8px; color: #cbd5e1; }
.ss-message-bubble code { font-family: ui-monospace, SFMono-Regular, monospace; }

/* Full-screen chat toggle button */
.ss-fullscreen-btn { width: 36px; height: 36px; border-radius: 8px; background: #1e293b; border: 1px solid #334155; color: #94a3b8; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: all 0.15s; }
.ss-fullscreen-btn:hover { background: #334155; color: #e8edf2; border-color: #475569; }
.ss-fullscreen-btn.active { background: linear-gradient(135deg, #3b82f6, #8b5cf6); border-color: transparent; color: white; }
`;

/* ── helpers ──────────────────────────────────────────────────────────────── */

function formatTime(ts: number): string {
  const d = new Date(ts);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
    d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatRelative(ts: number): string {
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return `${Math.max(s, 0)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function getInitials(name: string): string {
  return name.split(" ").map(w => w[0]).join("").slice(0, 2).toUpperCase();
}

/* ── components ───────────────────────────────────────────────────────────── */

function MessageBubble({
  msg,
  isLast,
}: { msg: SalesMessage; isLast: boolean }) {
  const isUser = msg.role === "user";
  const statusMeta = msg.status ? TASK_STATUS_META[msg.status] : null;

  return (
    <div className={`ss-message ${isUser ? "user" : "assistant"}`}>
      <div className="ss-message-avatar" aria-hidden="true">
        {isUser ? "U" : "S"}
      </div>
      <div className="ss-message-bubble">
        <div className="ss-message-text">{msg.text}</div>
        <div className="ss-message-meta">
          <span className="ss-message-time">{formatTime(msg.ts)}</span>
          {!isUser && msg.status && statusMeta && (
            <span className="ss-status-badge" style={{ background: `${statusMeta.color}26`, color: statusMeta.color }}>
              {statusMeta.label}
            </span>
          )}
          {!isUser && msg.intent && (
            <span className="ss-status-badge" style={{ background: "rgba(167,139,250,0.15)", color: "#a78bfa" }}>
              {msg.intent}
            </span>
          )}
        </div>
        {isLast && msg.task_id && msg.status === "running" && (
          <TaskProgressPanel taskId={msg.task_id} />
        )}
      </div>
    </div>
  );
}

function TaskProgressPanel({ taskId }: { taskId: string }) {
  const [task, setTask] = useState<SalesTask | null>(null);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const res = await saraFetch(`${SARA_API_URL}/api/sales/tasks/${taskId}`, { cache: "no-store" });
        if (res.ok) {
          const data = await res.json();
          if (alive) setTask(data);
        }
      } catch { /* ignore */ }
    };
    poll();
    const intv = setInterval(poll, 2000);
    return () => { alive = false; clearInterval(intv); };
  }, [taskId]);

  if (!task) return null;

  return (
    <div className="ss-task-progress">
      {task.plan.map((step, i) => (
        <div key={i} className={`ss-task-step ${step.status}`}>
          <span className="ss-task-step-icon">
            {step.status === "succeeded" && "✓"}
            {step.status === "failed" && "✕"}
            {step.status === "running" && "⟳"}
            {step.status === "pending" && "○"}
          </span>
          <span className="ss-task-step-label">
            {step.title}
            {step.detail && <span style={{ color: "#64748b", marginLeft: "8px" }}>{step.detail}</span>}
          </span>
        </div>
      ))}
    </div>
  );
}

function ConversationItem({
  conv,
  isActive,
  onClick,
  onDelete,
}: { conv: SalesConversation; isActive: boolean; onClick: () => void; onDelete: (e: React.MouseEvent) => void }) {
  const lastMsg = conv.messages[conv.messages.length - 1];
  const unread = false;

  return (
    <button
      className={`ss-conv-item ${isActive ? "active" : ""}`}
      onClick={onClick}
      aria-pressed={isActive}
    >
      <div className="ss-conv-avatar">{getInitials(conv.title)}</div>
      <div className="ss-conv-content">
        <div className="ss-conv-header">
          <span className="ss-conv-name">{conv.title}</span>
          <span className="ss-conv-time">{formatRelative(conv.updated_at)}</span>
        </div>
        <div className="ss-conv-preview">
          {lastMsg ? (lastMsg.role === "user" ? "You: " : "SARA: ") + lastMsg.text.slice(0, 60) : "No messages yet"}
        </div>
      </div>
      {unread && <div className="ss-conv-unread" />}
      <div className="ss-conv-actions">
        <button
          className="ss-conv-action-btn delete"
          onClick={(e) => { e.stopPropagation(); onDelete(e); }}
          aria-label="Delete conversation"
          title="Delete conversation"
        >
          🗑
        </button>
      </div>
    </button>
  );
}

function SidePanel({ task, lead, campaign, listing, onClose }: {
  task?: SalesTask;
  lead?: SalesLead;
  campaign?: SalesCampaign;
  listing?: SalesListing;
  onClose: () => void;
}) {
  if (!task && !lead && !campaign && !listing) {
    return (
      <div className="ss-panel" style={{ width: 0, border: "none" }}>
        <div className="ss-panel-header">
          <span className="ss-panel-title">Details</span>
          <button className="ss-panel-close" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="ss-panel-content">
          <div className="ss-panel-empty">Select a task, lead, campaign, or listing to see details</div>
        </div>
      </div>
    );
  }

  return (
    <div className="ss-panel">
      <div className="ss-panel-header">
        <span className="ss-panel-title">
          {task ? "Task Details" : lead ? "Lead Details" : campaign ? "Campaign Details" : "Listing Details"}
        </span>
        <button className="ss-panel-close" onClick={onClose} aria-label="Close">✕</button>
      </div>
      <div className="ss-panel-content">
        {task && (
          <>
            <div className="ss-panel-section">
              <div className="ss-panel-section-title">Execution Plan</div>
              <div className="ss-panel-card">
                <div className="ss-panel-card-title">
                  {task.title}
                  <span className="ss-status-badge" style={{ background: `${TASK_STATUS_META[task.status]?.color}26`, color: TASK_STATUS_META[task.status]?.color }}>
                    {TASK_STATUS_META[task.status]?.label}
                  </span>
                </div>
                <div className="ss-panel-card-meta">
                  Kind: {task.kind} • Created {formatRelative(task.created_at)}
                </div>
                {task.plan.map((step, i) => (
                  <div key={i} className={`ss-task-step ${step.status}`} style={{ padding: "6px 0", borderRadius: 0, background: "transparent" }}>
                    <span className="ss-task-step-icon">
                      {step.status === "succeeded" && "✓"}
                      {step.status === "failed" && "✕"}
                      {step.status === "running" && "⟳"}
                      {step.status === "pending" && "○"}
                    </span>
                    <span className="ss-task-step-label">
                      {step.title}
                      {step.detail && <span style={{ color: "#64748b", marginLeft: "8px" }}>{step.detail}</span>}
                    </span>
                  </div>
                ))}
                {task.result && (
                  <div style={{ marginTop: "12px", paddingTop: "12px", borderTop: "1px solid #1e293b" }}>
                    <div className="ss-kv"><span className="ss-kv-key">Result</span><span className="ss-kv-value">{task.result.slice(0, 200)}...</span></div>
                  </div>
                )}
                {task.error && (
                  <div style={{ marginTop: "12px", paddingTop: "12px", borderTop: "1px solid #1e293b" }}>
                    <div className="ss-kv"><span className="ss-kv-key" style={{ color: "#f87171" }}>Error</span><span className="ss-kv-value" style={{ color: "#f87171" }}>{task.error}</span></div>
                  </div>
                )}
              </div>
            </div>
          </>
        )}
        {lead && (
          <>
            <div className="ss-panel-section">
              <div className="ss-panel-section-title">Lead Information</div>
              <div className="ss-panel-card">
                <div className="ss-panel-card-title">{lead.name}</div>
                <div className="ss-panel-card-meta">{lead.industry} • {lead.region}</div>
                <div className="ss-kv"><span className="ss-kv-key">Domain</span><span className="ss-kv-value">{lead.domain || "—"}</span></div>
                <div className="ss-kv"><span className="ss-kv-key">Website</span><span className="ss-kv-value"><a href={lead.website} target="_blank" rel="noopener noreferrer" className="ss-link">{lead.website || "—"}</a></span></div>
                <div className="ss-kv"><span className="ss-kv-key">Email</span><span className="ss-kv-value">{lead.email || "—"}</span></div>
                <div className="ss-kv"><span className="ss-kv-key">Phone</span><span className="ss-kv-value">{lead.phone || "—"}</span></div>
                <div className="ss-kv"><span className="ss-kv-key">Source</span><span className="ss-kv-value"><a href={lead.source_url} target="_blank" rel="noopener noreferrer" className="ss-link">OpenStreetMap</a></span></div>
              </div>
            </div>
            <div className="ss-panel-section">
              <div className="ss-panel-section-title">Qualification</div>
              <div className="ss-panel-card">
                <div className="ss-kv"><span className="ss-kv-key">Stage</span>
                  <span className="ss-panel-list-item-stage" style={{ background: STAGE_META[lead.stage]?.bg, color: STAGE_META[lead.stage]?.color }}>
                    {STAGE_META[lead.stage]?.label || lead.stage}
                  </span>
                </div>
                <div className="ss-kv"><span className="ss-kv-key">Score</span><span className="ss-kv-value" style={{ color: "#fbbf24" }}>{lead.score}/100</span></div>
                <div style={{ marginTop: "10px" }}>
                  <div className="ss-kv"><span className="ss-kv-key">Reasons</span></div>
                  {lead.score_reasons.map((r, i) => (
                    <div key={i} className="ss-kv" style={{ paddingLeft: "16px", fontSize: "11px" }}>
                      <span className="ss-kv-value" style={{ color: "#94a3b8" }}>{r}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
            {lead.profile && Object.keys(lead.profile).length > 0 && (
              <div className="ss-panel-section">
                <div className="ss-panel-section-title">Website Profile</div>
                <div className="ss-panel-card">
                  <div className="ss-kv"><span className="ss-kv-key">Reachable</span><span className="ss-kv-value">{(lead.profile as Record<string, unknown>).reachable ? "Yes" : "No"}</span></div>
                  <div className="ss-kv"><span className="ss-kv-key">Tech Stack</span><span className="ss-kv-value">{((lead.profile as Record<string, unknown>).tech as string[])?.join(", ") || "—"}</span></div>
                  <div className="ss-kv"><span className="ss-kv-key">Emails Found</span><span className="ss-kv-value">{((lead.profile as Record<string, unknown>).emails as string[])?.join(", ") || "—"}</span></div>
                  <div className="ss-kv"><span className="ss-kv-key">Phones Found</span><span className="ss-kv-value">{((lead.profile as Record<string, unknown>).phones as string[])?.join(", ") || "—"}</span></div>
                  <div className="ss-kv"><span className="ss-kv-key">Has Schema.org</span><span className="ss-kv-value">{(lead.profile as Record<string, unknown>).has_schema ? "Yes" : "No"}</span></div>
                  <div className="ss-kv"><span className="ss-kv-key">Booking Hint</span><span className="ss-kv-value">{(lead.profile as Record<string, unknown>).has_booking_hint ? "Yes" : "No"}</span></div>
                  {(lead.profile as Record<string, unknown>).error ? (
                    <div className="ss-kv"><span className="ss-kv-key" style={{ color: "#f87171" }}>Error</span><span className="ss-kv-value" style={{ color: "#f87171" }}>{String((lead.profile as Record<string, unknown>).error)}</span></div>
                  ) : null}
                </div>
              </div>
            )}
          </>
        )}
        {campaign && (
          <>
            <div className="ss-panel-section">
              <div className="ss-panel-section-title">Campaign</div>
              <div className="ss-panel-card">
                <div className="ss-panel-card-title">Email Automation Campaign</div>
                <div className="ss-panel-card-meta">EA ID: {campaign.ea_campaign_id}</div>
                <div className="ss-kv"><span className="ss-kv-key">Status</span>
                  <span className="ss-status-badge" style={{ background: `${TASK_STATUS_META[campaign.status]?.color}26`, color: TASK_STATUS_META[campaign.status]?.color }}>
                    {TASK_STATUS_META[campaign.status]?.label}
                  </span>
                </div>
                <div className="ss-kv"><span className="ss-kv-key">Recipients</span><span className="ss-kv-value">{campaign.recipients}</span></div>
                <div className="ss-kv"><span className="ss-kv-key">Sent</span><span className="ss-kv-value" style={{ color: "#34d399" }}>{campaign.sent}</span></div>
                <div className="ss-kv"><span className="ss-kv-key">Failed</span><span className="ss-kv-value" style={{ color: "#f87171" }}>{campaign.failed}</span></div>
              </div>
            </div>
          </>
        )}
        {listing && (
          <>
            <div className="ss-panel-section">
              <div className="ss-panel-section-title">Listing Draft</div>
              <div className="ss-panel-card">
                <div className="ss-panel-card-title">Product Listing Draft</div>
                <div className="ss-panel-card-meta">Status: {listing.status}</div>
                <pre style={{ maxHeight: "300px", overflow: "auto" }}>{listing.body}</pre>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* ── main page ────────────────────────────────────────────────────────────── */

function SaraSalesPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [conversations, setConversations] = useState<SalesConversation[]>([]);
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [messages, setMessages] = useState<SalesMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [composerText, setComposerText] = useState("");
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [products, setProducts] = useState<Product[]>([]);
  const [leads, setLeads] = useState<SalesLead[]>([]);
  const [campaigns, setCampaigns] = useState<SalesCampaign[]>([]);
  const [listings, setListings] = useState<SalesListing[]>([]);
  const [tasks, setTasks] = useState<SalesTask[]>([]);
  const [panelTask, setPanelTask] = useState<SalesTask | undefined>(undefined);
  const [panelLead, setPanelLead] = useState<SalesLead | undefined>(undefined);
  const [panelCampaign, setPanelCampaign] = useState<SalesCampaign | undefined>(undefined);
  const [panelListing, setPanelListing] = useState<SalesListing | undefined>(undefined);
  const [showSidebar, setShowSidebar] = useState(true);
  const [showPanel, setShowPanel] = useState(false);
  const [fullChat, setFullChat] = useState(false);
  const [error, setError] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const activeConv = useMemo(
    () => conversations.find(c => c.id === activeConvId) || null,
    [conversations, activeConvId]
  );

  // Load initial data
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [dashRes, prodRes, convRes] = await Promise.all([
          saraFetch(`${SARA_API_URL}/api/sales/dashboard`, { cache: "no-store" }),
          saraFetch(`${SARA_API_URL}/api/sales/products`, { cache: "no-store" }),
          saraFetch(`${SARA_API_URL}/api/sales/conversations`, { cache: "no-store" }),
        ]);
        if (!alive) return;
        if (dashRes.ok) setDashboard(await dashRes.json());
        if (prodRes.ok) setProducts((await prodRes.json()).products);
        if (convRes.ok) {
          const data = await convRes.json();
          const convs = data.conversations || [];
          setConversations(convs);
          // Select conversation from URL or latest
          const urlConvId = searchParams.get("conv");
          const targetId = urlConvId || (convs[0]?.id);
          if (targetId) {
            setActiveConvId(targetId);
            await loadConversation(targetId);
          }
        }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (alive) setLoading(false);
      }
    };
    load();
    return () => { alive = false; };
  }, [searchParams]);

  // Poll for updates when a task is running
  useEffect(() => {
    const hasRunningTask = messages.some(m => m.status === "running" && m.task_id);
    if (hasRunningTask) {
      pollingRef.current = setInterval(async () => {
        try {
          const [dashRes, tasksRes, leadsRes, campsRes, listingsRes] = await Promise.all([
            saraFetch(`${SARA_API_URL}/api/sales/dashboard`, { cache: "no-store" }),
            saraFetch(`${SARA_API_URL}/api/sales/tasks?limit=50`, { cache: "no-store" }),
            saraFetch(`${SARA_API_URL}/api/sales/leads`, { cache: "no-store" }),
            saraFetch(`${SARA_API_URL}/api/sales/campaigns`, { cache: "no-store" }),
            saraFetch(`${SARA_API_URL}/api/sales/listings`, { cache: "no-store" }),
          ]);
          if (dashRes.ok) setDashboard(await dashRes.json());
          if (tasksRes.ok) setTasks((await tasksRes.json()).tasks);
          if (leadsRes.ok) setLeads((await leadsRes.json()).leads);
          if (campsRes.ok) setCampaigns((await campsRes.json()).campaigns);
          if (listingsRes.ok) setListings((await listingsRes.json()).listings);

          // Refresh active conversation messages
          if (activeConvId) {
            const convRes = await saraFetch(`${SARA_API_URL}/api/sales/conversations/${activeConvId}`, { cache: "no-store" });
            if (convRes.ok) {
              const conv = await convRes.json();
              setMessages(conv.messages || []);
            }
          }
        } catch { /* ignore */ }
      }, 3000);
    } else if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
    return () => { if (pollingRef.current) clearInterval(pollingRef.current); };
  }, [messages, activeConvId]);

  const loadConversation = useCallback(async (convId: string) => {
    try {
      const res = await saraFetch(`${SARA_API_URL}/api/sales/conversations/${convId}`, { cache: "no-store" });
      if (res.ok) {
        const conv = await res.json();
        setMessages(conv.messages || []);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  const handleSend = useCallback(async () => {
    if (!composerText.trim() || !activeConvId || sending) return;
    const text = composerText.trim();
    setComposerText("");
    setSending(true);
    setError("");

    try {
      const res = await saraFetch(`${SARA_API_URL}/api/sales/conversations/${activeConvId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, attachment_ids: [] }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const result = await res.json() as IntentResponse;
      // Reload conversation to get the new messages
      await loadConversation(activeConvId);
      // Refresh dashboard and related data
      const [dashRes, tasksRes, leadsRes, campsRes, listingsRes] = await Promise.all([
        saraFetch(`${SARA_API_URL}/api/sales/dashboard`, { cache: "no-store" }),
        saraFetch(`${SARA_API_URL}/api/sales/tasks?limit=50`, { cache: "no-store" }),
        saraFetch(`${SARA_API_URL}/api/sales/leads`, { cache: "no-store" }),
        saraFetch(`${SARA_API_URL}/api/sales/campaigns`, { cache: "no-store" }),
        saraFetch(`${SARA_API_URL}/api/sales/listings`, { cache: "no-store" }),
      ]);
      if (dashRes.ok) setDashboard(await dashRes.json());
      if (tasksRes.ok) setTasks((await tasksRes.json()).tasks);
      if (leadsRes.ok) setLeads((await leadsRes.json()).leads);
      if (campsRes.ok) setCampaigns((await campsRes.json()).campaigns);
      if (listingsRes.ok) setListings((await listingsRes.json()).listings);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  }, [composerText, activeConvId, sending, loadConversation]);

  const handleNewConversation = useCallback(async () => {
    try {
      const res = await saraFetch(`${SARA_API_URL}/api/sales/conversations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (res.ok) {
        const conv = await res.json();
        setConversations(prev => [conv, ...prev]);
        setActiveConvId(conv.id);
        setMessages([]);
        router.push(`/hub/sara-sales?conv=${conv.id}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [router]);

  const handleConvSelect = useCallback((convId: string) => {
    setActiveConvId(convId);
    loadConversation(convId);
    router.push(`/hub/sara-sales?conv=${convId}`);
  }, [loadConversation, router]);

  const handleDeleteConversation = useCallback(async (convId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!window.confirm("Delete this conversation? This cannot be undone.")) return;
    
    try {
      const res = await saraFetch(`${SARA_API_URL}/api/sales/conversations/${convId}`, {
        method: "DELETE",
      });
      if (res.ok) {
        setConversations(prev => prev.filter(c => c.id !== convId));
        if (activeConvId === convId) {
          const next = conversations.find(c => c.id !== convId);
          if (next) {
            setActiveConvId(next.id);
            await loadConversation(next.id);
            router.push(`/hub/sara-sales?conv=${next.id}`);
          } else {
            setActiveConvId(null);
            setMessages([]);
            router.push(`/hub/sara-sales`);
          }
        }
      } else {
        throw new Error("Failed to delete conversation");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [activeConvId, conversations, loadConversation, router]);

  const handleOpenTask = useCallback((task: SalesTask) => {
    setPanelTask(task);
    setPanelLead(undefined);
    setPanelCampaign(undefined);
    setPanelListing(undefined);
    setShowPanel(true);
  }, []);

  const handleOpenLead = useCallback((lead: SalesLead) => {
    setPanelLead(lead);
    setPanelTask(undefined);
    setPanelCampaign(undefined);
    setPanelListing(undefined);
    setShowPanel(true);
  }, []);

  const handleOpenCampaign = useCallback((campaign: SalesCampaign) => {
    setPanelCampaign(campaign);
    setPanelTask(undefined);
    setPanelLead(undefined);
    setPanelListing(undefined);
    setShowPanel(true);
  }, []);

  const handleOpenListing = useCallback((listing: SalesListing) => {
    setPanelListing(listing);
    setPanelTask(undefined);
    setPanelLead(undefined);
    setPanelCampaign(undefined);
    setShowPanel(true);
  }, []);

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  if (loading) {
    return (
      <div className="ss-root" style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
        <div style={{ textAlign: "center", color: "#64748b" }}>
          <div style={{ fontSize: "18px", marginBottom: "8px" }}>Loading Sales Command Center…</div>
          <div style={{ fontSize: "12px" }}>Connecting to SARA Sales Agent backend</div>
        </div>
      </div>
    );
  }

  const rootStyle: React.CSSProperties = {
    "--ss-sidebar-x": showSidebar ? "0" : "-100%",
    "--ss-panel-x": showPanel ? "0" : "100%",
  } as React.CSSProperties;

  return (
    <>
      <style>{CSS}</style>
      <div className={`ss-root ${fullChat ? "full-chat" : ""}`} style={rootStyle}>
        {/* Sidebar - Conversation List */}
        <aside className={`ss-sidebar ${showSidebar ? "open" : ""}`} aria-label="Conversations">
          <div className="ss-sidebar-header">
            <div style={{ width: "32px", height: "32px", borderRadius: "8px", background: "linear-gradient(135deg,#3b82f6,#8b5cf6)", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: "700", fontSize: "14px", color: "white" }}>S</div>
            <div>
              <div className="ss-sidebar-title">Sales Command Center</div>
              <div className="ss-sidebar-subtitle">SARA Autonomous AI Sales Agent</div>
            </div>
          </div>
          <div className="ss-new-conv">
            <button className="ss-new-btn" onClick={handleNewConversation}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>
              New conversation
            </button>
          </div>
          <nav className="ss-conv-list" aria-label="Conversation history">
            {conversations.length === 0 ? (
              <div className="ss-empty-conv">
                No conversations yet.<br />
                <button className="ss-new-btn" style={{ marginTop: "12px", width: "auto" }} onClick={handleNewConversation}>Start one</button>
              </div>
            ) : (
              conversations.map(conv => (
                <ConversationItem
                  key={conv.id}
                  conv={conv}
                  isActive={activeConvId === conv.id}
                  onClick={() => handleConvSelect(conv.id)}
                  onDelete={(e) => handleDeleteConversation(conv.id, e)}
                />
              ))
            )}
          </nav>
        </aside>

        {/* Main Chat Area */}
        <main className="ss-main">
          <header className="ss-main-header">
            <div className="ss-main-title-area">
              <div className="ss-main-avatar">S</div>
              <div>
                <div className="ss-main-title">
                  {activeConv?.title || "Select a conversation"}
                </div>
                <div className="ss-main-status">
                  {dashboard ? (
                    <>
                      <span className={`ss-status-dot ${dashboard.worker.emergency_stop ? "disconnected" : dashboard.integrations.llm?.connected ? "connected" : "pending"}`} />
                      {dashboard.worker.emergency_stop ? "Emergency Stop ON" : dashboard.worker.mode === "continuous" ? "Continuous mode" : "Ready"}
                    </>
                  ) : "Connecting…"}
                </div>
              </div>
            </div>
            <div className="ss-main-actions">
              <button className="ss-icon-btn" onClick={() => setShowPanel(!showPanel)} aria-label="Toggle details panel" title="Details panel">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><line x1="9" y1="3" x2="9" y2="21"></line><line x1="3" y1="9" x2="21" y2="9"></line></svg>
              </button>
              <button className={`ss-fullscreen-btn ${fullChat ? "active" : ""}`} onClick={() => setFullChat(!fullChat)} aria-label={fullChat ? "Exit full-screen chat" : "Enter full-screen chat"} title={fullChat ? "Exit full-screen chat" : "Enter full-screen chat"}>
                {fullChat ? (
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"></path></svg>
                ) : (
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3"></path></svg>
                )}
              </button>
              <button className="ss-mobile-toggle ss-icon-btn" onClick={() => setShowSidebar(!showSidebar)} aria-label="Toggle conversations">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="4" y1="6" x2="20" y2="6"></line><line x1="4" y1="12" x2="20" y2="12"></line><line x1="4" y1="18" x2="20" y2="18"></line></svg>
              </button>
            </div>
          </header>

          <div className="ss-messages" role="log" aria-live="polite" aria-label="Conversation">
            {messages.length === 0 ? (
              <div style={{ flex: "1", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "16px", color: "#64748b", padding: "40px" }}>
                <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" style={{ opacity: 0.3 }}>
                  <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
                </svg>
                <div style={{ fontSize: "16px", fontWeight: 500 }}>Welcome to SARA Sales Command Center</div>
                <div style={{ fontSize: "13px", maxWidth: "400px", textAlign: "center" }}>
                  Type commands in English, Hindi, or Hinglish. Examples:
                  <ul style={{ textAlign: "left", marginTop: "12px", paddingLeft: "20px", lineHeight: "2" }}>
                    <li>"Find hotels in Jaipur for my WhatsApp AI Agent"</li>
                    <li>"मेरे Voice Calling Agent के लिए listing बनाओ"</li>
                    <li>"Create email campaign for AI Automation Agency"</li>
                    <li>"Start continuous automation mode"</li>
                    <li>"Show today's sales status"</li>
                  </ul>
                </div>
              </div>
            ) : (
              messages.map((msg, i) => (
                <MessageBubble key={msg.id} msg={msg} isLast={i === messages.length - 1} />
              ))
            )}
            <div ref={messagesEndRef} />
          </div>

          {error && (
            <div style={{ padding: "12px 20px", background: "rgba(239,68,68,0.1)", borderTop: "1px solid #ef4444", color: "#fca5a5", fontSize: "13px" }}>
              {error}
              <button onClick={() => setError("")} style={{ marginLeft: "12px", color: "#f87171", background: "none", border: "none", cursor: "pointer" }}>✕</button>
            </div>
          )}

          <footer className="ss-composer">
            <div className="ss-composer-row">
              <div className="ss-composer-input-wrap">
                <textarea
                  className="ss-composer-input"
                  placeholder="Type a command… (English, Hindi, Hinglish)"
                  value={composerText}
                  onChange={e => setComposerText(e.target.value)}
                  onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }} }
                  disabled={sending || !activeConvId}
                  rows={1}
                  aria-label="Command input"
                />
              </div>
              <button
                className="ss-composer-send"
                onClick={handleSend}
                disabled={sending || !composerText.trim() || !activeConvId}
                aria-label="Send command"
              >
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
              </button>
            </div>
            <div className="ss-composer-hint">
              Enter = Send • Shift+Enter = New line • Commands: discover leads, create listing, stage campaign, approve, send, status, automation start/stop, integration health
            </div>
          </footer>
        </main>

        {/* Right Panel - Details */}
        <aside className={`ss-panel ${showPanel ? "open" : ""}`} aria-label="Details">
          <SidePanel
            task={panelTask}
            lead={panelLead}
            campaign={panelCampaign}
            listing={panelListing}
            onClose={() => { setShowPanel(false); setPanelTask(undefined); setPanelLead(undefined); setPanelCampaign(undefined); setPanelListing(undefined); }}
          />
        </aside>

        {/* Mobile backdrop */}
        {(showSidebar || showPanel) && window.innerWidth <= 1024 && (
          <div
            className="fixed inset-0 bg-black/50 z-30"
            onClick={() => { setShowSidebar(false); setShowPanel(false); }}
            aria-hidden="true"
          />
        )}
      </div>
    </>
  );
}

export default function SaraSalesPage() {
  return (
    <Suspense fallback={
      <div className="ss-root" style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
        <div style={{ textAlign: "center", color: "#64748b" }}>
          <div style={{ fontSize: "18px", marginBottom: "8px" }}>Loading Sales Command Center…</div>
          <div style={{ fontSize: "12px" }}>Connecting to SARA Sales Agent backend</div>
        </div>
      </div>
    }>
      <SaraSalesPageInner />
    </Suspense>
  );
}