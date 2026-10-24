"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Mic, Send, RotateCcw, AlertOctagon, Pause, Play, Monitor, Radio, Share2, Terminal, Activity, Camera } from "lucide-react";
import { DESKTOP_API_URL, desktopHeaders, desktopWebSocketUrl } from "@/lib/api";

const API = DESKTOP_API_URL;
const WSU = desktopWebSocketUrl();

type Evt = {
  id: string; ts: number; kind: string; task_id?: string; step?: number;
  tool?: string; agent?: string; level?: number; why?: string; args?: unknown;
  error?: string; status?: string; attempt?: number; verified?: boolean | null;
  confirmation_id?: string; action?: string; target?: string; impact?: string;
  goal?: string; output?: { image?: unknown }; argsDump?: string;
  plan?: { tool: string; why: string }[];   // task_started
  steps?: { tool: string; why: string }[];  // plan_ready
  image?: string;
  source?: string;
  answer?: string;
  question?: string;
  reason?: string;
  typed?: number;   // step_progress (typing)
  total?: number;   // step_progress (typing)
  wpm?: number;     // step_progress (typing)
  eta_s?: number;   // step_progress (typing)
  path?: string;    // step_progress (typing)
  done?: boolean;   // step_progress (typing)
};
type Conf = { confirmation_id: string; action: string; target: string; why: string; impact: string; level: number };
type HelloEvent = Evt & { capabilities?: Record<string, string> };
type RecognitionResult = { transcript: string };
type RecognitionEvent = { results: ArrayLike<ArrayLike<RecognitionResult>> };
type VoiceRecognition = {
  lang: string; continuous: boolean; interimResults: boolean;
  onstart: (() => void) | null; onend: (() => void) | null;
  onresult: ((event: RecognitionEvent) => void) | null;
  start: () => void; stop: () => void;
};
type VoiceRecognitionConstructor = new () => VoiceRecognition;

const LVL = ["Informational", "Low Risk", "User-Authorized", "Confirmation Required"];

