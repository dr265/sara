"use client";

/**
 * SARA ULTRON - VOICE INTERFACE
 * =============================
 * The holographic-orb UI, wired to the real SARA Ultron agent.
 *
 * Live backend calls (nothing here is simulated):
 *   POST /ultron/run          start a supervised plan -> execute -> replan run
 *   GET  /ultron/events       SSE stream of that run (plan, tool calls, results)
 *   GET  /ultron/voice/status real mic / synth capability probe
 *   POST /ultron/voice/listen  server-side recording + Whisper transcription
 *   POST /ultron/voice/speak   server-side TTS with echo guard
 *   POST /ultron/voice/cancel  drop the echo guard
 *   POST /ultron/stop          halt a run in flight
 *   GET  /api/llm/health       live provider chain + credit status
 *
 * Voice input is dual-path on purpose. The browser SpeechRecognition path is
 * instant and costs nothing; the server Whisper path is the automatic
 * fallback for browsers without support (Firefox, most mobile) and for the
 * "network"/"no-speech" cases. Speech OUTPUT prefers the browser synthesiser
 * (no round trip, no API spend) and falls back to server TTS.
 *
 * No API key appears in this file. Authenticated calls go through saraFetch(),
 * and the backend rejects unauthenticated writes with 401.
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ULTRON_API_URL, saraFetch } from "@/lib/api";

declare global {
  interface Window {
    webkitSpeechRecognition: any;
    SpeechRecognition: any;
  }
}

const API = `${ULTRON_API_URL}/ultron`;

interface UltronEvent {
  type: string;
  message?: string;
  tools?: string[];
  steps?: Array<{ tool: string; rationale?: string }>;
  source?: string;
  name?: string;
  ok?: boolean;
  output?: string;
  seconds?: number;
  attempt?: number;
  status?: string;
  summary?: string;
  elapsed?: number;
}

interface VoiceStatus {
  available: boolean;
  microphone: boolean;
  microphone_detail: string;
  transcriber: boolean;
  speech_synth: boolean;
  problems: string[];
}

const STATUS_STYLE: Record<string, { label: string; color: string }> = {
  idle: { label: "STANDBY // READY", color: "#94a3b8" },
  starting: { label: "DECOMPOSING GOAL", color: "#f59e0b" },
  running: { label: "EXECUTING PLAN", color: "#22d3ee" },
  replanning: { label: "RE-PLANNING", color: "#a78bfa" },
  completed: { label: "GOAL VERIFIED", color: "#34d399" },
  completed_with_issues: { label: "COMPLETED // ISSUES", color: "#f59e0b" },
  failed: { label: "GOAL NOT MET", color: "#ef4444" },
  stopped: { label: "HALTED", color: "#f97316" },
};

export default function UltronVoiceUI() {
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [transcript, setTranscript] = useState("SYSTEM READY");
  const [response, setResponse] = useState("Tap the core and speak to Ultron.");
  const [clock, setClock] = useState("");
  const [runStatus, setRunStatus] = useState("idle");
  const [trace, setTrace] = useState<string[]>([]);
  const [voice, setVoice] = useState<VoiceStatus | null>(null);
  const [llm, setLlm] = useState<{
    active: string | null;
    live_count?: number;
    total_count?: number;
  } | null>(null);

  const recognitionRef = useRef<any>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const animationRef = useRef<number | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const taskRef = useRef<string | null>(null);
  const echoGuardRef = useRef(0);
  const utterRef = useRef<SpeechSynthesisUtterance | null>(null);
  // Read inside the SSE handler without making it a dependency of the callback.
  const runStatusRef = useRef(runStatus);
  runStatusRef.current = runStatus;

  /* --- clock --- */
  useEffect(() => {
    const update = () =>
      setClock(
        new Date().toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        }),
      );
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, []);

  /* --- capability probes --- */
  useEffect(() => {
    let cancelled = false;
    fetch(`${API}/voice/status`)
      .then((r) => r.json())
      .then((d: VoiceStatus) => {
        if (!cancelled) setVoice(d);
      })
      .catch(() => {
        if (!cancelled) setTranscript("VOICE PROBE UNAVAILABLE");
      });
    fetch(`${ULTRON_API_URL}/api/llm/health`)
      .then((r) => r.json())
      .then((d) => {
        if (!cancelled) setLlm(d);
      })
      .catch(() => {
        /* LLM health is informational only - never block the UI on it. */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /* --- 3D animated holographic orb --- */
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    let t = 0;
    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    window.addEventListener("resize", resize);
    const draw = () => {
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      ctx.clearRect(0, 0, width, height);
      const cx = width / 2;
      const cy = height / 2;
      // The orb reacts to whichever interaction is actually happening.
      const excited = listening || thinking;
      t += excited ? 0.045 : 0.022;
      const glow = ctx.createRadialGradient(
        cx, cy, 10, cx, cy, Math.min(width, height) * 0.43,
      );
      glow.addColorStop(0, "rgba(0,255,255,0.18)");
      glow.addColorStop(0.35, "rgba(0,150,255,0.08)");
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, width, height);
      for (let ring = 0; ring < 7; ring++) {
        const radius = Math.min(width, height) * (0.18 + ring * 0.035);
        ctx.beginPath();
        for (let i = 0; i <= 180; i++) {
          const a = (i / 180) * Math.PI * 2;
          const wave =
            Math.sin(a * (3 + ring) + t * (1.5 + ring * 0.2)) * (2 + ring * 0.8);
          const r = radius + wave;
          const x = cx + Math.cos(a) * r;
          const y = cy + Math.sin(a) * r * (0.38 + 0.025 * Math.sin(t + ring));
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.closePath();
        ctx.strokeStyle = `rgba(70,220,255,${0.16 + ring * 0.045})`;
        ctx.lineWidth = ring === 3 ? 2.2 : 1;
        ctx.shadowBlur = 18;
        ctx.shadowColor = "rgba(0,220,255,.7)";
        ctx.stroke();
      }
      for (let i = 0; i < 130; i++) {
        const angle = i * 2.399 + t * 0.08;
        const distance = 60 + ((i * 37) % 180) + Math.sin(t + i) * 8;
        const x = cx + Math.cos(angle) * distance;
        const y = cy + Math.sin(angle) * distance * 0.48;
        const size = 0.5 + ((Math.sin(t * 2 + i) + 1) / 2) * 1.4;
        ctx.beginPath();
        ctx.arc(x, y, size, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(100,235,255,${0.25 + ((i % 5) / 10)})`;
        ctx.shadowBlur = 8;
        ctx.shadowColor = "rgba(0,220,255,.8)";
        ctx.fill();
      }
      const coreRadius =
        68 + Math.sin(t * (excited ? 5 : 2)) * (excited ? 10 : 3);
      const core = ctx.createRadialGradient(cx, cy, 2, cx, cy, coreRadius);
      core.addColorStop(0, "rgba(245,255,255,1)");
      core.addColorStop(0.12, "rgba(120,255,255,.95)");
      core.addColorStop(0.35, "rgba(0,220,255,.65)");
      core.addColorStop(0.7, "rgba(0,110,255,.12)");
      core.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(cx, cy, coreRadius, 0, Math.PI * 2);
      ctx.fill();
      const sphere = ctx.createRadialGradient(cx - 17, cy - 17, 3, cx, cy, 58);
      sphere.addColorStop(0, "#ffffff");
      sphere.addColorStop(0.08, "#baffff");
      sphere.addColorStop(0.26, "#2ff8ff");
      sphere.addColorStop(0.55, "#008cff");
      sphere.addColorStop(1, "#02152e");
      ctx.fillStyle = sphere;
      ctx.shadowBlur = excited ? 45 : 28;
      ctx.shadowColor = "rgba(0,240,255,.9)";
      ctx.beginPath();
      ctx.arc(cx, cy, 55, 0, Math.PI * 2);
      ctx.fill();
      for (let i = 0; i < 4; i++) {
        const radius = 80 + i * 14;
        const start = t * (i % 2 ? -1 : 1) + i;
        const end = start + Math.PI * (0.7 + i * 0.12);
        ctx.beginPath();
        ctx.arc(cx, cy, radius, start, end);
        ctx.strokeStyle = `rgba(100,245,255,${0.7 - i * 0.12})`;
        ctx.lineWidth = i === 0 ? 3 : 1.3;
        ctx.shadowBlur = 15;
        ctx.shadowColor = "#00eaff";
        ctx.stroke();
      }
      animationRef.current = requestAnimationFrame(draw);
    };
    draw();
    return () => {
      window.removeEventListener("resize", resize);
      if (animationRef.current) cancelAnimationFrame(animationRef.current);
    };
  }, [listening, thinking]);

  /* --- speech output --- */
  const stopSpeaking = useCallback(() => {
    if (typeof window === "undefined" || !window.speechSynthesis) return;
    window.speechSynthesis.cancel();
    utterRef.current = null;
    setSpeaking(false);
    // Tell the server to drop its echo guard too, so the next recording does
    // not transcribe our own TTS back in as user speech.
    void saraFetch(`${API}/voice/cancel`, { method: "POST" }).catch(() => {});
  }, []);

  const speak = useCallback(async (text: string) => {
    if (!text) return;
    const clean = text.replace(/[*#`_]/g, "").trim();
    if (!clean) return;
    // Echo guard: ignore whatever the mic hears for the length of the reply.
    echoGuardRef.current = Date.now() + Math.min(clean.length * 70, 30000);
    if (typeof window !== "undefined" && window.speechSynthesis) {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(clean.slice(0, 900));
      utterance.lang = "en-IN";
      utterance.rate = 0.95;
      utterance.pitch = 0.95;
      utterance.volume = 1;
      const clear = () => {
        setSpeaking(false);
        utterRef.current = null;
      };
      utterance.onstart = () => setSpeaking(true);
      utterance.onend = clear;
      // "interrupted"/"canceled" fire on an intentional cancel(); without this
      // handler the orb would show SPEAKING forever.
      utterance.onerror = clear;
      utterRef.current = utterance;
      window.speechSynthesis.speak(utterance);
      return;
    }
    // No browser synth (rare) - fall back to the server's TTS.
    try {
      await saraFetch(`${API}/voice/speak`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: clean.slice(0, 900) }),
      });
    } catch {
      /* silent: the text is already on screen */
    }
  }, []);

  /* --- the live Ultron run, streamed over SSE --- */
  const startRun = useCallback(
    async (goal: string) => {
      stopSpeaking();
      setThinking(true);
      setRunStatus("starting");
      setTrace([]);
      const t0 = Date.now();
      try {
        const res = await saraFetch(`${API}/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ goal, mode: "autonomous" }),
        });
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}));
          throw new Error(
            (typeof detail?.detail === "string" && detail.detail) ||
              `Ultron returned HTTP ${res.status}`,
          );
        }
        const { task_id } = await res.json();
        taskRef.current = task_id;

        const es = new EventSource(
          `${API}/events?task_id=${encodeURIComponent(task_id)}`,
        );
        esRef.current = es;

        es.onmessage = (msg) => {
          let ev: UltronEvent;
          try {
            ev = JSON.parse(msg.data);
          } catch {
            return; // keepalive / malformed frame - ignore it, stay connected
          }
          switch (ev.type) {
            case "meta":
              setTrace((p) => [
                ...p,
                `engine online - ${(ev.tools || []).length} tools available`,
              ]);
              break;
            case "log":
              if (ev.message)
                setTrace((p) => [...p, ev.message!.replace("[ULTRON] ", "")]);
              break;
            case "plan": {
              const names = (ev.steps || []).map((s) => s.tool);
              setRunStatus("running");
              setTrace((p) => [
                ...p,
                `plan (${ev.source}): ${
                  names.length ? names.join(" -> ") : "no tools needed"
                }`,
              ]);
              break;
            }
            case "tool_call":
              setTrace((p) => [...p, `> running ${ev.name}...`]);
              break;
            case "tool_result":
              setTrace((p) => [
                ...p,
                `${ev.ok ? "OK" : "FAIL"} ${ev.name} (${ev.seconds?.toFixed(
                  1,
                )}s) ${(ev.output || "").slice(0, 140).replace(/\s+/g, " ")}`,
              ]);
              break;
            case "replan":
              setRunStatus("replanning");
              setTrace((p) => [
                ...p,
                `re-planning (attempt ${ev.attempt ?? 1})...`,
              ]);
              break;
            case "done": {
              const status = ev.status || "completed";
              setRunStatus(status);
              setThinking(false);
              const summary = (ev.summary || "Ultron finished.").trim();
              setResponse(summary);
              setTrace((p) => [
                ...p,
                `${STATUS_STYLE[status]?.label ?? status} - ${(
                  ((Date.now() - t0) / 1000) ||
                  ev.elapsed ||
                  0
                ).toFixed(1)}s`,
              ]);
              es.close();
              esRef.current = null;
              void speak(summary);
              break;
            }
            default:
              break;
          }
        };

        // A dropped SSE connection must never leave the UI stuck on "WORKING".
        es.onerror = () => {
          es.close();
          esRef.current = null;
          setThinking(false);
          const s = runStatusRef.current;
          if (s === "starting" || s === "running" || s === "replanning") {
            setRunStatus("idle");
            setResponse(
              "Lost the live connection to Ultron. Check that the backend is running, then try again.",
            );
            setTrace((p) => [...p, "event stream disconnected"]);
          }
        };
      } catch (err: any) {
        setThinking(false);
        setRunStatus("failed");
        const msg = err?.message || "Could not reach Ultron.";
        setResponse(`Ultron is unreachable: ${msg}`);
        setTrace((p) => [...p, `error: ${msg}`]);
      }
    },
    [speak, stopSpeaking],
  );
/* --- submit a spoken goal to Ultron --- */
  const submit = useCallback(
    async (raw: string) => {
      const text = (raw || "").trim();
      if (!text) return;
      // While the agent is speaking, its own audio is echoing back at the mic.
      if (Date.now() < echoGuardRef.current) {
        setTranscript("ECHO GUARD - WAIT FOR THE REPLY TO FINISH");
        return;
      }
      // "stop" is a control word, not a goal - never spend an LLM call on it.
      const low = text.toLowerCase();
      if (low === "stop" || low === "halt" || low === "be quiet") {
        stopSpeaking();
        setResponse("Halted.");
        return;
      }
      await startRun(text);
    },
    [startRun, stopSpeaking],
  );

  /* --- browser speech recognition (primary, zero-latency) --- */
  useEffect(() => {
    const SpeechRecognition =
      window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) return;
    const recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = "en-IN";
    recognition.onstart = () => {
      setListening(true);
      setTranscript("LISTENING...");
    };
    recognition.onresult = (event: any) => {
      let finalText = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        finalText += event.results[i][0].transcript;
      }
      setTranscript(finalText.toUpperCase());
      if (event.results[0].isFinal) void submit(finalText.trim());
    };
    recognition.onerror = (event: any) => {
      setListening(false);
      // "no-speech" and "aborted" are normal, not failures worth alarming about.
      if (event?.error === "no-speech") {
        setTranscript("NOTHING HEARD - TRY AGAIN");
      } else if (event?.error === "not-allowed") {
        setResponse(
          "Microphone permission was denied. Allow it in the browser, then tap the core again.",
        );
      } else if (event?.error === "network") {
        // Browser recogniser dropped out - the server Whisper path still works.
        setTranscript("VOICE FALLBACK");
        void listenOnServerRef.current?.();
      } else {
        setTranscript("VOICE INPUT ERROR");
      }
    };
    recognition.onend = () => setListening(false);
    recognitionRef.current = recognition;
    return () => {
      try {
        recognition.abort();
      } catch {
        /* already stopped */
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* --- server Whisper fallback (no browser support / network error) --- */
  const listenOnServer = useCallback(async () => {
    setListening(true);
    setTranscript("RECORDING ON SERVER...");
    try {
      const res = await saraFetch(`${API}/voice/listen`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timeout: 8, phrase_limit: 12 }),
      });
      const data = await res.json().catch(() => ({}));
      setListening(false);
      if (!res.ok || !data?.ok) {
        const why =
          data?.detail?.error || data?.detail || data?.error || `HTTP ${res.status}`;
        setTranscript("VOICE INPUT ERROR");
        setResponse(
          `Server microphone unavailable: ${
            typeof why === "string" ? why : JSON.stringify(why)
          }`,
        );
        return;
      }
      const heard = (data.text || "").trim();
      if (!heard) {
        setTranscript("NOTHING HEARD - TRY AGAIN");
        return;
      }
      setTranscript(heard.toUpperCase());
      await submit(heard);
    } catch {
      setListening(false);
      setTranscript("VOICE INPUT ERROR");
      setResponse("Could not reach the voice endpoint. Is the backend running?");
    }
  }, [submit]);

  // Lets the SpeechRecognition onerror handler call the server fallback without
  // re-creating the recogniser on every render.
  const listenOnServerRef = useRef<(() => Promise<void>) | null>(null);
  listenOnServerRef.current = listenOnServer;

  const toggleListening = useCallback(() => {
    if (listening) {
      try {
        recognitionRef.current?.stop();
      } catch {
        /* not running */
      }
      return;
    }
    if (thinking) return; // one run at a time - never overlap runs
    stopSpeaking();
    if (recognitionRef.current) {
      try {
        recognitionRef.current.start();
        return;
      } catch {
        // Already started / unsupported - fall through to the server recorder.
      }
    }
    void listenOnServer();
  }, [listening, thinking, stopSpeaking, listenOnServer]);

  const halt = useCallback(async () => {
    stopSpeaking();
    const tid = taskRef.current;
    if (!tid) return;
    await saraFetch(`${API}/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: tid }),
    }).catch(() => {});
  }, [stopSpeaking]);

  // Close the SSE stream and stop any speech when the component unmounts, so
  // navigating away mid-run does not leave a dangling connection or a talking
  // orb on the next page.
  useEffect(() => {
    return () => {
      esRef.current?.close();
      if (typeof window !== "undefined") window.speechSynthesis?.cancel();
    };
  }, []);

  const st = STATUS_STYLE[runStatus] ?? STATUS_STYLE.idle;
  const voiceIn = Boolean(voice?.microphone) || Boolean(recognitionRef.current);

  return (
    <main className="jarvis">
      <div className="noise" />
      <header className="topbar">
        <Link
          href="/hub"
          className="brand"
          style={{ textDecoration: "none", color: "inherit" }}
        >
          <div className="brandMark">
            <span />
            <span />
            <span />
          </div>
          <div>
            <div className="brandTitle">ULTRON</div>
            <div className="brandSub">
              SARA NEXUS HUB // SUPERVISORY AGENT
            </div>
          </div>
        </Link>
        <div className="systemStatus">
          <span
            className="statusDot"
            style={
              voiceIn
                ? undefined
                : { background: "#f59e0b", boxShadow: "0 0 7px #f59e0b" }
            }
          />
          {voiceIn ? "ONLINE" : "VOICE OFFLINE"}
          {llm?.active && (
            <span
              className="llmBadge"
              title={`Live LLM providers: ${llm.live_count}/${llm.total_count}. Ultron fails over automatically.`}
            >
              LLM {llm.active}
              {llm.total_count ? ` ${llm.live_count}/${llm.total_count}` : ""}
            </span>
          )}
        </div>
      </header>

      <section className="hero">
        <div className="orbContainer">
          <canvas ref={canvasRef} className="orbCanvas" />
          <button
            className={`voiceButton ${listening ? "active" : ""}`}
            onClick={toggleListening}
            disabled={thinking}
            aria-label="Voice control"
            style={thinking ? { opacity: 0.7, cursor: "wait" } : undefined}
          >
            <div className="micIcon">
              <span className="micBody" />
              <span className="micLine" />
              <span className="micBase" />
            </div>
          </button>
          <div className="orbLabel">
            {listening
              ? "LISTENING"
              : thinking
                ? "WORKING"
                : speaking
                  ? "SPEAKING"
                  : "READY"}
          </div>
        </div>

        <div className="commandPanel">
          <div className="eyebrow" style={{ color: st.color }}>
            {st.label}
          </div>
          <div className="transcript">{transcript}</div>
          <div className="response">{response}</div>
          <div className="wave">
            {Array.from({ length: 34 }).map((_, i) => (
              <span
                key={i}
                style={{
                  animationDelay: `${i * 0.035}s`,
                  height: `${10 + ((Math.sin(i * 0.8) + 1) / 2) * 22}px`,
                }}
              />
            ))}
          </div>
          <div className="instruction">
            <span>*</span>
            {listening
              ? "Speak naturally..."
              : thinking
                ? "Ultron is executing the plan..."
                : "Tap the core to activate voice"}
          </div>

          {thinking && (
            <button onClick={halt} className="haltBtn">
              HALT RUN
            </button>
          )}

          {trace.length > 0 && (
            <div className="trace">
              {trace.slice(-6).map((t, i) => (
                <div key={i} className="traceLine">
                  {t}
                </div>
              ))}
            </div>
          )}
        </div>
      </section>

      <section className="metrics">
        <div className="metric">
          <span>VOICE IN</span>
          <strong>{voiceIn ? "READY" : "SERVER"}</strong>
        </div>
        <div className="metric">
          <span>VOICE OUT</span>
          <strong>{voice?.speech_synth ? "READY" : "BROWSER"}</strong>
        </div>
        <div className="metric">
          <span>LLM CHAIN</span>
          <strong>
            {llm
              ? `${llm.live_count ?? "?"}/${llm.total_count ?? "?"}`
              : "PROBING"}
          </strong>
        </div>
        <div className="metric">
          <span>RUN</span>
          <strong>{st.label.split(" ")[0]}</strong>
        </div>
      </section>

      <footer>
        <div>ULTRON VOICE INTERFACE / SARA CORE</div>
        <div>{clock}</div>
      </footer>

      <style jsx>{`
        * { box-sizing: border-box; }
        .jarvis {
          min-height: 100vh; overflow: hidden; position: relative;
          background:
            radial-gradient(circle at 50% 48%, rgba(0,141,255,0.13), transparent 25%),
            radial-gradient(circle at 50% 50%, rgba(0,255,255,0.05), transparent 43%),
            #02060d;
          color: #dffcff;
          font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }
        .jarvis::before {
          content: ""; position: absolute; inset: 0; pointer-events: none;
          background:
            linear-gradient(rgba(255,255,255,0.018) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,255,255,0.018) 1px, transparent 1px);
          background-size: 42px 42px;
          mask-image: radial-gradient(circle, black, transparent 78%);
        }
        .noise {
          position: absolute; inset: 0; opacity: 0.045; pointer-events: none;
          background-image: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(255,255,255,0.12) 3px);
        }
        .topbar {
          position: relative; z-index: 3; height: 86px;
          display: flex; align-items: center; justify-content: space-between;
          padding: 0 42px; border-bottom: 1px solid rgba(75,220,255,0.12);
          backdrop-filter: blur(18px);
        }
        .brand { display: flex; align-items: center; gap: 13px; }
        .brandMark {
          width: 34px; height: 34px; border: 1px solid rgba(77,236,255,0.55);
          border-radius: 50%; display: flex; align-items: center; justify-content: center;
          gap: 2px; transform: perspective(500px) rotateX(58deg);
          box-shadow: 0 0 16px rgba(0,220,255,0.3), inset 0 0 18px rgba(0,220,255,0.16);
        }
        .brandMark span { width: 2px; border-radius: 3px; background: #6cf7ff; box-shadow: 0 0 7px #00eaff; }
        .brandMark span:nth-child(1) { height: 10px; }
        .brandMark span:nth-child(2) { height: 18px; }
        .brandMark span:nth-child(3) { height: 13px; }
        .brandTitle { font-size: 15px; font-weight: 800; letter-spacing: 0.4em; }
        .brandSub { margin-top: 3px; color: rgba(190,245,255,0.4); font-size: 8px; letter-spacing: 0.25em; }
        .systemStatus { display: flex; align-items: center; gap: 9px; color: rgba(215,255,255,0.55); font-size: 9px; letter-spacing: 0.22em; }
        .statusDot {
          width: 7px; height: 7px; border-radius: 50%; background: #57faff;
          box-shadow: 0 0 7px #00eaff, 0 0 20px rgba(0,240,255,.8); animation: blink 1.6s infinite;
        }
        .llmBadge {
          margin-left: 6px; padding: 3px 8px; border-radius: 20px;
          border: 1px solid rgba(96,228,255,.25); background: rgba(0,220,255,.07);
          color: rgba(190,245,255,.65); letter-spacing: .12em;
        }
        .hero {
          position: relative; z-index: 2; min-height: calc(100vh - 150px);
          display: grid; place-items: center; align-content: center; gap: 8px;
        }
        .orbContainer {
.commandPanel {
          width: min(650px, 88vw); padding: 22px 26px;
          border: 1px solid rgba(96,228,255,.13); border-radius: 18px;
          background: rgba(5,17,31,.44);
          box-shadow: 0 20px 70px rgba(0,0,0,.4), inset 0 1px rgba(255,255,255,.03);
          backdrop-filter: blur(18px);
        }
        .eyebrow { font-size: 8px; letter-spacing: 0.3em; color: rgba(173,245,255,.5); margin-bottom: 8px; }
        .transcript {
          min-height: 23px; font-size: 15px; font-weight: 700; letter-spacing: 0.08em;
          color: #aefcff; text-shadow: 0 0 15px rgba(0,235,255,.45);
        }
        .response {
          margin-top: 5px; min-height: 22px; color: rgba(214,245,255,.62);
          font-size: 12px; line-height: 1.55; max-height: 132px; overflow-y: auto;
        }
        .wave {
          height: 40px; margin-top: 15px; display: flex; align-items: center;
          justify-content: center; gap: 3px; overflow: hidden;
        }
        .wave span {
          width: 2px; max-height: 27px; border-radius: 10px;
          background: rgba(90,242,255,.75); box-shadow: 0 0 8px rgba(0,235,255,.8);
          animation: wave 1.1s ease-in-out infinite alternate;
        }
        .instruction {
          display: flex; align-items: center; justify-content: center; gap: 8px;
          color: rgba(206,243,255,.32); font-size: 9px; letter-spacing: 0.18em;
        }
        .instruction span { color: #57faff; text-shadow: 0 0 8px #00eaff; }
        .haltBtn {
          margin-top: 12px; width: 100%; padding: 8px; cursor: pointer;
          background: rgba(239,68,68,.12); border: 1px solid rgba(239,68,68,.35);
          border-radius: 8px; color: #fca5a5; font-size: 9px; letter-spacing: 0.2em;
        }
        .haltBtn:hover { background: rgba(239,68,68,.2); }
        .trace {
          margin-top: 12px; padding-top: 10px; border-top: 1px solid rgba(96,228,255,.1);
          display: flex; flex-direction: column; gap: 3px;
        }
        .traceLine {
          font-size: 10px; color: rgba(150,205,225,.5);
          font-family: ui-monospace, Menlo, Consolas, monospace;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .metrics { position: fixed; bottom: 50px; left: 40px; display: flex; gap: 26px; z-index: 3; }
        .metric { display: flex; flex-direction: column; gap: 3px; }
        .metric span { color: rgba(180,235,255,.28); font-size: 7px; letter-spacing: 0.25em; }
        .metric strong { color: rgba(213,255,255,.66); font-size: 9px; letter-spacing: 0.14em; }
        footer {
          position: fixed; bottom: 20px; left: 40px; right: 40px;
          display: flex; justify-content: space-between;
          color: rgba(175,230,255,.2); font-size: 7px; letter-spacing: 0.23em; z-index: 3;
        }
        @keyframes pulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.05); } }
        @keyframes wave {
          from { transform: scaleY(.35); opacity: .45; }
          to { transform: scaleY(1); opacity: 1; }
        }
        @keyframes blink { 0%,100% { opacity: .45; } 50% { opacity: 1; } }
        @media (max-width: 700px) {
          .topbar { padding: 0 20px; }
          .orbContainer { width: 100vw; height: 420px; }
          .commandPanel { padding: 18px; }
          .metrics { display: none; }
          footer { left: 20px; right: 20px; }
          .brandSub { display: none; }
        }
      `}</style>
    </main>
  );
}
