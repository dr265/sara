"use client";

import React, {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Activity,
  AlertOctagon,
  AppWindow,
  Bot,
  BrainCircuit,
  ChevronDown,
  ChevronRight,
  CircleUserRound,
  Command,
  Cpu,
  Database,
  Expand,
  FileText,
  Gauge,
  Globe2,
  HardDrive,
  Layers3,
  LayoutDashboard,
  Mail,
  Maximize2,
  MessageSquare,
  Mic,
  Monitor,
  MoreHorizontal,
  Network,
  Pause,
  Play,
  Power,
  Radio,
  RotateCcw,
  Search,
  Send,
  Settings,
  Share2,
  Shield,
  Sparkles,
  Terminal,
  Volume2,
  WandSparkles,
  X,
  Zap,
} from "lucide-react";

/* =========================================================
   TYPES
========================================================= */

type CapabilityStatus = "LIVE" | "OFF" | "ERROR";

export type Capability = {
  name: string;
  status: CapabilityStatus;
  detail?: string;
  icon?: ReactNode;
};

export type DesktopAgentPremiumProps = {
  screenStream?: MediaStream | null;
  capabilities?: Capability[];
  initialCommand?: string;
  connected?: boolean;
  onExecute?: (command: string) => Promise<void> | void;
  onListen?: () => Promise<void> | void;
  onPause?: () => Promise<void> | void;
  onEmergencyStop?: () => Promise<void> | void;
  onShareScreen?: () => Promise<void> | void;
  onNewChat?: () => void;
  onAgentChange?: (agent: string) => void;
  onConnectGmail?: () => void;
  cpuUsage?: number;
  memoryUsed?: number;
  memoryTotal?: number;
  uptime?: string;
  viewerCount?: number;
  fps?: number;
};

type GlassPanelProps = {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
  hover?: boolean;
};

/* =========================================================
   DEFAULT DATA
========================================================= */

const DEFAULT_CAPABILITIES: Capability[] = [
  { name: "registry", status: "LIVE", icon: <Database size={14} /> },
  { name: "windows", status: "LIVE", icon: <AppWindow size={14} /> },
  { name: "powershell", status: "LIVE", icon: <Terminal size={14} /> },
  { name: "system_metrics", status: "LIVE", icon: <Gauge size={14} /> },
  { name: "llm", status: "LIVE", icon: <BrainCircuit size={14} /> },
  { name: "vision_share", status: "LIVE", icon: <Share2 size={14} /> },
  {
    name: "vision",
    status: "OFF",
    detail: "SARA_SEND_SCREENSHOTS=1 + SARA_VISION_MODEL required",
    icon: <EyeIcon />,
  },
  { name: "netsh", status: "LIVE", icon: <Network size={14} /> },
  { name: "winget", status: "LIVE", icon: <PackageIcon /> },
  { name: "tzutil", status: "LIVE", icon: <Globe2 size={14} /> },
  { name: "shutdown_exec", status: "LIVE", icon: <Power size={14} /> },
  { name: "nvidia_smi", status: "LIVE", icon: <Cpu size={14} /> },
];

function EyeIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function PackageIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="m16.5 9.4-9-5.19" />
      <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z" />
      <path d="M3.3 7 12 12l8.7-5" />
      <path d="M12 22V12" />
    </svg>
  );
}

/* =========================================================
   GLASS PANEL
========================================================= */

function GlassPanel({ children, className = "", style, hover = false }: GlassPanelProps) {
  return (
    <motion.div
      whileHover={hover ? { y: -2, transition: { duration: 0.18 } } : undefined}
      className={[
        "relative overflow-hidden rounded-[26px]",
        "bg-[rgba(41,41,43,0.955)]",
        "backdrop-blur-[26px] backdrop-saturate-[112%]",
        "shadow-[inset_0_0_0_1px_rgba(214,228,255,0.14),0_22px_60px_rgba(0,0,0,0.30)]",
        className,
      ].join(" ")}
      style={style}
    >
      <div className="relative z-10 h-full">{children}</div>
    </motion.div>
  );
}