export default function DesktopAgentPage() {
  const [caps, setCaps] = useState<Record<string, string>>({});
  const [connected, setConnected] = useState(false);
  const [goal, setGoal] = useState("");
  const [events, setEvents] = useState<Evt[]>([]);
  const [confs, setConfs] = useState<Conf[]>([]);
  const [locked, setLocked] = useState(false);
  const [paused, setPaused] = useState(false);
  const [screen, setScreen] = useState<string | null>(null);
  const [liveView, setLiveView] = useState(false);
  const [listening, setListening] = useState(false);

  // --- live screen share (browser-side, same path the trading agent uses) ---
  const [sharing, setSharing] = useState(false);
  const [relay, setRelay] = useState(true);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [banner, setBanner] = useState<{ kind: "error" | "info"; text: string } | null>(null);

  const end = useRef<HTMLDivElement>(null);
  const recognitionRef = useRef<VoiceRecognition | null>(null);
  const listeningRef = useRef(false);
  const shareVideo = useRef<HTMLVideoElement>(null);
  const shareCanvas = useRef<HTMLCanvasElement>(null);
  const shareStream = useRef<MediaStream | null>(null);
  const sharingRef = useRef(false);
  const lastFrame = useRef<string | null>(null);

  useEffect(() => {
    listeningRef.current = listening;
  }, [listening]);

  useEffect(() => {
    sharingRef.current = sharing;
  }, [sharing]);

  useEffect(() => {
    void (async () => {
      try {
        const c = await (await fetch(`${API}/capabilities`, { headers: desktopHeaders() })).json();
        setCaps(c.capabilities);
      } catch (e) {
        console.error("Agent offline", e);
        setBanner({ kind: "error", text: `Desktop agent API unreachable: ${(e as Error).message}` });
      }
    })();
  }, []);

  // --- TEXT TO SPEECH ---
  const speak = useCallback((text: string, lang = "en-IN") => {
    try {
      const synth = window.speechSynthesis;
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = lang;
      utterance.rate = 1.0;
      const voices = synth.getVoices();
      const voice = voices.find((v) => v.lang.toLowerCase().startsWith(lang.slice(0, 2).toLowerCase()));
      if (voice) utterance.voice = voice;
      synth.speak(utterance);
    } catch {
      /* speech is best-effort */
    }
  }, []);

  const speakHindi = useCallback((text: string) => speak(text, "hi-IN"), [speak]);

  useEffect(() => {
    const s = new WebSocket(WSU);
    s.onopen = () => setConnected(true);
    s.onclose = () => setConnected(false);
    s.onmessage = (m) => {
      let e: Evt;
      try {
        e = JSON.parse(m.data) as Evt;
      } catch {
        return;
      }

      // --- STREAM FRAMES: never logged into telemetry ---
      if (e.kind === "stream_frame") {
        // While THIS browser is the one sharing, its own frames are fresher than the relayed copy.
        if (e.image && !sharingRef.current) {
          setScreen(e.image);
          lastFrame.current = e.image; // keeps "Ask SARA" working from the agent-side feed too
        }
        return;
      }

      if (e.kind === "hello") { setCaps((e as HelloEvent).capabilities ?? {}); return; }

      // ONLY normal events reach here and get added to telemetry
      // Coalesce rapid typing progress into one live row instead of flooding the stream.
      if (e.kind === "step_progress") {
        setEvents((p) => {
          const last = p[p.length - 1];
          if (last && last.kind === "step_progress" && last.task_id === e.task_id && last.step === e.step)
            return [...p.slice(0, -1), e];
          return [...p.slice(-400), e];
        });
      } else {
        setEvents((p) => [...p.slice(-400), e]);
      }

      if (e.kind === "confirmation_required") setConfs((c) => [...c, e as unknown as Conf]);
      if (e.kind === "emergency_stop") setLocked(true);
      if (e.kind === "unlocked") setLocked(false);
      if (e.kind === "paused") setPaused(true);
      if (e.kind === "resumed") setPaused(false);

      // Plan failures now carry a reason — surface it instead of looking idle.
      if (e.kind === "plan_failed") setBanner({ kind: "error", text: e.error ?? "The agent could not build a plan." });
      if (e.kind === "planner_fallback") setBanner({ kind: "error", text: `Model chain unavailable — using offline rules. ${e.reason ?? ""}` });
      if (e.kind === "vision_result" && e.answer) { setAnswer(e.answer); speak(e.answer.slice(0, 300)); }
      if (e.kind === "vision_failed") setBanner({ kind: "error", text: `Vision failed: ${e.error ?? "unknown error"}` });

      // SARA speaks when a task finishes!
      if (e.kind === "task_finished") {
        if (e.status === "completed") {
          speakHindi("आपका काम हो गया है।"); // "Your work is done"
        } else {
          speakHindi("मुझे यह काम करने में समस्या हुई।"); // "I had a problem doing this"
        }
      }

      // Backup check for tools that output an image directly
      const img = e.output?.image;
      if (typeof img === "string" && img.startsWith("data:image")) setScreen(img);
    };
    return () => s.close();
  }, [speakHindi, speak]);

  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [events]);

  // --- Agent-side (OS) capture stream: the server grabs the desktop, no sharing needed ---
  useEffect(() => {
    if (!liveView || !caps.screen_capture?.startsWith("available")) return;
    let stop = false;
    const loop = async () => {
      while (!stop) {
        try {
          const r = await fetch(`${API}/tools/call`, {
            method: "POST", headers: desktopHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({ tool: "screen_capture", args: { scale: 0.4 } }),
          });
          const j: { output?: { image?: unknown } } = await r.json();
          if (typeof j.output?.image === "string") { setScreen(j.output.image); lastFrame.current = j.output.image; }
        } catch { /* backend busy, next tick */ }
        await new Promise((res) => setTimeout(res, 1200));
      }
    };
    void loop();
    return () => { stop = true; };
  }, [liveView, caps.screen_capture]);

  // --- Live screen share: sample the shared stream, show it, relay it to every viewer ---
  const grabFrame = useCallback((): string | null => {
    const v = shareVideo.current, cv = shareCanvas.current;
    if (!v || !cv || !v.videoWidth) return null;
    const scale = Math.min(1, 1280 / v.videoWidth);
    cv.width = Math.round(v.videoWidth * scale);
    cv.height = Math.round(v.videoHeight * scale);
    const ctx = cv.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(v, 0, 0, cv.width, cv.height);
    const url = cv.toDataURL("image/jpeg", 0.6);
    lastFrame.current = url;
    setScreen(url);
    return url;
  }, []);

  useEffect(() => {
    if (!sharing) return;
    let stop = false;
    const loop = async () => {
      while (!stop) {
        const frame = grabFrame();
        if (frame && relay) {
          try {
            await fetch(`${API}/vision/frame`, {
              method: "POST", headers: desktopHeaders({ "Content-Type": "application/json" }),
              body: JSON.stringify({ image: frame }),
            });
          } catch { /* relay is best-effort */ }
        }
        await new Promise((res) => setTimeout(res, 1200));
      }
    };
    void loop();
    return () => { stop = true; };
  }, [sharing, relay, grabFrame]);

  const stopShare = useCallback((clear = true) => {
    shareStream.current?.getTracks().forEach((t) => t.stop());
    shareStream.current = null;
    if (shareVideo.current) shareVideo.current.srcObject = null;
    setSharing(false);
    if (clear) setBanner(null);
  }, []);

  const startShare = async () => {
    setBanner(null);
    try {
      if (!navigator.mediaDevices?.getDisplayMedia) {
        throw new Error("this browser has no screen sharing — use Chrome/Edge, or the agent-side stream");
      }
      const stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 10 }, audio: false });
      shareStream.current = stream;
      if (shareVideo.current) {
        shareVideo.current.srcObject = stream;
        await shareVideo.current.play().catch(() => undefined);
      }
      stream.getVideoTracks()[0].onended = () => stopShare(false);
      setSharing(true);
      setBanner({ kind: "info", text: "Screen shared live — SARA can see this feed, and every connected viewer sees it too." });
    } catch (e) {
      setBanner({ kind: "error", text: `Screen share failed: ${(e as Error).message}` });
    }
  };

  const askSara = async () => {
    if (sharing) grabFrame();
    const image = lastFrame.current;
    if (!image) {
      setBanner({ kind: "error", text: "Nothing to look at yet — share your screen or enable the agent-side stream first." });
      return;
    }
    setAsking(true);
    setBanner(null);
    try {
      const r = await fetch(`${API}/vision/analyze`, {
        method: "POST", headers: desktopHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ image, question }),
      });
      const j: { answer?: string; detail?: string } = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail ?? `Agent returned ${r.status}`);
      setAnswer(j.answer ?? "");
      speak((j.answer ?? "").slice(0, 300));
    } catch (e) {
      setBanner({ kind: "error", text: `Vision failed: ${(e as Error).message}` });
    } finally {
      setAsking(false);
    }
  };

  const send = async (text: string) => {
    const g = text.trim(); if (!g) return;
    setGoal("");
    setBanner(null);

    // Announce she is starting the task
    speakHindi(`ठीक है, मैं यह कर रही हूँ: ${g}`);

    try {
      const response = await fetch(`${API}/tasks`, {
        method: "POST", headers: desktopHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ goal: g }),
      });
      const j: { status?: string; say?: string; plan?: unknown[]; detail?: string } =
        await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(j.detail ?? `Agent returned ${response.status}`);
      if (j.status === "started") {
        setBanner({ kind: "info", text: `Plan accepted (${j.plan?.length ?? 0} steps) — watch the telemetry below.` });
      } else if (j.status === "paused") {
        setBanner({ kind: "info", text: "System is paused — resume it to run tasks." });
      } else {
        const reason = j.say || "The agent could not build a plan for that.";
        setBanner({ kind: "error", text: reason });
        speak(reason.slice(0, 300));
      }
    } catch (e) {
      setBanner({ kind: "error", text: `Task failed to start: ${(e as Error).message}` });
      speak("I could not start that task.");
    }
  };

  const post = (p: string) => fetch(`${API}${p}`, { method: "POST", headers: desktopHeaders() });

  const resolve = async (id: string, approved: boolean) => {
    await fetch(`${API}/confirm`, {
      method: "POST", headers: desktopHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ confirmation_id: id, approved }),
    });
    setConfs((c) => c.filter((x) => x.confirmation_id !== id));
  };

  const stopAll = async () => {
    listeningRef.current = false;
    recognitionRef.current?.stop();
    setListening(false);
    stopShare(false);
    await post("/emergency-stop");
    setConfs([]);
    setLiveView(false);
    speakHindi("सिस्टम रोक दिया गया है"); // "System stopped"
  };

  // --- CONTINUOUS VOICE COMMAND LOGIC ---
  const toggleVoice = () => {
    if (listening) {
      listeningRef.current = false;
      recognitionRef.current?.stop();
      setListening(false);
      return;
    }

    const browserWindow = window as unknown as {
      SpeechRecognition?: VoiceRecognitionConstructor;
      webkitSpeechRecognition?: VoiceRecognitionConstructor;
    };
    const SpeechRecognition = browserWindow.SpeechRecognition || browserWindow.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setBanner({ kind: "error", text: "Speech recognition not supported in this browser. Please use Chrome/Edge or type your command." });
      return;
    }
    const recognition = new SpeechRecognition();
    recognition.lang = "en-IN";
    recognition.continuous = true; // Keeps listening until you stop it!
    recognition.interimResults = false;

    recognition.onstart = () => setListening(true);

    recognition.onend = () => {
      // If it ends but user didn't turn it off, restart it (keeps it continuous)
      if (listeningRef.current) recognition.start();
      else setListening(false);
    };

    recognition.onresult = (e) => {
      const said = e.results[e.results.length - 1][0].transcript.trim();
      if (/\b(stop|ruk jao|band karo)\b/i.test(said)) {
        void stopAll();
      } else if (said) {
        void send(said);
      }
    };

    recognitionRef.current = recognition;
    listeningRef.current = true;
    recognition.start();
  };

  const capList = Object.entries(caps);

  return (
    <div className="w-full flex-1 flex flex-col p-4 md:p-6 lg:p-8 max-w-[1500px] mx-auto min-h-0 overflow-y-auto">
      {/* HEADER */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-8 gap-4">
        <div className="flex items-center gap-4">
          <div className="flex h-11 items-center gap-2 rounded-[14px] border border-[rgba(255,255,255,0.08)] bg-[rgba(255,255,255,0.03)] px-4 text-white">
            <Terminal size={16} className="text-[#98999C]" />
            <span className="text-[13px] font-semibold tracking-[0.1em]">FULL AI DESKTOP AGENT</span>
          </div>
          <div className="flex flex-col">
            <div className="flex items-center gap-2 text-[12.5px] font-medium text-[#8B8C8E]">
              <span className={`h-2 w-2 rounded-full ${connected ? 'bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.5)]' : 'bg-red-400'}`} />
              {connected ? "System Agent Connected" : "Agent Offline"}
            </div>
            <div className="text-[11px] text-[#52525B] font-mono mt-0.5">
              {capList.filter(([, v]) => v.startsWith("available")).length}/{capList.length} Capabilities Live {sharing && " · Screen Shared Live"}
            </div>
          </div>
        </div>
        
        <div className="flex items-center gap-3">
          <button onClick={() => post(paused ? "/resume" : "/pause")} className="flex items-center gap-2 rounded-[12px] border border-[rgba(255,255,255,0.1)] bg-[rgba(255,255,255,0.05)] px-5 py-2.5 text-[12.5px] font-semibold text-white transition hover:bg-[rgba(255,255,255,0.1)]">
            {paused ? <Play size={14} /> : <Pause size={14} />} {paused ? "Resume System" : "Pause System"}
          </button>
          {locked ? (
            <button onClick={() => post("/unlock")} className="flex items-center gap-2 rounded-[12px] bg-orange-600 px-5 py-2.5 text-[12.5px] font-bold text-white transition hover:bg-orange-500 shadow-[0_4px_20px_rgba(234,88,12,0.4)]">
              Unlock Agent
            </button>
          ) : (
            <button onClick={stopAll} className="flex items-center gap-2 rounded-[12px] bg-red-600/90 px-5 py-2.5 text-[12.5px] font-bold text-white transition hover:bg-red-500 shadow-[0_4px_20px_rgba(220,38,38,0.4)]">
              <AlertOctagon size={14} /> EMERGENCY STOP
            </button>
          )}
        </div>
      </div>

      {locked && <div className="mb-6 rounded-[14px] bg-red-900/40 border border-red-500/30 p-4 text-[13px] font-medium text-red-200 backdrop-blur-md">Emergency stop active. Execution disabled.</div>}

      {banner && (
        <div className={`mb-6 rounded-[14px] border p-4 text-[13px] backdrop-blur-md ${banner.kind === 'error' ? 'bg-red-900/30 border-red-500/30 text-red-200' : 'bg-emerald-900/30 border-emerald-500/30 text-emerald-200'}`}>
          <span className="font-bold mr-2">{banner.kind === "error" ? "ISSUE" : "STATUS"}</span>{banner.text}
        </div>
      )}

      {/* INPUT */}
      <div className="mb-8 flex items-center gap-3 w-full">
        <div className="flex-1 flex items-center min-h-[64px] px-5 rounded-[22px] bg-[rgba(41,41,43,0.955)] backdrop-blur-[26px] backdrop-saturate-[112%] shadow-[inset_0_0_0_1px_rgba(214,228,255,0.14),0_22px_60px_rgba(0,0,0,0.30)]">
          <input disabled={locked} value={goal} onChange={(e) => setGoal(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void send(goal)} placeholder={listening ? "Listening continuously... Speak now!" : "Instruct SARA to control your computer..."} className="flex-1 bg-transparent text-[15px] font-medium text-[#E1E1E2] outline-none placeholder:text-[#8B8C8E]" />
        </div>
        <button disabled={locked} onClick={toggleVoice} className={`w-[64px] h-[64px] shrink-0 rounded-full flex items-center justify-center transition-all ${listening ? 'bg-fuchsia-500 text-white shadow-[0_0_20px_rgba(217,70,239,0.5)]' : 'bg-[rgba(41,41,43,0.95)] shadow-[inset_0_0_0_1px_rgba(214,228,255,0.14),0_12px_30px_rgba(0,0,0,0.2)] text-[#A9AAAD] hover:bg-[rgba(255,255,255,0.1)] hover:text-white'}`}>
          <Mic size={20} />
        </button>
        <button disabled={locked} onClick={() => void send(goal)} className="w-[64px] h-[64px] shrink-0 rounded-full flex items-center justify-center transition-all bg-[linear-gradient(163deg,#FBBC94_0%,#F49D70_46%,#E88654_100%)] shadow-[0_4px_16px_rgba(210,110,60,0.4)] hover:brightness-110 active:scale-95 disabled:opacity-50 disabled:grayscale">
          <Send size={20} className="text-white translate-y-[1px] -translate-x-[1px]" />
        </button>
      </div>

      {confs.map((c) => (
        <div key={c.confirmation_id} className="mb-6 rounded-[22px] bg-orange-950/40 border border-orange-500/30 p-6 backdrop-blur-xl shadow-2xl">
          <div className="text-orange-400 font-bold mb-4 text-[14px]">Action requires authorization · Level {c.level} ({LVL[c.level]})</div>
          <div className="grid gap-3 text-[13px]">
            <div className="flex gap-4"><span className="w-24 text-orange-200/60 font-semibold">ACTION</span><span className="text-white">{c.action}</span></div>
            <div className="flex gap-4"><span className="w-24 text-orange-200/60 font-semibold">TARGET</span><span className="text-white">{c.target}</span></div>
            <div className="flex gap-4"><span className="w-24 text-orange-200/60 font-semibold">WHY</span><span className="text-white">{c.why || "Part of the current plan"}</span></div>
            <div className="flex gap-4"><span className="w-24 text-orange-200/60 font-semibold">IMPACT</span><span className="text-white">{c.impact}</span></div>
          </div>
          <div className="flex gap-3 mt-6">
            <button onClick={() => resolve(c.confirmation_id, true)} className="px-6 py-2.5 rounded-[12px] bg-emerald-600 text-white font-bold hover:bg-emerald-500">Authorize</button>
            <button onClick={() => resolve(c.confirmation_id, false)} className="px-6 py-2.5 rounded-[12px] bg-[rgba(255,255,255,0.1)] text-white font-bold hover:bg-[rgba(255,255,255,0.15)]">Deny</button>
          </div>
        </div>
      ))}

      {/* DASHBOARD GRID */}
      <div className="grid gap-6 xl:grid-cols-[2fr_1fr] mb-6">
        {/* OPTICAL FEED */}
        <section className="rounded-[24px] bg-[rgba(41,41,43,0.955)] backdrop-blur-[26px] backdrop-saturate-[112%] shadow-[inset_0_0_0_1px_rgba(214,228,255,0.14),0_22px_60px_rgba(0,0,0,0.30)] flex flex-col overflow-hidden">
          <div className="flex items-center justify-between border-b border-[rgba(255,255,255,0.05)] px-6 py-5 bg-[rgba(255,255,255,0.02)]">
            <h2 className="text-[12px] font-bold tracking-[0.15em] text-[#98999C] uppercase flex items-center gap-2"><Camera size={14}/> Live Optical Feed · Capture</h2>
            {caps.screen_capture?.startsWith("available") ? (
              <label className="flex items-center gap-2 text-[12px] text-[#8B8C8E] cursor-pointer hover:text-white">
                <input type="checkbox" checked={liveView} disabled={locked} onChange={(e) => setLiveView(e.target.checked)} className="accent-white" /> Stream Desktop
              </label>
            ) : <span className="text-[11px] font-mono text-[#52525B]">Screen Capture Offline</span>}
          </div>
          <div className="p-6 flex-1 flex flex-col">
            <div className="relative flex-1 w-full overflow-hidden rounded-[16px] bg-[#09090b] border border-[rgba(255,255,255,0.05)] flex items-center justify-center min-h-[300px]">
              {screen ? (
                /* eslint-disable-next-line @next/next/no-img-element */
                <img src={screen} alt="desktop" className="w-full h-full object-contain rounded-[16px]" />
              ) : <div className="text-[#52525B] text-[13px] font-medium">Awaiting optical frame. Share your screen or enable the agent-side stream.</div>}
            </div>
          </div>
        </section>

        {/* CAPABILITIES */}
        <section className="rounded-[24px] bg-[rgba(41,41,43,0.955)] backdrop-blur-[26px] backdrop-saturate-[112%] shadow-[inset_0_0_0_1px_rgba(214,228,255,0.14),0_22px_60px_rgba(0,0,0,0.30)] flex flex-col overflow-hidden max-h-[500px]">
          <div className="border-b border-[rgba(255,255,255,0.05)] px-6 py-5 bg-[rgba(255,255,255,0.02)]">
            <h2 className="text-[12px] font-bold tracking-[0.15em] text-[#98999C] uppercase flex items-center gap-2"><Activity size={14}/> System Capabilities</h2>
          </div>
          <div className="p-3 overflow-y-auto [scrollbar-width:thin]">
            <div className="space-y-1.5">
              {capList.map(([k, v]) => (
                <div key={k} className="flex items-center gap-3 rounded-[12px] px-4 py-3 transition hover:bg-[rgba(255,255,255,0.02)]">
                  <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${v.startsWith('available') ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-[#52525B]/20 text-[#52525B] border border-[#52525B]/30'}`}>
                    {v.startsWith("available") ? "LIVE" : "OFF"}
                  </span>
                  <span className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-[#E1E1E2]">{k}</span>
                  {!v.startsWith("available") && <span className="text-[10px] text-[#52525B] truncate max-w-[80px]">{v.slice(9)}</span>}
                </div>
              ))}
            </div>
          </div>
        </section>
      </div>

      {/* SHARE AND ASK SECTION */}
      <section className="mb-6 rounded-[24px] bg-[rgba(41,41,43,0.955)] backdrop-blur-[26px] backdrop-saturate-[112%] shadow-[inset_0_0_0_1px_rgba(214,228,255,0.14),0_22px_60px_rgba(0,0,0,0.30)] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between border-b border-[rgba(255,255,255,0.05)] px-6 py-5 bg-[rgba(255,255,255,0.02)]">
          <h2 className="text-[12px] font-bold tracking-[0.15em] text-[#98999C] uppercase flex items-center gap-2"><Share2 size={14}/> Live Screen Share · This Browser</h2>
          <div className="flex items-center gap-4">
            <label className="flex items-center gap-2 text-[12px] text-[#8B8C8E] cursor-pointer hover:text-white">
              <input type="checkbox" checked={relay} disabled={!sharing} onChange={(e) => setRelay(e.target.checked)} className="accent-white" /> Relay to viewers
            </label>
            {sharing ? (
              <button onClick={() => stopShare()} className="px-4 py-2 rounded-[10px] bg-red-600/80 hover:bg-red-500 text-white text-[12px] font-bold transition">Stop Share</button>
            ) : (
              <button disabled={locked} onClick={() => void startShare()} className="px-4 py-2 rounded-[10px] bg-[rgba(255,255,255,0.1)] hover:bg-[rgba(255,255,255,0.15)] text-white text-[12px] font-bold transition">Share My Screen</button>
            )}
          </div>
        </div>
        <div className="p-6 grid xl:grid-cols-2 gap-8">
          <div>
            <video ref={shareVideo} muted playsInline autoPlay className={`w-full max-h-[380px] object-contain bg-[#09090b] rounded-[16px] border border-[rgba(255,255,255,0.05)] ${sharing ? 'block' : 'hidden'}`} />
            {!sharing && <div className="text-[#52525B] text-[13px] font-medium h-full min-h-[200px] bg-[#09090b] rounded-[16px] border border-[rgba(255,255,255,0.05)] flex items-center justify-center text-center p-6">Not sharing. "Share My Screen" streams a window, a tab or the whole desktop straight into SARA.</div>}
          </div>
          <div className="flex flex-col justify-center">
            <div className="text-[11px] font-bold tracking-[0.15em] text-[#98999C] mb-3 uppercase">Ask About the Shared Screen</div>
            <input value={question} placeholder="e.g. read the error dialog / where is the save button?" onChange={(e) => setQuestion(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void askSara()} className="mb-4 w-full px-5 py-3.5 rounded-[14px] bg-[rgba(0,0,0,0.2)] border border-[rgba(255,255,255,0.08)] text-[14px] text-white outline-none focus:border-[rgba(255,255,255,0.2)] placeholder:text-[#52525B]" />
            <div className="flex gap-3">
              <button disabled={asking} onClick={() => void askSara()} className="px-5 py-3 rounded-[12px] bg-fuchsia-600 hover:bg-fuchsia-500 text-white font-bold text-[13px] transition disabled:opacity-50">{asking ? "Looking..." : "Ask SARA"}</button>
              <button onClick={() => { grabFrame(); setBanner({ kind: "info", text: "Captured the current frame into the optical feed." }); }} className="px-5 py-3 rounded-[12px] bg-[rgba(255,255,255,0.05)] hover:bg-[rgba(255,255,255,0.1)] text-[#98999C] hover:text-white font-medium text-[13px] transition">Capture Frame</button>
            </div>
            {answer && (
              <div className="mt-6 p-5 rounded-[16px] bg-[#0a0f1c] border border-blue-900/30">
                <div className="text-[10px] font-bold tracking-[0.1em] text-blue-400 mb-2 uppercase">SARA\'S ANSWER</div>
                <div className="text-[13px] text-blue-100 whitespace-pre-wrap leading-relaxed">{answer}</div>
              </div>
            )}
          </div>
        </div>
        <canvas ref={shareCanvas} style={{ display: "none" }} />
      </section>

      {/* TELEMETRY */}
      <section className="rounded-[24px] bg-[rgba(41,41,43,0.955)] backdrop-blur-[26px] backdrop-saturate-[112%] shadow-[inset_0_0_0_1px_rgba(214,228,255,0.14),0_22px_60px_rgba(0,0,0,0.30)] flex flex-col overflow-hidden min-h-[300px]">
        <div className="flex items-center justify-between border-b border-[rgba(255,255,255,0.05)] px-6 py-5 bg-[rgba(255,255,255,0.02)]">
          <h2 className="text-[12px] font-bold tracking-[0.15em] text-[#98999C] uppercase flex items-center gap-2"><Terminal size={14}/> Execution Telemetry</h2>
        </div>
        <div className="p-6 overflow-y-auto font-mono text-[12.5px] max-h-[400px]">
          {events.length === 0 && <div className="text-[#52525B]">System idle.</div>}
          {events.map((e) => (
            <div key={e.id} className="flex gap-4 py-2 border-b border-[rgba(255,255,255,0.02)] hover:bg-[rgba(255,255,255,0.01)]">
              <span className="text-[#52525B] shrink-0 w-[85px]">{new Date(e.ts * 1000).toLocaleTimeString()}</span>
              <span className="shrink-0 w-[160px] font-semibold" style={{ color: color(e.kind) }}>{e.kind}</span>
              <span className="text-[#A9AAAD] whitespace-pre-wrap break-words">{describe(e)}</span>
            </div>
          ))}
          <div ref={end} />
        </div>
      </section>
    </div>
  );
}

const KV = ({ k, v }: { k: string; v: string }) => (
  <div style={{ display: "flex", gap: 10, fontSize: 13, marginTop: 4 }}>
    <span style={{ width: 72, color: "#94a3b8" }}>{k}</span>
    <span style={{ color: "#e2e8f0", wordBreak: "break-all" }}>{v}</span>
  </div>
);
const planOf = (e: Evt) => e.plan ?? e.steps ?? [];
const asText = (v: unknown) => typeof v === "string" ? v : v === undefined || v === null ? "" : JSON.stringify(v);

function describe(e: Evt): string {
  switch (e.kind) {
    case "planning": return `Synthesizing plan for: ${e.goal}`;
    case "plan_ready": return `Plan ready (${planOf(e).length} steps): ${planOf(e).map((s) => s.tool).join(" -> ")}`;
    case "plan_failed": return `No plan could be built: ${e.error}`;
    case "planner_fallback": return `Model chain unavailable — using offline rules: ${e.reason ?? ""}`;
    case "task_started": return `Plan synthesized (${planOf(e).length} steps): ${planOf(e).map((s) => s.tool).join(" -> ")}`;
    case "step_started": return `${e.agent} · ${e.tool} (L${e.level}) ${e.why ?? ""} ${asText(e.args)}`;
    case "step_progress": {
      if (e.tool !== "code") return `${e.tool} progress: ${asText(e.args)}`;
      const pct = e.total ? Math.min(100, Math.round(((e.typed ?? 0) / e.total) * 100)) : 0;
      const bar = "█".repeat(Math.round(pct / 5)) + "░".repeat(20 - Math.round(pct / 5));
      return `${e.tool} · typing ${e.path ?? "file"} [${bar}] ${pct}% (${e.typed ?? 0}/${e.total} chars${e.done ? " · done" : e.wpm ? `, ${e.wpm} wpm, eta ${e.eta_s ?? "?"}s` : ""})`;
    }
    case "step_retry": return `${e.tool} attempt ${e.attempt} failed, retrying: ${e.error}`;
    case "step_completed": return `${e.tool} execution complete${e.verified === true ? " · verified" : e.verified === false ? " · VERIFICATION FAILED" : ""}`;
    case "step_failed": return `${e.tool} failed: ${e.error}`;
    case "confirmation_required": return `Awaiting human authorization for: ${e.action}`;
    case "vision_result": return `Vision answer for "${e.question ?? "screen"}": ${(e.answer ?? "").replace(/\s+/g, " ").slice(0, 200)}`;
    case "vision_failed": return `Vision failed: ${e.error}`;
    case "task_finished": return `Task ${e.task_id}: ${e.status}`;
    case "task_cancelled": return `Task ${e.task_id} aborted`;
    case "emergency_stop": return "System halted manually";
    default: return "";
  }
}

const color = (k: string) =>
  k.includes("failed") || k === "emergency_stop" ? "#f87171"
    : k.includes("completed") || k === "task_finished" || k === "vision_result" ? "#d946ef"
    : k === "step_progress" ? "#34d399"
    : k === "confirmation_required" || k.includes("retry") || k === "planner_fallback" ? "#fbbf24" : "#60a5fa";

const S: Record<string, React.CSSProperties> = {
  page: { background: "#050505", color: "#e2e8f0", padding: "30px", fontFamily: "system-ui, sans-serif", borderRadius: "12px", height: "100%", overflowY: "auto" },
  head: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  row: { display: "flex", gap: 8, alignItems: "center" },
  title: { margin: 0, fontSize: 24, fontWeight: "bold", color: "#f3f4f6" },
  sub: { color: "#94a3b8", fontSize: 12 },
  lock: { marginTop: 12, padding: 12, borderRadius: 10, background: "#7f1d1d", fontSize: 13 },
  banner: { marginTop: 12, padding: "10px 12px", borderRadius: 10, border: "1px solid #333", fontSize: 13, lineHeight: 1.45 },
  bar: { display: "flex", gap: 10, marginTop: 20 },
  input: { flex: 1, padding: "14px 16px", borderRadius: 8, background: "#111", border: "1px solid #333", color: "#fff", fontSize: 15, outline: "none" },
  btn: { padding: "12px 18px", borderRadius: 8, border: "none", background: "#d946ef", color: "#fff", cursor: "pointer", fontSize: 13, fontWeight: "bold" },
  conf: { marginTop: 14, background: "#1c1917", border: "1px solid #b45309", borderRadius: 12, padding: 16 },
  confTitle: { color: "#fbbf24", fontWeight: 600, marginBottom: 6 },
  grid: { display: "grid", gridTemplateColumns: "2fr 1fr", gap: 16, marginTop: 24 },
  panel: { background: "#0a0a0a", border: "1px solid #1f1f1f", borderRadius: 12, padding: 16 },
  panelHead: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, gap: 12, flexWrap: "wrap" },
  h2: { margin: 0, fontSize: 12, color: "#d946ef", textTransform: "uppercase", letterSpacing: 1 },
  toggle: { fontSize: 12, color: "#94a3b8", display: "flex", gap: 6, alignItems: "center", cursor: "pointer" },
  shot: { width: "100%", borderRadius: 8, border: "1px solid #1f1f1f" },
  empty: { color: "#475569", fontSize: 13, padding: 12 },
  na: { color: "#64748b", fontSize: 11 },
  capRow: { display: "grid", gridTemplateColumns: "44px 150px 1fr", gap: 8, fontSize: 12, padding: "4px 0", borderBottom: "1px solid #111" },
  stream: { maxHeight: "300px", overflowY: "auto", fontSize: 13 },
  line: { display: "flex", gap: 10, padding: "6px 0", borderBottom: "1px dashed #1a1a1a" },
  t: { color: "#475569", fontFamily: "monospace", fontSize: 11, minWidth: 76 },
  kind: { minWidth: 148, fontFamily: "monospace", fontSize: 11 },
  det: { color: "#cbd5e1", wordBreak: "break-word" },
  mono: { fontFamily: "monospace" },
  shareGrid: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, alignItems: "start" },
  shareVideo: { width: "100%", maxHeight: 380, objectFit: "contain", background: "#000", borderRadius: 10, border: "1px solid #d946ef", boxShadow: "0 0 18px rgba(217,70,239,0.25)" },
  shareVideoIdle: { display: "none" },
  answer: { marginTop: 12, background: "#0f172a", border: "1px solid #1e293b", borderRadius: 10, padding: 12, fontSize: 13, color: "#e2e8f0" },
};
