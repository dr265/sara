"use client";

import React, {
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import Link from "next/link";
import { Shield, Check, X, Terminal, Disc, Play } from "lucide-react";

type ViewMode = "landing" | "workspace";

interface LogEvent {
  type: string;
  message?: string;
  prompt?: string;
  status?: string;
}

const VIDEO =
  "https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260831_232706_43757be4-2250-4f09-8cd7-23aebbf147ad.mp4";

const POSTER =
  "https://images.higgs.ai/?default=1&output=webp&url=https%3A%2F%2Fd8j0ntlcm91z4.cloudfront.net%2Fuser_38xzZboKViGWJOttwIXH07lWA1P%2Fhf_20260831_223518_f11bfa03-4e65-47e1-a4a7-30e42a7a8c2f.png&w=1920&q=85";

const styles = `
:root {
  --ink:#111111;
  --muted:#5c5c5c;
  --card:rgba(248,248,246,0.97);
  --shell:1120px;
  --ease:cubic-bezier(0.16,1,0.3,1);
}
*, *::before, *::after { box-sizing:border-box }

/* The main wrapper filling the flex area below Nexus Nav */
.backend-agent-wrapper {
  margin:0; padding:0; 
  background:#f8f9fa;
  font-family:"Poppins",-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 64px;
  left: 0;
  right: 0;
  bottom: 0;
  width: 100vw;
  height: calc(100vh - 64px);
  z-index: 10;
  overflow: hidden;
}

.backend-agent-wrapper button, .backend-agent-wrapper input, .backend-agent-wrapper textarea { font-family:inherit }
.backend-agent-wrapper button, .backend-agent-wrapper a, .backend-agent-wrapper textarea, .backend-agent-wrapper input { -webkit-tap-highlight-color:transparent }
.backend-agent-wrapper button:disabled { cursor:wait; opacity:.6 }
.backend-agent-wrapper a { color:inherit; text-decoration:none; }
.backend-agent-wrapper :focus-visible { outline:2px solid #3b82f6; outline-offset:2px }

/* Landing Page Styles */
.hero {
  position:relative; 
  height: 100%;
  width: 100%; 
  overflow: hidden; 
  display: flex; 
  flex-direction: column;
  color: var(--ink);
}
.hero__bg { position:absolute; inset:0; z-index:0; background-size:100% 100%; background-position:center; background-color:#eef2ee; }
.hero__bg video { position:absolute; inset:0; width:100%; height:100%; object-fit:fill; filter: hue-rotate(180deg) brightness(0.6); }
.hero__inner { position:relative; z-index:2; display:flex; flex-direction:column; flex:1 1 auto; min-height:0; }

.nav {
  position:relative; z-index:70; width:100%; max-width:var(--shell);
  margin:0 auto; padding:26px 24px 0; display:grid;
  grid-template-columns:1fr auto 1fr; align-items:center; gap:16px;
}
.brand {
  display:inline-flex; align-items:center; gap:10px; font-size:25px;
  font-weight:600; letter-spacing:-.02em; color: white;
  white-space:nowrap; width:fit-content; cursor:pointer;
}
.brand svg { width:30px; height:30px; display:block; flex-shrink:0; color: #3b82f6; }
.nav__links { display:flex; align-items:center; gap:42px; list-style:none; margin:0; padding:0; }
.nav__links a { font-size:15px; font-weight:400; color:white; opacity:.9; transition:opacity .18s ease; cursor:pointer; }
.nav__links a:hover { opacity:.55 }

.stage {
  flex:1 1 auto; min-height:0; display:flex; flex-direction:column;
  align-items:center; justify-content:center; text-align:center;
  padding:24px; gap:clamp(18px,2.4vh,26px); color: white;
}
.badge {
  display:inline-flex; align-items:center; gap:14px;
  background:rgba(0,0,0,.5); border:1px solid rgba(255,255,255,.1);
  border-radius:999px; padding:7px 16px 7px 8px; font-size:13.5px; color:white;
}
.badge__tag {
  background:#3b82f6; border:1px solid rgba(255,255,255,.1);
  border-radius:999px; padding:3px 12px; font-size:13.5px; font-weight:500; color:white;
}
.headline { margin:0; font-size:clamp(32px,5.7vw,72px); line-height:1.03; font-weight:500; letter-spacing:-.035em; max-width:20ch; }
.sub { margin:0; font-size:clamp(14px,1.35vw,17.5px); line-height:1.65; font-weight:400; color:#cbd5e1; max-width:66ch; }

.prompt {
  position:relative; width:min(790px,100%); background:rgba(0,0,0,.6);
  -webkit-backdrop-filter:blur(18px) saturate(1.15); backdrop-filter:blur(18px) saturate(1.15);
  border:1px solid rgba(255,255,255,.1); border-radius:20px;
  padding:22px 20px 16px; text-align:left;
  box-shadow:0 18px 44px -26px rgba(0,0,0,.5);
  margin-top:clamp(6px,1.6vh,16px);
}
.prompt__input {
  display:block; width:100%; border:0; outline:0; resize:none;
  background:transparent; font-family:inherit; font-size:16.5px;
  line-height:1.5; color:white; min-height:clamp(56px,11vh,108px);
  padding:2px 4px;
}
.prompt__input::placeholder { color:#94a3b8; opacity:.72 }
.prompt__bar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:10px; }
.prompt__right { display:flex; align-items:center; gap:12px }
.icon-btn {
  display:inline-flex; align-items:center; justify-content:center;
  width:42px; height:42px; border-radius:50%; border:1px solid rgba(255,255,255,.1);
  background:transparent; color:white; cursor:pointer; padding:0;
  transition:background .18s ease; flex-shrink:0;
}
.icon-btn:hover { background:rgba(255,255,255,0.1) }
.icon-btn--bare { border:0; background:transparent; width:34px }
.icon-btn--bare:hover { background:rgba(255,255,255,.1) }
.icon-btn--send {
  background:#3b82f6; border-color:#3b82f6; color:#fff;
  width:46px; height:46px; border-radius:12px;
}
.icon-btn--send:hover { background:#2563eb }
.icon-btn svg { width:19px; height:19px }
.icon-btn--send svg { width:21px; height:21px }

/* Repo input */
.repo-input {
  width: 100%;
  background: rgba(0,0,0,0.3);
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: 8px;
  padding: 8px 12px;
  color: white;
  margin-bottom: 12px;
  font-size: 14px;
}
.repo-input::placeholder { color: #64748b; }

/* Workspace Layout */
.workspace {
  display: flex;
  flex: 1;
  width: 100%;
  background: #0f172a;
  overflow: hidden;
  height: 100%;
}

.workspace__sidebar {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-right: 1px solid #1e293b;
  background: #1e293b;
  height: 100%;
  overflow: hidden;
  transition: transform 0.3s ease;
  color: white;
}
.workspace__sidebar.closed {
  transform: translateX(-100%);
  border-right: none;
}
.resizer {
  width: 6px;
  cursor: col-resize;
  background: transparent;
  flex-shrink: 0;
  z-index: 10;
  position: relative;
}
.resizer:hover {
  background: rgba(255,255,255,0.1);
}
.resizer:active {
  background: rgba(59, 130, 246, 0.5);
}
body.resizing * {
  pointer-events: none !important;
  user-select: none !important;
}

.sidebar__header {
  padding: 16px 20px;
  border-bottom: 1px solid #334155;
  background: #0f172a;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}

.btn-new-chat {
  background: #3b82f6; color: #fff; border: none;
  padding: 8px 14px; border-radius: 8px; font-size: 13px; font-weight: 500;
  cursor: pointer; display: flex; align-items: center; gap: 6px;
  transition: opacity 0.2s;
}
.btn-new-chat:hover { opacity: 0.8; }

.sidebar__chat {
  flex: 1;
  overflow-y: auto;
  padding: 24px 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.sidebar__input-area {
  padding: 20px;
  background: #0f172a;
  border-top: 1px solid #334155;
  flex-shrink: 0;
}

/* Main Workspace Pane */
.workspace__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #020617; 
  overflow: hidden;
}

.main__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 24px;
  background: #0f172a;
  border-bottom: 1px solid #1e293b;
  flex-shrink: 0;
}

.terminal-controls {
  display: flex; gap: 8px;
}
.control-dot { width: 12px; height: 12px; border-radius: 50%; }
.dot-red { background: #ef4444; }
.dot-yellow { background: #f59e0b; }
.dot-green { background: #10b981; }

.status-badge {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 1px;
  padding: 6px 12px; border-radius: 20px;
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.1);
  color: white;
}
.status-indicator {
  width: 8px; height: 8px; border-radius: 50%;
}
.status-indicator.pulse {
  animation: pulse 1.5s infinite;
}
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 var(--status-color); }
  70% { box-shadow: 0 0 0 6px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}

.terminal-body {
  flex: 1;
  padding: 24px;
  overflow-y: auto;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 14px;
  line-height: 1.6;
  color: #e4e4e7;
}

/* Custom Scrollbars */
.terminal-body::-webkit-scrollbar, .sidebar__chat::-webkit-scrollbar {
  width: 8px; height: 8px;
}
.terminal-body::-webkit-scrollbar-track, .sidebar__chat::-webkit-scrollbar-track {
  background: transparent;
}
.terminal-body::-webkit-scrollbar-thumb {
  background: rgba(255,255,255,0.1); border-radius: 4px;
}
.sidebar__chat::-webkit-scrollbar-thumb {
  background: rgba(255,255,255,0.2); border-radius: 4px;
}

.log-line {
  margin-bottom: 8px;
  word-wrap: break-word;
  animation: fade-in 0.3s ease-out;
}
.log-error { color: #f87171; }
.log-system { color: #60a5fa; font-weight: bold; }
.log-user { color: #a78bfa; font-weight: bold; }
.log-success { color: #34d399; }

/* Approval Overlay */
.approval-overlay {
  position: absolute;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0, 0, 0, 0.7);
  backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  animation: fade-in 0.4s ease-out;
}
.approval-card {
  background: #1e293b;
  border: 1px solid #334155;
  border-radius: 16px;
  width: 90%;
  max-width: 600px;
  box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);
}
.card-header {
  padding: 24px;
  border-bottom: 1px solid #334155;
  display: flex; align-items: center; gap: 12px;
}
.card-title {
  font-size: 18px; font-weight: 600; color: #fff;
}
.card-body {
  padding: 24px;
  max-height: 400px;
  overflow-y: auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 13px;
  color: #e4e4e7;
  background: #0f172a;
  white-space: pre-wrap;
}
.card-footer {
  padding: 24px;
  display: flex;
  gap: 16px;
  border-top: 1px solid #334155;
}

.btn-approve {
  flex: 1;
  background: #10b981;
  color: white; border: none; padding: 12px; border-radius: 8px;
  font-weight: 600; font-size: 15px; cursor: pointer;
  display: flex; align-items: center; justify-content: center; gap: 8px;
  transition: all 0.2s;
}
.btn-approve:hover { background: #059669; }

.btn-deny {
  flex: 1;
  background: transparent;
  border: 1px solid #ef4444;
  color: #ef4444; padding: 12px; border-radius: 8px;
  font-weight: 600; font-size: 15px; cursor: pointer;
  display: flex; align-items: center; justify-content: center; gap: 8px;
  transition: all 0.2s;
}
.btn-deny:hover { background: rgba(239, 68, 68, 0.1); }

@keyframes fade-in { from { opacity:0 } to { opacity:1 } }
.fade-in { animation:fade-in .4s ease-out; }
@keyframes rise { from { opacity:0; transform:translate3d(0,18px,0) } to { opacity:1; transform:translate3d(0,0,0) } }
.rise { animation:rise .9s var(--ease) both; animation-delay:calc(var(--i,0) * 70ms) }

/* Simple toggle */
.tab-btn {
  background: transparent; border: none; font-family: inherit;
  font-size: 13px; font-weight: 500; color: #94a3b8;
  padding: 8px 16px; cursor: pointer; border-radius: 6px;
  transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px;
}
.tab-btn:hover { color: #f8fafc; }
.tab-btn.active { background: #1e293b; color: #fff; }
`;

function RiseStyle(index: number) { return { "--i": index } as React.CSSProperties; }

function Logo() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
    </svg>
  );
}