/* =========================================================
   STATUS PILL
========================================================= */

function StatusPill({ status }: { status: CapabilityStatus }) {
  const live = status === "LIVE";
  const error = status === "ERROR";

  return (
    <div
      className={[
        "inline-flex items-center gap-2 rounded-full px-2.5 py-1",
        "border text-[10px] font-semibold tracking-[0.12em]",
        live
          ? "border-[rgba(255,255,255,0.1)] bg-[rgba(255,255,255,0.05)] text-white"
          : error
          ? "border-red-400/20 bg-red-500/[0.08] text-red-400"
          : "border-[rgba(255,255,255,0.05)] bg-[rgba(255,255,255,0.02)] text-[#8B8C8E]",
      ].join(" ")}
    >
      <span
        className={[
          "h-1.5 w-1.5 rounded-full",
          live ? "bg-white shadow-[0_0_10px_rgba(255,255,255,0.8)]" : error ? "bg-red-400" : "bg-[#8B8C8E]",
        ].join(" ")}
      />
      {status}
    </div>
  );
}

/* =========================================================
   COMMAND CONSOLE
========================================================= */

function CommandConsole({ command, setCommand, onExecute, onListen, disabled }: any) {
  const [running, setRunning] = useState(false);
  const execute = async () => {
    if (!command.trim() || running) return;
    try {
      setRunning(true);
      await onExecute?.(command.trim());
      setCommand("");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex items-center justify-between gap-3 w-full">
      <GlassPanel className="flex-1 rounded-[26px]">
        <div className="flex min-h-[60px] items-center px-4 gap-3">
          <input
            value={command}
            onChange={(e) => setCommand(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && execute()}
            placeholder="Instruct SARA to control your computer..."
            className="flex-1 bg-transparent text-[15px] font-medium text-[#E1E1E2] outline-none placeholder:text-[#8B8C8E]"
          />
        </div>
      </GlassPanel>

      <div className="flex items-center gap-3 shrink-0">
        <button
          onClick={() => onListen?.()}
          className="w-[48px] h-[48px] rounded-full flex items-center justify-center transition-colors bg-[rgba(41,41,43,0.95)] backdrop-blur-[26px] shadow-[inset_0_0_0_1px_rgba(214,228,255,0.14),0_12px_30px_rgba(0,0,0,0.2)] text-[#A9AAAD] hover:bg-[rgba(255,255,255,0.1)] hover:text-white"
        >
          <Mic size={18} />
        </button>
        <button
          onClick={execute}
          disabled={disabled || running}
          className="w-[48px] h-[48px] rounded-full flex items-center justify-center transition-all bg-[linear-gradient(163deg,#FBBC94_0%,#F49D70_46%,#E88654_100%)] shadow-[0_3px_12px_rgba(210,110,60,0.34)] hover:brightness-110 active:scale-95 disabled:opacity-50 disabled:grayscale"
        >
          {running ? <RotateCcw size={18} className="animate-spin text-white" /> : <Send size={18} className="text-white translate-y-[1px]" />}
        </button>
      </div>
    </div>
  );
}

/* =========================================================
   LIVE DESKTOP PRESENTATION
========================================================= */

function LiveDesktopPresentation({ stream, onShareScreen, viewerCount, fps }: any) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [presenting, setPresenting] = useState(Boolean(stream));

  useEffect(() => {
    setPresenting(Boolean(stream));
    if (videoRef.current && stream) {
      videoRef.current.srcObject = stream;
      videoRef.current.play().catch(() => {});
    }
  }, [stream]);

  return (
    <GlassPanel className="overflow-hidden flex flex-col min-h-[380px]">
      <div className="flex items-center justify-between border-b border-[rgba(255,255,255,0.05)] px-5 py-4 bg-[rgba(255,255,255,0.02)]">
        <div className="flex items-center gap-3">
          <span className="text-[11px] font-semibold tracking-[0.18em] text-[#98999C] uppercase">Live Optical Feed</span>
          {presenting && (
            <span className="flex items-center gap-2 rounded-full border border-orange-400/20 bg-orange-400/10 px-2.5 py-1 text-[9px] uppercase tracking-[0.12em] text-orange-400">
              <Radio size={11} /> LIVE
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-[12px] text-[#8B8C8E]">
          <label className="flex items-center gap-2 cursor-pointer hover:text-white transition-colors">
            <input type="checkbox" checked={presenting} onChange={() => setPresenting((v) => !v)} className="accent-white" />
            Stream Desktop
          </label>
        </div>
      </div>

      <div className="relative p-5 flex-1 flex flex-col">
        <div className="relative flex-1 w-full overflow-hidden rounded-[16px] bg-black/80 border border-[rgba(255,255,255,0.05)] flex items-center justify-center">
          {stream && presenting ? (
            <video ref={videoRef} autoPlay muted playsInline className="h-full w-full object-contain" />
          ) : (
            <div className="flex flex-col items-center justify-center text-center p-6">
              <Monitor size={32} className="text-[#52525B] mb-4" />
              <div className="text-[14px] font-medium text-white/60 mb-2">Desktop presentation offline</div>
              <div className="text-[12px] text-[#52525B] max-w-[260px] mb-6">Connect the existing SARA desktop stream to render the live presentation here.</div>
              <button
                onClick={() => onShareScreen?.()}
                className="flex items-center gap-2 rounded-[12px] border border-[rgba(255,255,255,0.1)] bg-[rgba(255,255,255,0.05)] px-4 py-2.5 text-[12px] font-medium text-white transition hover:bg-[rgba(255,255,255,0.1)]"
              >
                <Share2 size={14} /> Start Presentation
              </button>
            </div>
          )}
        </div>
      </div>
    </GlassPanel>
  );
}

/* =========================================================
   CAPABILITIES
========================================================= */

function CapabilityPanel({ capabilities }: { capabilities: Capability[] }) {
  const liveCount = capabilities.filter((cap) => cap.status === "LIVE").length;

  return (
    <GlassPanel className="h-full flex flex-col max-h-[380px]">
      <div className="flex items-center justify-between border-b border-[rgba(255,255,255,0.05)] px-5 py-4 bg-[rgba(255,255,255,0.02)]">
        <div className="text-[11px] font-semibold tracking-[0.16em] text-[#98999C] uppercase">System Capabilities</div>
        <div className="flex items-center gap-2 text-[10px] font-medium text-white">
          <span className="h-1.5 w-1.5 rounded-full bg-white shadow-[0_0_10px_rgba(255,255,255,0.8)]" />
          {liveCount} LIVE
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-3 [scrollbar-width:thin]">
        <div className="space-y-1.5">
          {capabilities.map((cap) => (
            <div key={cap.name} className="flex items-center gap-3 rounded-[12px] border border-transparent px-3 py-3 transition hover:border-[rgba(255,255,255,0.05)] hover:bg-[rgba(255,255,255,0.02)]">
              <StatusPill status={cap.status} />
              <span className={cap.status === "LIVE" ? "text-white" : "text-[#52525B]"}>{cap.icon}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-[#E1E1E2]">{cap.name}</span>
            </div>
          ))}
        </div>
      </div>
    </GlassPanel>
  );
}

/* =========================================================
   TELEMETRY HUD
========================================================= */

function Telemetry({ connected, capabilities, cpuUsage, memoryUsed, memoryTotal, uptime }: any) {
  const liveCount = capabilities.filter((x: any) => x.status === "LIVE").length;
  const memoryLabel = typeof memoryUsed === "number" && typeof memoryTotal === "number" ? `${memoryUsed.toFixed(1)} GB / ${memoryTotal.toFixed(0)} GB` : "—";
  
  const items = [
    { label: "Status", value: `${liveCount}/${capabilities.length} Live`, icon: <Activity size={15} /> },
    { label: "CPU", value: typeof cpuUsage === "number" ? `${Math.round(cpuUsage)}%` : "—", icon: <Cpu size={15} /> },
    { label: "Memory", value: memoryLabel, icon: <HardDrive size={15} /> },
    { label: "Uptime", value: uptime ?? "—", icon: <Activity size={15} /> },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4 mt-3">
      {items.map((item) => (
        <GlassPanel key={item.label} className="rounded-[20px] px-5 py-4">
          <div className="flex items-center gap-4">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px] border border-[rgba(255,255,255,0.1)] bg-[rgba(255,255,255,0.03)] text-white">
              {item.icon}
            </div>
            <div className="min-w-0">
              <div className="truncate text-[10px] uppercase tracking-[0.14em] text-[#8B8C8E]">{item.label}</div>
              <div className="mt-1 truncate text-[13px] font-semibold text-white">{item.value}</div>
            </div>
          </div>
        </GlassPanel>
      ))}
    </div>
  );
}

/* =========================================================
   MAIN COMPONENT
========================================================= */

export default function DesktopAgentPremium({
  screenStream = null, capabilities = DEFAULT_CAPABILITIES, initialCommand = "", connected = true,
  onExecute, onListen, onPause, onEmergencyStop, onShareScreen, onNewChat, onAgentChange, onConnectGmail,
  cpuUsage, memoryUsed, memoryTotal, uptime, viewerCount, fps
}: DesktopAgentPremiumProps) {
  const [command, setCommand] = useState(initialCommand);

  return (
    <div className="w-full flex-1 flex flex-col p-4 md:p-6 lg:p-8 max-w-[1400px] mx-auto min-h-0 overflow-y-auto">
        
      {/* HEADER ROW */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-6 gap-4">
        <div className="flex items-center gap-3">
            <div className="flex h-9 items-center gap-2 rounded-[12px] border border-[rgba(255,255,255,0.08)] bg-[rgba(255,255,255,0.03)] px-3 text-[#98999C]">
              <Command size={14} />
              <span className="text-[11px] uppercase tracking-[0.12em] font-medium">Live Desktop Agent</span>
            </div>
            <div className="flex items-center gap-2 text-[13px] text-[#8B8C8E]">
              <span className={`h-2.5 w-2.5 rounded-full ${connected ? "bg-white shadow-[0_0_10px_rgba(255,255,255,0.5)]" : "bg-red-400"}`} />
              {connected ? "System Connected" : "System Offline"}
            </div>
        </div>
        
        <button
          onClick={() => onEmergencyStop?.()}
          className="flex items-center gap-2 rounded-[12px] border border-[rgba(255,255,255,0.1)] bg-[rgba(255,255,255,0.05)] px-4 py-2.5 text-[12px] font-medium text-white transition hover:bg-[rgba(255,255,255,0.1)]"
        >
          <AlertOctagon size={14} className="text-white" /> Stop
        </button>
      </div>

      {/* INPUT */}
      <div className="mb-6 w-full max-w-[708px] mx-auto">
         <CommandConsole command={command} setCommand={setCommand} onExecute={onExecute} onListen={onListen} disabled={!connected} />
      </div>

      {/* DASHBOARD GRID */}
      <div className="grid gap-4 min-h-[380px] xl:grid-cols-[1.8fr_1fr]">
        <LiveDesktopPresentation stream={screenStream} onShareScreen={onShareScreen} viewerCount={viewerCount} fps={fps} />
        <CapabilityPanel capabilities={capabilities} />
      </div>

      {/* TELEMETRY */}
      <Telemetry connected={connected} capabilities={capabilities} cpuUsage={cpuUsage} memoryUsed={memoryUsed} memoryTotal={memoryTotal} uptime={uptime} />
    </div>
  );
}
