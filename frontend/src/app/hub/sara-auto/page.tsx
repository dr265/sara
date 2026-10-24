"use client";

import React, { useState, useRef, useEffect, useCallback } from "react";
import Link from "next/link";

/* ─── types ─────────────────────────────────────────────────────────────────── */
interface SaraEvent {
  type: string;
  message?: string;
  // meta
  model?: string;
  provider?: string;
  tools?: string[];
  // tool events
  call_id?: string;
  name?: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  output?: string;
  seconds?: number;
  prompt?: string;
  // done
  status?: string;
  summary?: string;
  steps?: { name: string; ok: boolean; args?: Record<string, unknown> }[];
  error?: string;
}

interface ToolStep {
  call_id: string;
  name: string;
  args?: Record<string, unknown>;
  status: "running" | "ok" | "failed" | "denied";
  output?: string;
  seconds?: number;
}

interface Capability {
  id: string;
  name: string;
  icon: string;
  category: "System" | "Media" | "Web" | "Dev";
  desc: string;
}

/* Static fallback matrix — replaced by live /capabilities data when reachable. */
const FALLBACK_CAPS: Capability[] = [
  { id: "system_command", name: "PowerShell & Terminal", icon: "⚡", category: "System", desc: "Runs real CLI/PowerShell commands on the host" },
  { id: "open_app", name: "App Launcher", icon: "📱", category: "System", desc: "Launches WhatsApp, Chrome, VS Code, Spotify…" },
  { id: "youtube_video", name: "YouTube & Music", icon: "🎵", category: "Media", desc: "Plays songs, searches and summarizes videos" },
  { id: "browser_control", name: "Web Automation", icon: "🌐", category: "Web", desc: "Navigates, clicks, types and captures pages" },
  { id: "web_search", name: "Live Web Search", icon: "🔍", category: "Web", desc: "Real-time grounded research with sources" },
  { id: "system_monitor", name: "Diagnostics", icon: "⚙️", category: "System", desc: "CPU load, memory, disk, network, battery" },
  { id: "code_helper", name: "Code & Terminal", icon: "💻", category: "Dev", desc: "Writes, fixes, runs and explains code" },
  { id: "file_controller", name: "File Architect", icon: "📁", category: "Dev", desc: "Creates, reads, moves, organizes files" },
  { id: "file_processor", name: "Document Reader", icon: "📄", category: "Dev", desc: "Extracts and summarizes PDFs and docs" },
  { id: "desktop_control", name: "Desktop Manager", icon: "🖥️", category: "System", desc: "Screenshots, windows, power actions" },
  { id: "computer_control", name: "Input Automation", icon: "⌨️", category: "System", desc: "Keyboard, mouse and screen understanding" },
  { id: "weather_report", name: "Weather Radar", icon: "⛅", category: "Web", desc: "Live conditions and forecasts, real data" },
  { id: "reminder", name: "Schedule & Alarms", icon: "⏰", category: "System", desc: "Timed alerts and reminders" },
  { id: "send_message", name: "Communications", icon: "💬", category: "System", desc: "Sends messages via connected channels" },
  { id: "flight_finder", name: "Flight Radar", icon: "✈️", category: "Web", desc: "Flight routes, prices and live status" },
  { id: "game_updater", name: "Game Updater", icon: "🎮", category: "System", desc: "Scans and launches game updates" },
  { id: "dev_agent", name: "Autonomous Dev", icon: "🛠️", category: "Dev", desc: "Generates, refactors and tests code" },
];

const PROMPT_SUGGESTIONS = [
  { label: "🔍 Research AI news", prompt: "search for the most important AI news this week and summarize the top three" },
  { label: "⛅ Live weather", prompt: "what is the weather in Tokyo right now, with tomorrow's forecast" },
  { label: "⚙️ System diagnostics", prompt: "check my CPU, memory and battery status and tell me if anything needs attention" },
  { label: "💬 Open WhatsApp", prompt: "open my whatsapp" },
  { label: "🎵 Play on YouTube", prompt: "open youtube and play never gonna give you up" },
  { label: "📁 Desktop cleanup", prompt: "organize the files on my desktop into folders by type" },
];

const STATUS_CONFIG: Record<string, { label: string; color: string; bg: string }> = {
  idle:      { label: "STANDBY // READY", color: "#94a3b8", bg: "rgba(148, 163, 184, 0.1)" },
  starting:  { label: "INITIALIZING CORE", color: "#f59e0b", bg: "rgba(245, 158, 11, 0.12)" },
  running:   { label: "AUTONOMOUS EXECUTION", color: "#06b6d4", bg: "rgba(6, 182, 212, 0.15)" },
  completed: { label: "MISSION ACCOMPLISHED", color: "#10b981", bg: "rgba(16, 185, 129, 0.15)" },
  completed_with_issues: { label: "⚠ COMPLETED // WITH ISSUES", color: "#f59e0b", bg: "rgba(245, 158, 11, 0.15)" },
  failed:    { label: "EXECUTION HALTED", color: "#ef4444", bg: "rgba(239, 68, 68, 0.15)" },
  stopped:   { label: "MANUAL OVERRIDE STOP", color: "#f97316", bg: "rgba(249, 115, 22, 0.15)" },
};

const TOOL_ICON: Record<string, string> = {
  system_command: "⚡", open_app: "📱", youtube_video: "🎵", browser_control: "🌐",
  web_search: "🔍", system_monitor: "⚙️", code_helper: "💻", file_controller: "📁",
  file_processor: "📄", desktop_control: "🖥️", computer_control: "⌨️",
  weather_report: "⛅", reminder: "⏰", send_message: "💬", flight_finder: "✈️",
  game_updater: "🎮", dev_agent: "🛠️", computer_settings: "🎛️",
};