function Icon({ kind }: { kind: "plus" | "send" | "check" | "mic" }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {kind === "plus" && <path d="M12 5v14M5 12h14" />}
      {kind === "send" && <path d="M4 12h15M13 6l6 6-6 6" />}
      {kind === "check" && <path d="M20 6L9 17l-5-5" />}
      {kind === "mic" && <><rect x="9" y="2.5" width="6" height="11" rx="3" /><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" /></>}
    </svg>
  );
}

export default function BackendArchitect() {
  const [viewMode, setViewMode] = useState<ViewMode>("landing");
  const [task, setTask] = useState("");
  const [repo, setRepo] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [approvalRequest, setApprovalRequest] = useState<string | null>(null);
  const [status, setStatus] = useState<"idle" | "running" | "waiting" | "completed" | "error">("idle");
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);
  const [sidebarWidth, setSidebarWidth] = useState(400);

  const logsEndRef = useRef<HTMLDivElement>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const isDragging = useRef(false);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  useEffect(() => {
    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
      }
    };
  }, []);

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDragging.current) return;
      const newWidth = Math.max(250, Math.min(e.clientX, window.innerWidth - 300));
      setSidebarWidth(newWidth);
    };
    const handleMouseUp = () => {
      if (isDragging.current) {
        isDragging.current = false;
        document.body.classList.remove('resizing');
      }
    };
    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
      document.body.classList.remove('resizing');
    };
  }, []);

  const handleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    isDragging.current = true;
    document.body.classList.add('resizing');
  };

  const handleStart = async (e?: FormEvent) => {
    if (e) e.preventDefault();
    if (!task.trim()) return;

    if (viewMode === "landing") {
      setViewMode("workspace");
    }

    setLogs(["[SYSTEM] Initializing Backend Architect Engine..."]);
    setStatus("running");
    setApprovalRequest(null);
    setSessionId(null);
    
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    try {
      const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
      const res = await fetch(`${baseUrl}/backend-architect/api/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task, repo: repo || "." }),
      });

      if (!res.ok) throw new Error("Failed to start agent");
      const data = await res.json();
      setSessionId(data.session_id);

      const source = new EventSource(`${baseUrl}/backend-architect/api/events?session_id=${data.session_id}`);
      eventSourceRef.current = source;

      source.onmessage = (event) => {
        try {
          const parsed: LogEvent = JSON.parse(event.data);
          
          if (parsed.type === "log" && parsed.message) {
            setLogs((prev) => [...prev, parsed.message!]);
          } else if (parsed.type === "approval_request" && parsed.prompt) {
            setStatus("waiting");
            setApprovalRequest(parsed.prompt);
          } else if (parsed.type === "done") {
            setStatus(parsed.status === "completed" ? "completed" : "error");
            source.close();
          }
        } catch (e) {
          console.error("Error parsing event:", e);
        }
      };

      source.onerror = (e) => {
        console.error("SSE Error:", e);
        source.close();
        setStatus("error");
      };

    } catch (err: any) {
      setLogs((prev) => [...prev, `[ERROR] ${err.message}`]);
      setStatus("error");
    }
  };

  const handleApproval = async (action: "APPROVE" | "DENY") => {
    if (!sessionId) return;
    
    setLogs((prev) => [...prev, `[USER] Action ${action}D`]);
    setApprovalRequest(null);
    setStatus("running");

    try {
      const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
      await fetch(`${baseUrl}/backend-architect/api/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, action }),
      });
    } catch (e: any) {
      console.error(e);
      setLogs((prev) => [...prev, `[ERROR] Failed to send approval: ${e.message}`]);
    }
  };

  const startNewSession = () => {
    setTask("");
    setLogs([]);
    setSessionId(null);
    setStatus("idle");
    setViewMode("landing");
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }
  };

  const formatLog = (text: string) => {
    if (text.startsWith("[SYSTEM]")) return <span className="log-system">{text}</span>;
    if (text.startsWith("[ERROR]")) return <span className="log-error">{text}</span>;
    if (text.startsWith("[USER]")) return <span className="log-user">{text}</span>;
    if (text.includes("completed") || text.includes("success")) return <span className="log-success">{text}</span>;
    return text;
  };

  const getStatusColor = () => {
    switch (status) {
      case "idle": return "#94a3b8";
      case "running": return "#3b82f6";
      case "waiting": return "#f59e0b";
      case "completed": return "#10b981";
      case "error": return "#ef4444";
      default: return "#94a3b8";
    }
  };

  if (viewMode === "workspace") {
    return (
      <div className="backend-agent-wrapper fade-in">
        <style dangerouslySetInnerHTML={{ __html: styles }} />
        <div className="workspace">
          <aside className={`workspace__sidebar ${!isSidebarOpen ? "closed" : ""}`} style={{ width: isSidebarOpen ? sidebarWidth : 0 }}>
            <header className="sidebar__header">
              <div className="brand" style={{ fontSize: "20px", whiteSpace: "nowrap" }}>
                <Logo /> Backend Architect
              </div>
              <button className="btn-new-chat" onClick={startNewSession} title="New Session" style={{ whiteSpace: "nowrap" }}>
                <Icon kind="plus" /> New
              </button>
            </header>
            
            <div className="sidebar__chat">
              <div style={{ color: "#94a3b8", fontSize: "14px", lineHeight: 1.5 }}>
                <p>Welcome to the autonomous Backend Architect workspace.</p>
                <p style={{ marginTop: "12px" }}>The agent will recursively inspect the provided repository, formulate an architectural plan, and safely execute backend changes with your explicit approval.</p>
              </div>
            </div>

            <div className="sidebar__input-area">
              <form 
                className="prompt" 
                style={{ marginTop: 0, width: "100%", borderRadius: "16px", padding: "16px 14px 12px" }}
                onSubmit={handleStart}
              >
                <input
                  type="text"
                  className="repo-input"
                  placeholder="/path/to/project (default: cwd)"
                  value={repo}
                  onChange={(e) => setRepo(e.target.value)}
                  disabled={status !== "idle" && status !== "error" && status !== "completed"}
                />
                <textarea
                  className="prompt__input"
                  style={{ minHeight: "50px", fontSize: "14px" }}
                  placeholder="Describe the architectural changes you want..."
                  value={task}
                  onChange={(e) => setTask(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      handleStart();
                    }
                  }}
                  disabled={status !== "idle" && status !== "error" && status !== "completed"}
                />
                <div className="prompt__bar">
                  <div className="prompt__right" style={{ width: "100%", justifyContent: "flex-end" }}>
                    <button type="submit" className="icon-btn icon-btn--send" style={{ width: "36px", height: "36px", borderRadius: "10px" }} disabled={status !== "idle" && status !== "error" && status !== "completed"}>
                      <Icon kind="send" />
                    </button>
                  </div>
                </div>
              </form>
            </div>
          </aside>
          {isSidebarOpen && <div className="resizer" onMouseDown={handleMouseDown} />}
          <main className="workspace__main">
            <header className="main__header">
              <div style={{ display: "flex", alignItems: "center" }}>
                <button className="tab-btn" onClick={() => setIsSidebarOpen(!isSidebarOpen)} title="Toggle Sidebar" style={{ marginRight: "8px" }}>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 3h18v18H3zM9 3v18"/></svg>
                </button>
                <div className="terminal-controls" style={{ marginLeft: "12px" }}>
                  <div className="control-dot dot-red" />
                  <div className="control-dot dot-yellow" />
                  <div className="control-dot dot-green" />
                </div>
              </div>
              <div 
                className="status-badge"
                style={{ "--status-color": getStatusColor() } as any}
              >
                <div 
                  className={`status-indicator ${(status === "running" || status === "waiting") ? "pulse" : ""}`}
                  style={{ background: getStatusColor() }}
                />
                {status.toUpperCase()}
              </div>
            </header>
            
            <div className="terminal-body">
              {logs.length === 0 ? (
                <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center", color: "#64748b" }}>
                  No output yet. Enter a task and start the agent.
                </div>
              ) : (
                logs.map((log, i) => (
                  <div key={i} className="log-line">
                    {formatLog(log)}
                  </div>
                ))
              )}
              <div ref={logsEndRef} />
            </div>

            {/* APPROVAL OVERLAY */}
            {status === "waiting" && approvalRequest && (
              <div className="approval-overlay">
                <div className="approval-card">
                  <div className="card-header">
                    <Shield size={24} color="#f59e0b" />
                    <span className="card-title">Authorization Required</span>
                  </div>
                  <div className="card-body">
                    {approvalRequest}
                  </div>
                  <div className="card-footer">
                    <button className="btn-deny" onClick={() => handleApproval("DENY")}>
                      <X size={18} /> Deny
                    </button>
                    <button className="btn-approve" onClick={() => handleApproval("APPROVE")}>
                      <Check size={18} /> Approve Execution
                    </button>
                  </div>
                </div>
              </div>
            )}
          </main>
        </div>
      </div>
    );
  }

  // Landing Page Mode
  return (
    <div className="backend-agent-wrapper fade-in">
      <style dangerouslySetInnerHTML={{ __html: styles }} />
      <div className="hero">
        <div className="hero__bg">
          <video src={VIDEO} poster={POSTER} autoPlay loop muted playsInline />
        </div>
        
        <div className="hero__inner">
          <header className="nav">
            <a href="#" className="brand rise" style={RiseStyle(0)}><Logo /> Backend Architect</a>
            <nav className="nav__nav">
              <ul className="nav__links">
                <li><Link href="/hub" className="rise" style={RiseStyle(1)}>Back to Hub</Link></li>
              </ul>
            </nav>
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <div className="badge rise" style={RiseStyle(3)}>
                <span className="badge__tag">v1.0</span> Autonomous
              </div>
            </div>
          </header>
          
          <div className="stage">
            <h1 className="headline rise" style={RiseStyle(4)}>
              Build backend systems <br /> autonomously.
            </h1>
            <p className="sub rise" style={RiseStyle(5)}>
              Provide a repository path and an engineering task. The agent will inspect, plan, and implement backend architecture directly within your codebase.
            </p>
            
            <form className="prompt rise" style={RiseStyle(6)} onSubmit={handleStart}>
              <input
                type="text"
                className="repo-input"
                style={{ background: "transparent", border: "none", borderBottom: "1px solid rgba(255,255,255,0.2)", borderRadius: 0, paddingLeft: "4px" }}
                placeholder="/path/to/project (default: cwd)"
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
              />
              <textarea
                className="prompt__input"
                placeholder="Describe your backend requirements or refactoring goals..."
                value={task}
                onChange={(e) => setTask(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleStart();
                  }
                }}
              />
              <div className="prompt__bar">
                <button type="button" className="icon-btn icon-btn--bare" title="Config">
                  <Icon kind="plus" />
                </button>
                <div className="prompt__right">
                  <button type="submit" className="icon-btn icon-btn--send">
                    <Icon kind="send" />
                  </button>
                </div>
              </div>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}