/* ─── component ─────────────────────────────────────────────────────────────── */
export default function SaraJarvisAgentPage() {
  const [goal, setGoal]                 = useState("");
  const [taskId, setTaskId]             = useState<string | null>(null);
  const [status, setStatus]             = useState("idle");
  const [logs, setLogs]                 = useState<string[]>([]);
  const [toolSteps, setToolSteps]       = useState<ToolStep[]>([]);
  const [finalSummary, setFinalSummary] = useState("");
  const [approval, setApproval]         = useState<{ callId: string; name: string; prompt: string } | null>(null);
  const [loading, setLoading]           = useState(false);
  const [isListening, setIsListening]   = useState(false);
  const [voiceSpeechEnabled, setVoiceSpeechEnabled] = useState(true);
  const [agentMode, setAgentMode]       = useState<"autonomous" | "assisted">("autonomous");
  const [engine, setEngine]             = useState<{ model: string; provider: string; tools: number } | null>(null);
  const [caps, setCaps]                 = useState<Capability[]>(FALLBACK_CAPS);
  const [activeCategory, setActiveCategory] = useState<string>("All");

  const logsEndRef     = useRef<HTMLDivElement>(null);
  const esRef          = useRef<EventSource | null>(null);
  const recognitionRef = useRef<any>(null);

  const BACKEND = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

  /* ── Live capability list ────────────────────────────────────────────────── */
  useEffect(() => {
    let cancelled = false;
    fetch(`${BACKEND}/sara-auto/capabilities`)
      .then(r => r.json())
      .then(data => {
        if (cancelled || !Array.isArray(data.tools) || data.tools.length === 0) return;
        const known = new Map(FALLBACK_CAPS.map(c => [c.id, c]));
        const live: Capability[] = data.tools.map((id: string) => {
          const hit = known.get(id);
          return hit ?? { id, name: id.replace(/_/g, " ").replace(/\b\w/g, (m: string) => m.toUpperCase()), icon: TOOL_ICON[id] || "🔧", category: "System" as const, desc: "Live tool from the JARVIS core registry" };
        });
        setCaps(live);
      })
      .catch(() => { /* fallback list stays */ });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* ── Speech Synthesis (Jarvis Voice) ────────────────────────────────────── */
  const speakJarvis = useCallback((text: string) => {
    if (!voiceSpeechEnabled || typeof window === "undefined" || !("speechSynthesis" in window)) return;
    try {
      window.speechSynthesis.cancel();
      const cleanText = text.replace(/\[SARA\]|\*|_|#|```[\s\S]*?```/g, " ").replace(/\s+/g, " ").trim().slice(0, 400);
      const utterance = new SpeechSynthesisUtterance(cleanText);
      utterance.rate = 1.05;
      utterance.pitch = 0.95;
      const voices = window.speechSynthesis.getVoices();
      const preferred = voices.find(v => v.lang.includes("en") && (v.name.includes("Natural") || v.name.includes("David") || v.name.includes("Google")));
      if (preferred) utterance.voice = preferred;
      window.speechSynthesis.speak(utterance);
    } catch {
      /* ignore audio context restrictions */
    }
  }, [voiceSpeechEnabled]);

  /* ── Web Speech Recognition (Voice Input) ───────────────────────────────── */
  const toggleVoiceListen = () => {
    if (typeof window === "undefined") return;
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SpeechRecognition) {
      alert("Voice recognition is not supported in this browser. Please use Chrome or Edge.");
      return;
    }

    if (isListening) {
      recognitionRef.current?.stop();
      setIsListening(false);
      return;
    }

    try {
      const recognition = new SpeechRecognition();
      recognition.continuous = false;
      recognition.interimResults = true;
      recognition.lang = "en-US";

      recognition.onstart = () => setIsListening(true);
      recognition.onresult = (event: any) => {
        const transcript = Array.from(event.results)
          .map((r: any) => r[0].transcript)
          .join("");
        setGoal(transcript);
      };
      recognition.onerror = () => setIsListening(false);
      recognition.onend   = () => setIsListening(false);

      recognitionRef.current = recognition;
      recognition.start();
    } catch {
      setIsListening(false);
    }
  };

  /* Auto-scroll logs */
  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs, toolSteps, finalSummary]);

  /* Cleanup on unmount */
  useEffect(() => {
    return () => {
      esRef.current?.close();
      recognitionRef.current?.stop();
      if (typeof window !== "undefined" && "speechSynthesis" in window) {
        window.speechSynthesis.cancel();
      }
    };
  }, []);

  /* ── Handle Real-time Stream Events ─────────────────────────────────────── */
  const handleEvent = (ev: SaraEvent) => {
    if (ev.type === "meta") {
      setEngine({ model: ev.model || "?", provider: ev.provider || "?", tools: ev.tools?.length || 0 });
    } else if (ev.type === "log") {
      setLogs(prev => [...prev, ev.message ?? ""]);
    } else if (ev.type === "tool_call") {
      setToolSteps(prev => [...prev, {
        call_id: ev.call_id || `${prev.length}`,
        name: ev.name || "tool",
        args: ev.args,
        status: "running",
      }]);
    } else if (ev.type === "tool_result") {
      setToolSteps(prev => prev.map(s =>
        s.call_id === (ev.call_id || "")
          ? { ...s, status: (ev.ok ? "ok" : "failed") as ToolStep["status"], output: ev.output, seconds: ev.seconds }
          : s
      ));
    } else if (ev.type === "approval_request") {
      setApproval({ callId: ev.call_id || "", name: ev.name || "action", prompt: ev.prompt || "Authorization required" });
      speakJarvis(`Authorization required for ${ev.name}, sir.`);
    } else if (ev.type === "done") {
      setStatus(ev.status ?? "completed");
      setLoading(false);
      esRef.current?.close();
      esRef.current = null;
      if (ev.status === "completed") {
        const sum = ev.summary || "All requested actions have been executed, sir.";
        setFinalSummary(sum);
        speakJarvis(sum);
      } else if (ev.status === "completed_with_issues") {
        const sum = ev.summary || "Task finished, but some actions failed, sir.";
        setFinalSummary(sum);
        setLogs(prev => [...prev, "[SARA] ⚠ TASK COMPLETED WITH ISSUES — see the failed steps above."]);
        speakJarvis("Task finished, but some actions had issues, sir.");
      } else if (ev.status === "failed") {
        setLogs(prev => [...prev, `[SARA] EXECUTION HALTED: ${ev.error || ev.summary || "unknown error"}`]);
        speakJarvis("Execution halted due to an anomaly, sir.");
      } else {
        setFinalSummary(ev.summary || "Stopped by user.");
      }
    }
  };

  /* ── Open SSE Stream ────────────────────────────────────────────────────── */
  const openStream = (tid: string) => {
    esRef.current?.close();

    const es = new EventSource(`${BACKEND}/sara-auto/events?task_id=${encodeURIComponent(tid)}`);
    esRef.current = es;

    es.onmessage = (e) => {
      try {
        handleEvent(JSON.parse(e.data) as SaraEvent);
      } catch { /* ignore malformed frames */ }
    };

    es.onerror = () => {
      // Don't kill the UI on transient SSE drops; only stop spinning if idle.
      if (esRef.current && esRef.current.readyState === EventSource.CLOSED) {
        esRef.current = null;
        setLoading(false);
      }
    };
  };

  /* ── Run Execution Goal ─────────────────────────────────────────────────── */
  const handleRun = async (e?: React.FormEvent, customGoal?: string) => {
    if (e) e.preventDefault();
    const targetGoal = (customGoal ?? goal).trim();
    if (!targetGoal || loading) return;

    if (customGoal) setGoal(customGoal);
    setLogs([]);
    setToolSteps([]);
    setFinalSummary("");
    setApproval(null);
    setTaskId(null);
    setLoading(true);
    setStatus("starting");

    speakJarvis("Acknowledged. Initiating autonomous execution sequence, sir.");

    try {
      const res = await fetch(`${BACKEND}/sara-auto/run`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ goal: targetGoal, mode: agentMode }),
      });

      if (!res.ok) {
        const text = await res.text().catch(() => "");
        let errMsg = `Failed to start task (${res.status})`;
        try {
          const errData = JSON.parse(text);
          errMsg = errData.detail || errMsg;
        } catch {
          if (text) errMsg = text.slice(0, 100);
        }
        throw new Error(errMsg);
      }

      const data = await res.json();
      setTaskId(data.task_id);
      setStatus("running");
      openStream(data.task_id);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setLogs(prev => [...prev, `[ERROR] ${msg}`]);
      setStatus("failed");
      setLoading(false);
      speakJarvis("An anomaly occurred during task initiation.");
    }
  };

  /* ── Approval Gate Response ─────────────────────────────────────────────── */
  const handleApprove = async (action: "APPROVE" | "DENY") => {
    if (!taskId) return;
    await fetch(`${BACKEND}/sara-auto/approve`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ task_id: taskId, call_id: approval?.callId || "", action }),
    });
    if (action === "DENY" && approval) {
      setToolSteps(prev => prev.map(s =>
        s.call_id === approval.callId ? { ...s, status: "denied" as const } : s));
    }
    setApproval(null);
    speakJarvis(action === "APPROVE" ? "Authorization granted. Proceeding." : "Action denied.");
  };

  /* ── Emergency Stop ─────────────────────────────────────────────────────── */
  const handleStop = async () => {
    if (!taskId) return;
    await fetch(`${BACKEND}/sara-auto/stop`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ task_id: taskId }),
    });
    esRef.current?.close();
    esRef.current = null;
    setStatus("stopped");
    setLoading(false);
    speakJarvis("Emergency override engaged. All processes halted.");
  };

  /* ── Clear Logs ─────────────────────────────────────────────────────────── */
  const handleClearLogs = () => setLogs([]);

  const filteredCapabilities = caps.filter(
    c => activeCategory === "All" || c.category === activeCategory
  );

  const curStatus = STATUS_CONFIG[status] || STATUS_CONFIG.idle;
  const completedTools = toolSteps.filter(s => s.status === "ok").length;
  const failedTools = toolSteps.filter(s => s.status === "failed" || s.status === "denied").length;

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap');

        .jarvis-viewport {
          font-family: 'Inter', -apple-system, sans-serif;
          position: fixed;
          top: 64px; left: 0; right: 0; bottom: 0;
          background: radial-gradient(circle at 50% 20%, #061524 0%, #03080e 65%, #010306 100%);
          color: #e2e8f0;
          display: flex;
          flex-direction: column;
          overflow: hidden;
          z-index: 20;
        }

        /* Ambient HUD Scanlines */
        .hud-grid-overlay {
          position: absolute; inset: 0;
          background-image:
            linear-gradient(rgba(6, 182, 212, 0.04) 1px, transparent 1px),
            linear-gradient(90deg, rgba(6, 182, 212, 0.04) 1px, transparent 1px);
          background-size: 40px 40px;
          pointer-events: none;
          z-index: 1;
        }

        /* Glass HUD Panel */
        .jarvis-card {
          background: rgba(6, 17, 29, 0.72);
          backdrop-filter: blur(24px) saturate(1.3);
          border: 1px solid rgba(6, 182, 212, 0.22);
          border-radius: 16px;
          box-shadow: 0 16px 36px -12px rgba(0, 0, 0, 0.7), inset 0 1px 0 rgba(255, 255, 255, 0.08);
          position: relative;
          z-index: 2;
        }

        /* Top Bar */
        .jarvis-topbar {
          padding: 14px 28px;
          border-bottom: 1px solid rgba(6, 182, 212, 0.18);
          display: flex;
          align-items: center;
          justify-content: space-between;
          background: rgba(2, 8, 14, 0.85);
          position: relative;
          z-index: 10;
        }

        /* Arc-Reactor Core */
        .arc-reactor {
          width: 44px; height: 44px;
          position: relative;
          display: flex;
          align-items: center;
          justify-content: center;
        }
        .arc-ring-outer {
          position: absolute; inset: 0;
          border: 2px dashed rgba(6, 182, 212, 0.6);
          border-radius: 50%;
          animation: spin-cw 12s linear infinite;
        }
        .arc-ring-inner {
          position: absolute; inset: 6px;
          border: 2px solid rgba(56, 189, 248, 0.5);
          border-radius: 50%;
          border-top-color: transparent;
          border-bottom-color: transparent;
          animation: spin-ccw 6s linear infinite;
        }
        .arc-core {
          width: 18px; height: 18px;
          background: radial-gradient(circle, #38bdf8 0%, #0284c7 60%, #0369a1 100%);
          border-radius: 50%;
          box-shadow: 0 0 18px #06b6d4, 0 0 32px rgba(6, 182, 212, 0.8);
          animation: pulse-glow 2s ease-in-out infinite;
        }
        .arc-reactor.active .arc-core {
          box-shadow: 0 0 28px #22d3ee, 0 0 54px #06b6d4;
          animation: pulse-fast 0.8s ease-in-out infinite;
        }

        @keyframes spin-cw  { to { transform: rotate(360deg); } }
        @keyframes spin-ccw { to { transform: rotate(-360deg); } }
        @keyframes pulse-glow {
          0%, 100% { transform: scale(0.95); opacity: 0.85; }
          50%      { transform: scale(1.1);  opacity: 1; }
        }
        @keyframes pulse-fast {
          0%, 100% { transform: scale(0.9); }
          50%      { transform: scale(1.2); }
        }

        /* Jarvis Main Layout */
        .jarvis-content {
          flex: 1;
          display: grid;
          grid-template-columns: 1fr 390px;
          gap: 16px;
          padding: 16px 24px;
          overflow: hidden;
          position: relative;
          z-index: 2;
        }
        @media (max-width: 1080px) {
          .jarvis-content { grid-template-columns: 1fr; overflow-y: auto; }
        }

        /* Voice Waveform animation */
        .voice-wave-container {
          display: inline-flex;
          align-items: center;
          gap: 3px;
          height: 18px;
          margin-left: 8px;
        }
        .voice-bar {
          width: 3px;
          height: 6px;
          background: #06b6d4;
          border-radius: 2px;
          animation: wave 1s ease-in-out infinite;
        }
        .voice-bar:nth-child(2) { animation-delay: 0.15s; }
        .voice-bar:nth-child(3) { animation-delay: 0.3s; }
        .voice-bar:nth-child(4) { animation-delay: 0.45s; }
        @keyframes wave {
          0%, 100% { height: 6px; }
          50%      { height: 18px; }
        }

        /* Goal Input Box */
        .goal-box { display: flex; flex-direction: column; gap: 12px; }
        .goal-input-wrapper {
          position: relative;
          background: rgba(3, 10, 18, 0.85);
          border: 1px solid rgba(6, 182, 212, 0.28);
          border-radius: 14px;
          transition: all 0.25s;
          box-shadow: inset 0 2px 8px rgba(0, 0, 0, 0.6);
        }
        .goal-input-wrapper:focus-within {
          border-color: #06b6d4;
          box-shadow: 0 0 20px rgba(6, 182, 212, 0.28), inset 0 2px 8px rgba(0, 0, 0, 0.6);
        }
        .goal-input {
          width: 100%;
          min-height: 84px;
          padding: 16px 18px 52px;
          background: transparent;
          border: none;
          outline: none;
          color: #f1f5f9;
          font-size: 0.95rem;
          line-height: 1.5;
          font-family: inherit;
          resize: none;
        }
        .goal-input::placeholder { color: #475569; }

        /* Input Toolbar */
        .input-toolbar {
          position: absolute;
          bottom: 10px; left: 14px; right: 14px;
          display: flex;
          align-items: center;
          justify-content: space-between;
        }

        /* Suggestions chips */
        .chip-row { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 4px; }
        .chip-btn {
          background: rgba(6, 182, 212, 0.08);
          border: 1px solid rgba(6, 182, 212, 0.2);
          color: #93c5fd;
          padding: 5px 12px;
          border-radius: 999px;
          font-size: 0.74rem;
          font-weight: 500;
          cursor: pointer;
          transition: all 0.2s;
          white-space: nowrap;
        }
        .chip-btn:hover {
          background: rgba(6, 182, 212, 0.18);
          border-color: #38bdf8;
          color: #ffffff;
          transform: translateY(-1px);
        }

        /* Microphones */
        .btn-mic {
          width: 36px; height: 36px;
          border-radius: 50%;
          background: rgba(15, 23, 42, 0.8);
          border: 1px solid rgba(6, 182, 212, 0.35);
          color: #06b6d4;
          display: flex; align-items: center; justify-content: center;
          cursor: pointer;
          transition: all 0.2s;
        }
        .btn-mic:hover { background: rgba(6, 182, 212, 0.2); border-color: #38bdf8; color: #fff; }
        .btn-mic.listening {
          background: rgba(239, 68, 68, 0.25);
          border-color: #ef4444;
          color: #ef4444;
          box-shadow: 0 0 16px rgba(239, 68, 68, 0.6);
          animation: pulse-glow 1s infinite;
        }

        /* Action Buttons */
        .btn-execute {
          background: linear-gradient(135deg, #0284c7 0%, #0d9488 100%);
          border: 1px solid rgba(56, 189, 248, 0.4);
          color: #ffffff;
          font-weight: 600;
          font-size: 0.86rem;
          padding: 8px 22px;
          border-radius: 10px;
          cursor: pointer;
          display: inline-flex;
          align-items: center;
          gap: 8px;
          transition: all 0.2s;
          box-shadow: 0 4px 14px rgba(6, 182, 212, 0.25);
        }
        .btn-execute:hover:not(:disabled) {
          background: linear-gradient(135deg, #0ea5e9 0%, #14b8a6 100%);
          box-shadow: 0 6px 20px rgba(6, 182, 212, 0.45);
          transform: translateY(-1px);
        }
        .btn-execute:disabled { opacity: 0.5; cursor: not-allowed; }

        .btn-stop-override {
          background: rgba(239, 68, 68, 0.18);
          border: 1px solid rgba(239, 68, 68, 0.4);
          color: #fca5a5;
          font-weight: 600;
          font-size: 0.85rem;
          padding: 8px 16px;
          border-radius: 10px;
          cursor: pointer;
          transition: all 0.2s;
        }
        .btn-stop-override:hover { background: rgba(239, 68, 68, 0.35); color: #fff; }

        /* Log Terminal HUD */
        .terminal-container {
          flex: 1;
          display: flex;
          flex-direction: column;
          overflow: hidden;
          padding: 16px 20px;
        }
        .terminal-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding-bottom: 10px;
          border-bottom: 1px solid rgba(6, 182, 212, 0.15);
          margin-bottom: 10px;
        }
        .terminal-body {
          flex: 1;
          overflow-y: auto;
          font-family: 'JetBrains Mono', monospace;
          font-size: 0.79rem;
          line-height: 1.65;
          color: #94a3b8;
          padding-right: 6px;
        }
        .terminal-body::-webkit-scrollbar { width: 5px; }
        .terminal-body::-webkit-scrollbar-thumb {
          background: rgba(6, 182, 212, 0.25);
          border-radius: 4px;
        }

        .log-entry { margin-bottom: 4px; word-break: break-word; white-space: pre-wrap; }
        .log-entry.sara-tag  { color: #38bdf8; font-weight: 500; }
        .log-entry.tool-tag  { color: #c084fc; font-weight: 600; }
        .log-entry.done-tag  { color: #34d399; font-weight: 600; }
        .log-entry.error-tag { color: #f87171; font-weight: 600; }
        .log-entry.warn-tag  { color: #fbbf24; }

        /* Final report card inside the terminal */
        .final-report {
          margin-top: 12px;
          padding: 14px 16px;
          border-radius: 12px;
          background: rgba(16, 185, 129, 0.06);
          border: 1px solid rgba(52, 211, 153, 0.35);
          color: #d1fae5;
          font-family: 'Inter', sans-serif;
          font-size: 0.86rem;
          line-height: 1.6;
          white-space: pre-wrap;
        }

        /* Right Panel */
        .right-pane { display: flex; flex-direction: column; gap: 14px; overflow: hidden; }

        /* Tool Timeline */
        .steps-container {
          flex: 1;
          min-height: 120px;
          overflow-y: auto;
          display: flex;
          flex-direction: column;
          gap: 8px;
          padding: 2px;
        }
        .steps-container::-webkit-scrollbar { width: 4px; }
        .steps-container::-webkit-scrollbar-thumb {
          background: rgba(6, 182, 212, 0.25);
          border-radius: 4px;
        }

        .hud-step-card {
          padding: 10px 12px;
          border-radius: 10px;
          background: rgba(3, 11, 20, 0.7);
          border: 1px solid rgba(6, 182, 212, 0.15);
          display: flex;
          gap: 10px;
          align-items: flex-start;
          transition: all 0.2s;
        }
        .hud-step-card.running {
          border-color: #06b6d4;
          background: rgba(6, 182, 212, 0.1);
          box-shadow: 0 0 14px rgba(6, 182, 212, 0.2);
        }
        .hud-step-card.ok {
          border-color: rgba(52, 211, 153, 0.35);
          background: rgba(16, 185, 129, 0.05);
        }
        .hud-step-card.failed {
          border-color: rgba(248, 113, 113, 0.4);
          background: rgba(239, 68, 68, 0.06);
        }
        .hud-step-card.denied {
          border-color: rgba(251, 191, 36, 0.4);
          background: rgba(245, 158, 11, 0.06);
        }

        /* Capability Grid */
        .caps-grid {
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: 8px;
          flex: 1;
          min-height: 140px;
          overflow-y: auto;
          padding-right: 4px;
        }
        .caps-grid::-webkit-scrollbar { width: 4px; }
        .caps-grid::-webkit-scrollbar-thumb {
          background: rgba(6, 182, 212, 0.2);
          border-radius: 4px;
        }

        .cap-item {
          padding: 8px 10px;
          border-radius: 10px;
          background: rgba(3, 10, 18, 0.6);
          border: 1px solid rgba(6, 182, 212, 0.14);
          cursor: pointer;
          transition: all 0.2s;
        }
        .cap-item:hover {
          background: rgba(6, 182, 212, 0.12);
          border-color: #06b6d4;
          transform: translateY(-1px);
        }

        /* Approval Modal */
        .jarvis-modal-backdrop {
          position: fixed; inset: 0;
          background: rgba(0, 4, 8, 0.82);
          backdrop-filter: blur(12px);
          display: flex; align-items: center; justify-content: center;
          z-index: 1000;
        }
        .jarvis-modal {
          width: 460px;
          max-width: 92vw;
          padding: 28px;
          border-radius: 20px;
          background: rgba(5, 14, 24, 0.96);
          border: 1px solid #f59e0b;
          box-shadow: 0 0 60px rgba(245, 158, 11, 0.25);
        }
      `}</style>

      <div className="jarvis-viewport">
        <div className="hud-grid-overlay" />

        {/* ── TOP NAV BAR ── */}
        <header className="jarvis-topbar">
          <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
            {/* Arc Reactor */}
            <div className={`arc-reactor ${loading ? "active" : ""}`}>
              <div className="arc-ring-outer" />
              <div className="arc-ring-inner" />
              <div className="arc-core" />
            </div>

            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <span style={{ fontFamily: "'Orbitron', sans-serif", fontSize: "1.1rem", fontWeight: 700, letterSpacing: "1px", color: "#f8fafc" }}>
                  SARA // JARVIS
                </span>
                <span
                  style={{
                    background: "rgba(6, 182, 212, 0.16)",
                    border: "1px solid #06b6d4",
                    color: "#22d3ee",
                    padding: "3px 10px",
                    borderRadius: "999px",
                    fontSize: "0.68rem",
                    fontWeight: 700,
                    letterSpacing: "1.2px",
                    boxShadow: "0 0 12px rgba(6, 182, 212, 0.35)",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "6px",
                  }}
                >
                  <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#22d3ee", boxShadow: "0 0 8px #22d3ee" }} />
                  REAL TOOL EXECUTION
                </span>
              </div>
              <div style={{ fontSize: "0.72rem", color: "#64748b", fontFamily: "'JetBrains Mono', monospace", marginTop: 2 }}>
                {engine
                  ? `ENGINE: ${engine.model} · ${engine.tools} TOOLS ARMED · ${engine.provider.toUpperCase()} · PROTOCOL v3.0`
                  : "CORE ONLINE · HANDSHAKE PENDING · PROTOCOL v3.0"}
              </div>
            </div>
          </div>

          {/* Right Controls */}
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            {/* Voice Audio Feedback Toggle */}
            <button
              onClick={() => setVoiceSpeechEnabled(prev => !prev)}
              style={{
                background: voiceSpeechEnabled ? "rgba(6, 182, 212, 0.16)" : "rgba(30, 41, 59, 0.6)",
                border: `1px solid ${voiceSpeechEnabled ? "#06b6d4" : "#475569"}`,
                color: voiceSpeechEnabled ? "#38bdf8" : "#94a3b8",
                padding: "6px 12px",
                borderRadius: "8px",
                fontSize: "0.75rem",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                gap: "6px",
              }}
              title="Toggle Jarvis audible speech response"
            >
              <span>{voiceSpeechEnabled ? "🔊" : "🔇"}</span>
              <span>Voice: {voiceSpeechEnabled ? "ON" : "OFF"}</span>
            </button>

            {/* Status Pill */}
            <div
              style={{
                padding: "6px 14px",
                borderRadius: "8px",
                background: curStatus.bg,
                border: `1px solid ${curStatus.color}40`,
                color: curStatus.color,
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: "0.72rem",
                fontWeight: 600,
                display: "flex",
                alignItems: "center",
                gap: "8px",
              }}
            >
              <span style={{ width: 7, height: 7, borderRadius: "50%", background: curStatus.color, boxShadow: `0 0 8px ${curStatus.color}` }} />
              {curStatus.label}
            </div>

            {/* Return to Hub */}
            <Link
              href="/hub"
              style={{
                fontSize: "0.78rem",
                color: "#64748b",
                padding: "6px 12px",
                borderRadius: "8px",
                border: "1px solid rgba(100, 116, 139, 0.25)",
                textDecoration: "none",
                transition: "all 0.2s",
              }}
            >
              ← Nexus Hub
            </Link>
          </div>
        </header>

        {/* ── MAIN WORKSPACE ── */}
        <main className="jarvis-content">
          {/* LEFT: Command Terminal & Goal Input */}
          <div style={{ display: "flex", flexDirection: "column", gap: 14, overflow: "hidden" }}>
            {/* Interactive Agent Mode Selector HUD */}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "10px 16px",
                background: "rgba(6, 18, 30, 0.85)",
                border: "1px solid rgba(6, 182, 212, 0.3)",
                borderRadius: 12,
                boxShadow: "0 4px 20px rgba(0,0,0,0.5)",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                <span style={{ fontSize: "0.72rem", color: "#64748b", fontWeight: 700, letterSpacing: "1px", fontFamily: "'Orbitron', sans-serif" }}>
                  AGENT MODE:
                </span>
                <button
                  type="button"
                  onClick={() => {
                    setAgentMode("autonomous");
                    speakJarvis("Full Autonomous Agent Mode activated. I will act freely and ask only before destructive operations.");
                  }}
                  style={{
                    background: agentMode === "autonomous" ? "linear-gradient(135deg, rgba(6, 182, 212, 0.3) 0%, rgba(13, 148, 136, 0.3) 100%)" : "rgba(15, 23, 42, 0.6)",
                    border: `1px solid ${agentMode === "autonomous" ? "#06b6d4" : "rgba(100, 116, 139, 0.3)"}`,
                    color: agentMode === "autonomous" ? "#22d3ee" : "#94a3b8",
                    padding: "5px 14px",
                    borderRadius: 8,
                    fontSize: "0.74rem",
                    fontWeight: 700,
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    boxShadow: agentMode === "autonomous" ? "0 0 14px rgba(6, 182, 212, 0.35)" : "none",
                    transition: "all 0.2s",
                  }}
                >
                  <span>⚡</span> AUTONOMOUS (APPROVE DESTRUCTIVE ONLY)
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setAgentMode("assisted");
                    speakJarvis("Assisted Mode activated. Every action will require your confirmation.");
                  }}
                  style={{
                    background: agentMode === "assisted" ? "rgba(245, 158, 11, 0.2)" : "rgba(15, 23, 42, 0.6)",
                    border: `1px solid ${agentMode === "assisted" ? "#f59e0b" : "rgba(100, 116, 139, 0.3)"}`,
                    color: agentMode === "assisted" ? "#fbbf24" : "#94a3b8",
                    padding: "5px 14px",
                    borderRadius: 8,
                    fontSize: "0.74rem",
                    fontWeight: 700,
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    transition: "all 0.2s",
                  }}
                >
                  <span>🛡️</span> ASSISTED (APPROVE EVERYTHING)
                </button>
              </div>
              <span
                style={{
                  fontSize: "0.7rem",
                  color: agentMode === "autonomous" ? "#10b981" : "#f59e0b",
                  fontFamily: "'JetBrains Mono', monospace",
                  fontWeight: 600,
                  marginLeft: 12,
                }}
              >
                ● {agentMode === "autonomous" ? "FREE EXECUTION // GATED DESTRUCTIVE" : "ALL ACTIONS GATED"}
              </span>
            </div>

            {/* Goal Input Card */}
            <div className="jarvis-card" style={{ padding: "18px 20px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                <span style={{ fontFamily: "'Orbitron', sans-serif", fontSize: "0.75rem", letterSpacing: "1.5px", color: "#38bdf8", fontWeight: 700 }}>
                  COMMAND DIRECTIVE // SYSTEM GOAL
                </span>
                {isListening && (
                  <span style={{ color: "#ef4444", fontSize: "0.75rem", display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <span>🔴</span> LISTENING...
                    <div className="voice-wave-container">
                      <div className="voice-bar" />
                      <div className="voice-bar" />
                      <div className="voice-bar" />
                      <div className="voice-bar" />
                    </div>
                  </span>
                )}
              </div>

              <form onSubmit={handleRun} className="goal-box">
                <div className="goal-input-wrapper">
                  <textarea
                    className="goal-input"
                    value={goal}
                    onChange={e => setGoal(e.target.value)}
                    placeholder='Instruct SARA... e.g. "what is the weather in London and the latest Python version", "organize my desktop files", "search AI news and summarize"'
                    disabled={loading}
                    onKeyDown={e => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        handleRun();
                      }
                    }}
                  />
                  <div className="input-toolbar">
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <button
                        type="button"
                        onClick={toggleVoiceListen}
                        className={`btn-mic ${isListening ? "listening" : ""}`}
                        title={isListening ? "Click to stop listening" : "Click to speak with microphone"}
                      >
                        🎙️
                      </button>
                      <span style={{ fontSize: "0.72rem", color: "#64748b" }}>
                        {isListening ? "Listening live..." : "Click mic to speak"}
                      </span>
                    </div>

                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      {loading && (
                        <button type="button" onClick={handleStop} className="btn-stop-override">
                          ⏹ Abort Operation
                        </button>
                      )}
                      <button type="submit" disabled={loading || !goal.trim()} className="btn-execute">
                        {loading ? (
                          <>
                            <span style={{ animation: "spin-cw 1.2s linear infinite" }}>⚙️</span>
                            <span>Executing...</span>
                          </>
                        ) : (
                          <>
                            <span>▶</span>
                            <span>Engage SARA</span>
                          </>
                        )}
                      </button>
                    </div>
                  </div>
                </div>

                {/* Quick Directive Chips */}
                <div>
                  <div style={{ fontSize: "0.68rem", color: "#475569", textTransform: "uppercase", letterSpacing: "1px", marginBottom: 4 }}>
                    Direct Command Templates:
                  </div>
                  <div className="chip-row">
                    {PROMPT_SUGGESTIONS.map((item, idx) => (
                      <button
                        key={idx}
                        type="button"
                        className="chip-btn"
                        onClick={() => handleRun(undefined, item.prompt)}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                </div>
              </form>
            </div>

            {/* Live Terminal Output HUD */}
            <div className="jarvis-card terminal-container">
              <div className="terminal-header">
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <span style={{ fontFamily: "'Orbitron', sans-serif", fontSize: "0.75rem", letterSpacing: "1.5px", color: "#06b6d4", fontWeight: 700 }}>
                    JARVIS TELEMETRY & EVENT STREAM
                  </span>
                  <span style={{ fontSize: "0.68rem", color: "#64748b", fontFamily: "'JetBrains Mono', monospace" }}>
                    {taskId ? `SESSION: ${taskId.slice(0, 12)}...` : "IDLE // READY"}
                  </span>
                </div>

                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <button
                    onClick={handleClearLogs}
                    style={{
                      background: "transparent",
                      border: "1px solid rgba(100, 116, 139, 0.3)",
                      color: "#64748b",
                      fontSize: "0.7rem",
                      padding: "3px 10px",
                      borderRadius: "6px",
                      cursor: "pointer",
                    }}
                  >
                    Clear Feed
                  </button>
                  <button
                    onClick={() => {
                      if (logs.length > 0) {
                        navigator.clipboard.writeText(logs.join("\n"));
                      }
                    }}
                    style={{
                      background: "transparent",
                      border: "1px solid rgba(100, 116, 139, 0.3)",
                      color: "#64748b",
                      fontSize: "0.7rem",
                      padding: "3px 10px",
                      borderRadius: "6px",
                      cursor: "pointer",
                    }}
                  >
                    Copy Output
                  </button>
                </div>
              </div>

              <div className="terminal-body">
                {logs.length === 0 && !finalSummary ? (
                  <div style={{ height: "100%", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", color: "#334155", gap: 8 }}>
                    <div style={{ fontSize: "1.8rem" }}>📡</div>
                    <div>// Telemetry stream online. Ready for command directive.</div>
                    <div style={{ fontSize: "0.7rem", color: "#1e293b" }}>Every tool call and real output streams here live.</div>
                  </div>
                ) : (
                  logs.map((line, i) => {
                    let cls = "log-entry";
                    if (line.includes("[SARA] ▶") || line.includes("[SARA] ◀")) cls += " tool-tag";
                    else if (line.includes("[SARA]")) cls += " sara-tag";
                    else if (line.includes("Task complete") || line.includes("OK")) cls += " done-tag";
                    else if (line.includes("ERROR") || line.includes("FAILED") || line.includes("failed")) cls += " error-tag";
                    else if (line.includes("Approval") || line.includes("Denied") || line.includes("⏸")) cls += " warn-tag";
                    return (
                      <div key={i} className={cls}>
                        {line}
                      </div>
                    );
                  })
                )}
                {finalSummary && (
                  <div className="final-report">
                    <div style={{ fontFamily: "'Orbitron', sans-serif", fontSize: "0.7rem", letterSpacing: "1.5px", color: "#34d399", marginBottom: 8 }}>
                      ● MISSION REPORT
                    </div>
                    {finalSummary}
                  </div>
                )}
                <div ref={logsEndRef} />
              </div>
            </div>
          </div>

          {/* RIGHT: Tool Execution Timeline + Live Capability Matrix */}
          <div className="right-pane">
            {/* Tool Execution Timeline */}
            <div className="jarvis-card" style={{ padding: "16px 18px", display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                <span style={{ fontFamily: "'Orbitron', sans-serif", fontSize: "0.75rem", letterSpacing: "1px", color: "#38bdf8", fontWeight: 700 }}>
                  EXECUTION SEQUENCE
                </span>
                <span style={{ fontSize: "0.7rem", color: "#64748b", fontFamily: "'JetBrains Mono', monospace" }}>
                  {toolSteps.length > 0
                    ? `${completedTools}/${toolSteps.length} OK${failedTools ? ` · ${failedTools} ✗` : ""}`
                    : "No Active Task"}
                </span>
              </div>

              {toolSteps.length === 0 ? (
                <div style={{ padding: "20px 0", textAlign: "center", color: "#334155", fontSize: "0.78rem" }}>
                  Live tool calls will appear here as SARA executes them.
                </div>
              ) : (
                <div className="steps-container">
                  {toolSteps.map((s, idx) => (
                    <div key={s.call_id + idx} className={`hud-step-card ${s.status}`}>
                      <div style={{
                        width: 22, height: 22, borderRadius: "50%",
                        background: s.status === "ok" ? "rgba(16, 185, 129, 0.2)"
                          : s.status === "running" ? "rgba(6, 182, 212, 0.25)"
                          : s.status === "denied" ? "rgba(245, 158, 11, 0.2)"
                          : "rgba(239, 68, 68, 0.2)",
                        color: s.status === "ok" ? "#10b981"
                          : s.status === "running" ? "#22d3ee"
                          : s.status === "denied" ? "#fbbf24"
                          : "#f87171",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontSize: "0.68rem", fontWeight: 700, flexShrink: 0
                      }}>
                        {s.status === "ok" ? "✓" : s.status === "running" ? "◉" : s.status === "denied" ? "⊘" : "✗"}
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                          <span style={{ fontSize: "0.85rem" }}>{TOOL_ICON[s.name] || "🔧"}</span>
                          <span style={{ color: "#e2e8f0", fontWeight: 500, fontSize: "0.78rem", fontFamily: "'JetBrains Mono', monospace" }}>
                            {s.name}
                          </span>
                          {s.seconds !== undefined && (
                            <span style={{ color: "#475569", fontSize: "0.66rem" }}>{s.seconds}s</span>
                          )}
                        </div>
                        {s.args && Object.keys(s.args).length > 0 && (
                          <div style={{ color: "#64748b", fontSize: "0.66rem", marginTop: 2, fontFamily: "'JetBrains Mono', monospace", wordBreak: "break-all" }}>
                            {JSON.stringify(s.args).slice(0, 100)}
                          </div>
                        )}
                        {s.output && (
                          <div style={{
                            color: s.status === "failed" ? "#f87171" : "#38bdf8",
                            fontSize: "0.68rem",
                            marginTop: 4,
                            lineHeight: 1.4,
                          }}>
                            ↳ {s.output.slice(0, 140)}{s.output.length > 140 ? "..." : ""}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Live Capability Matrix */}
            <div className="jarvis-card" style={{ padding: "16px 18px", height: 340, display: "flex", flexDirection: "column", overflow: "hidden" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                <span style={{ fontFamily: "'Orbitron', sans-serif", fontSize: "0.75rem", letterSpacing: "1px", color: "#06b6d4", fontWeight: 700 }}>
                  SYSTEM MODULE MATRIX ({caps.length} ACTIVE)
                </span>
                <span style={{ fontSize: "0.68rem", color: "#10b981", fontWeight: 600 }}>● ALL LIVE</span>
              </div>

              {/* Category Filter */}
              <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
                {["All", "System", "Media", "Web", "Dev"].map(cat => (
                  <button
                    key={cat}
                    onClick={() => setActiveCategory(cat)}
                    style={{
                      padding: "2px 8px",
                      borderRadius: "6px",
                      fontSize: "0.68rem",
                      background: activeCategory === cat ? "rgba(6, 182, 212, 0.25)" : "rgba(30, 41, 59, 0.5)",
                      border: `1px solid ${activeCategory === cat ? "#06b6d4" : "rgba(100, 116, 139, 0.2)"}`,
                      color: activeCategory === cat ? "#fff" : "#94a3b8",
                      cursor: "pointer"
                    }}
                  >
                    {cat}
                  </button>
                ))}
              </div>

              {/* Capabilities List */}
              <div className="caps-grid">
                {filteredCapabilities.map(cap => (
                  <div
                    key={cap.id}
                    className="cap-item"
                    onClick={() => setGoal(`use ${cap.id.replace(/_/g, " ")} — `)}
                    title={`Click to start a directive with ${cap.name}`}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 2 }}>
                      <span style={{ fontSize: "1rem" }}>{cap.icon}</span>
                      <span style={{ fontSize: "0.73rem", fontWeight: 600, color: "#f1f5f9" }}>{cap.name}</span>
                    </div>
                    <div style={{ fontSize: "0.66rem", color: "#64748b", lineHeight: 1.3 }}>
                      {cap.desc}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </main>

        {/* ── HIGH-TECH APPROVAL CONFIRMATION MODAL ── */}
        {approval && (
          <div className="jarvis-modal-backdrop">
            <div className="jarvis-modal">
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
                <span style={{ fontSize: "1.4rem" }}>⚠️</span>
                <span style={{ fontFamily: "'Orbitron', sans-serif", color: "#f59e0b", fontSize: "0.95rem", fontWeight: 700 }}>
                  AUTHORIZATION PROTOCOL REQUIRED
                </span>
              </div>
              <div style={{ color: "#94a3b8", fontSize: "0.85rem", lineHeight: 1.6, marginBottom: 20 }}>
                SARA wants to execute <span style={{ color: "#fbbf24", fontFamily: "'JetBrains Mono', monospace", fontWeight: 700 }}>{approval.name}</span>. Your explicit authorization is required:
                <div style={{ marginTop: 8, padding: "10px 14px", background: "rgba(0,0,0,0.5)", borderRadius: 8, border: "1px solid rgba(245, 158, 11, 0.3)", color: "#fef3c7", fontFamily: "'JetBrains Mono', monospace", fontSize: "0.75rem", wordBreak: "break-all" }}>
                  {approval.prompt}
                </div>
              </div>
              <div style={{ display: "flex", gap: 12 }}>
                <button
                  onClick={() => handleApprove("APPROVE")}
                  style={{
                    flex: 1, padding: "10px", borderRadius: 10,
                    background: "linear-gradient(135deg, #059669, #047857)",
                    border: "none", color: "#fff", fontWeight: 600, fontSize: "0.85rem", cursor: "pointer"
                  }}
                >
                  ✓ Authorize Execution
                </button>
                <button
                  onClick={() => handleApprove("DENY")}
                  style={{
                    flex: 1, padding: "10px", borderRadius: 10,
                    background: "rgba(239, 68, 68, 0.2)",
                    border: "1px solid rgba(239, 68, 68, 0.4)",
                    color: "#fca5a5", fontWeight: 600, fontSize: "0.85rem", cursor: "pointer"
                  }}
                >
                  ✗ Deny Action
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
