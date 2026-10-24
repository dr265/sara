'use client';

/**
 * SARA AI NEXUS HUB — VOICE AGENT (live, WhatsApp-agent style)
 *
 * Everything talks to the real unified backend (/voice-agent/*):
 *   GET  /voice-agent/voice/status     llm/tts/stt/telephony config + run state
 *   POST /voice-agent/voice/talk       live loop: text in -> SARA reply + audio
 *   GET  /voice-agent/voice/calls      live call feed
 *   GET  /voice-agent/voice/analytics  metrics
 *   POST /voice-agent/voice/run|pause  persisted agent switch
 *   POST /voice-agent/voice/owner/command  owner console
 *   POST /voice-agent/voice/files/upload   knowledge upload (multipart)
 *   GET  /voice-agent/voice/knowledge/search
 *   GET  /voice-agent/voice/customers/{id}  customer memory
 *
 * Speech: browser mic (SpeechRecognition) -> /voice/talk -> ElevenLabs/Sarvam
 * audio played inline. Keys live in backend/.env only.
 */

import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { VOICE_AGENT_API_URL } from '@/lib/api';
import ElectionCampaignPanel from './election-campaign';

/* -------------------------------------------------------------------------- */
/*  Design tokens — identical system to the WhatsApp agent page               */
/* -------------------------------------------------------------------------- */

const css = `
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

.sara-voice{
  --ink:#0b0d10; --ink-2:#10131a; --ink-3:#161a22; --line:rgba(255,255,255,.08);
  --ivory:#f3efe6; --muted:#9aa1ab; --dim:#6b727d;
  --violet:#8b5cf6; --violet-deep:#6d3fd4;
  --green:#2fbf71; --brass:#c9a961; --red:#e05555;
  --font-sans:'Inter',ui-sans-serif,system-ui,sans-serif;
  --font-serif:'Instrument Serif',Georgia,serif;
  --font-mono:'JetBrains Mono',ui-monospace,SFMono-Regular,monospace;
  background:var(--ink); color:var(--ivory); font-family:var(--font-sans);
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
  min-height:100vh;
}
.sara-voice .serif{font-family:var(--font-serif);letter-spacing:-0.01em}
.sara-voice .mono{font-family:var(--font-mono)}
.sara-voice ::selection{background:rgba(139,92,246,.3)}
.sara-voice .grain::before{
  content:"";position:absolute;inset:0;pointer-events:none;opacity:.05;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' stitchTiles='stitch'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>");
}
.sara-voice .grid-bg{
  background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
  background-size:64px 64px;
  -webkit-mask-image:radial-gradient(ellipse 70% 60% at 50% 30%,#000 30%,transparent 75%);
  mask-image:radial-gradient(ellipse 70% 60% at 50% 30%,#000 30%,transparent 75%);
}
.sara-voice .dot{animation:blink 1.4s infinite both}
.sara-voice .dot:nth-child(2){animation-delay:.2s}.sara-voice .dot:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.25}40%{opacity:1}}
.sara-voice .pulse-ring::after{
  content:"";position:absolute;inset:-4px;border-radius:9999px;border:1px solid var(--violet);
  animation:pulseRing 2.4s ease-out infinite;opacity:0;
}
@keyframes pulseRing{0%{transform:scale(.6);opacity:.8}100%{transform:scale(1.8);opacity:0}}
.sara-voice [data-reveal]{opacity:0;transform:translateY(18px);transition:opacity .7s cubic-bezier(.2,.8,.2,1),transform .7s cubic-bezier(.2,.8,.2,1)}
.sara-voice [data-reveal].is-visible{opacity:1;transform:none}
.sara-voice .btn-primary{
  background:linear-gradient(180deg,#9f75f8,#7c4ce0);color:#fff;
  box-shadow:0 1px 0 rgba(255,255,255,.3) inset,0 -1px 0 rgba(0,0,0,.25) inset,0 8px 20px -8px rgba(139,92,246,.6);
  transition:transform .2s,box-shadow .2s,filter .2s;
}
.sara-voice .btn-primary:hover{transform:translateY(-1px);filter:brightness(1.05)}
.sara-voice .btn-primary:active{transform:translateY(0)}
.sara-voice .btn-ghost{border:1px solid var(--line);background:rgba(255,255,255,.02);transition:border-color .2s,background .2s,transform .2s}
.sara-voice .btn-ghost:hover{border-color:rgba(255,255,255,.2);background:rgba(255,255,255,.05);transform:translateY(-1px)}
.sara-voice :focus-visible{outline:2px solid var(--violet);outline-offset:3px;border-radius:6px}
.sara-voice .status-dot{animation:statusPulse 2s ease-in-out infinite}
@keyframes statusPulse{0%,100%{opacity:1}50%{opacity:.45}}
.sara-voice .wave span{display:inline-block;width:3px;border-radius:2px;background:var(--violet);animation:wave 1s ease-in-out infinite}
.sara-voice .wave span:nth-child(1){height:8px;animation-delay:0s}
.sara-voice .wave span:nth-child(2){height:14px;animation-delay:.12s}
.sara-voice .wave span:nth-child(3){height:10px;animation-delay:.24s}
.sara-voice .wave span:nth-child(4){height:16px;animation-delay:.36s}
.sara-voice .wave span:nth-child(5){height:9px;animation-delay:.48s}
@keyframes wave{0%,100%{transform:scaleY(.5)}50%{transform:scaleY(1.4)}}
@media (prefers-reduced-motion:reduce){.sara-voice *{animation:none!important;transition:none!important}.sara-voice [data-reveal]{opacity:1;transform:none}}
`;

/* -------------------------------------------------------------------------- */
/*  Types + backend hooks                                                     */
/* -------------------------------------------------------------------------- */

interface VoiceStatus {
  status: string;
  running: boolean;
  llm: { provider: string; configured: boolean };
  tts: { provider: string; configured: boolean };
  stt: { provider: string; configured: boolean };
  telephony: { provider: string; capabilities: Record<string, boolean> };
}

interface VoiceCall {
  id: string;
  customer_name: string | null;
  phone: string | null;
  direction: string;
  state: string;
  objective: string | null;
  duration_seconds: number | null;
  created_at: string;
}

interface Analytics {
  total_calls: number;
  completed_calls: number;
  failed_calls: number;
  callback_calls: number;
  total_messages: number;
}

interface TalkReply {
  reply: string;
  intent: string | null;
  audio_url: string | null;
  audio_error: string | null;
  llm_configured: boolean;
  phase?: string | null;
  state?: {
    requested_date?: string | null;
    guests?: number | null;
    selected_date?: string | null;
    booking_id?: string | null;
    phase?: string;
  } | null;
  action?: { type: string; reason?: string } | null;
}

interface KnowledgeDoc {
  id: string;
  filename: string;
  classification: string;
  confidence: number;
  processing_status: string;
  metadata_json: { size?: number };
  created_at: string;
}

function useEngineHealth() {
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);

  const check = () => {
    fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/status`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((s: VoiceStatus) => {
        setStatus(s);
        setReachable(true);
      })
      .catch(() => {
        setStatus(null);
        setReachable(false);
      });
  };

  useEffect(() => {
    check();
    const t = setInterval(check, 8000);
    return () => clearInterval(t);
  }, []);

  return { status, reachable, refresh: check };
}

function useReveal() {
  useEffect(() => {
    const els = document.querySelectorAll<HTMLElement>('.sara-voice [data-reveal]');
    const io = new IntersectionObserver(
      (entries) =>
        entries.forEach((e) => {
          if (e.isIntersecting) {
            const el = e.target as HTMLElement;
            const delay = Number(el.dataset.delay ?? 0);
            el.style.transitionDelay = `${delay}ms`;
            el.classList.add('is-visible');
            io.unobserve(el);
          }
        }),
      { threshold: 0.15, rootMargin: '0px 0px -8% 0px' },
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);
}

const Container = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <div className={`mx-auto w-full max-w-[1200px] px-5 sm:px-8 ${className}`}>{children}</div>
);

const Eyebrow = ({ children }: { children: ReactNode }) => (
  <span className="mono inline-flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-[var(--violet)]">
    <span className="h-1 w-1 rounded-full bg-[var(--violet)]" aria-hidden />
    {children}
  </span>
);

const SectionHead = ({
  eyebrow,
  title,
  body,
}: {
  eyebrow: string;
  title: ReactNode;
  body?: string;
}) => (
  <div className="max-w-2xl" data-reveal>
    <Eyebrow>{eyebrow}</Eyebrow>
    <h2 className="serif mt-4 text-[2.1rem] leading-[1.05] sm:text-[2.75rem]">{title}</h2>
    {body && <p className="mt-4 text-[15.5px] leading-relaxed text-[var(--muted)]">{body}</p>}
  </div>
);

/* -------------------------------------------------------------------------- */
/*  Speech input — browser SpeechRecognition (Chrome/Edge), like WhatsApp pg  */
/* -------------------------------------------------------------------------- */

type SpeechRecogEvent = { results: ArrayLike<ArrayLike<{ transcript: string }>> };
type SpeechRecog = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onresult: ((e: SpeechRecogEvent) => void) | null;
  start: () => void;
  stop: () => void;
};
type SpeechWindow = {
  SpeechRecognition?: new () => SpeechRecog;
  webkitSpeechRecognition?: new () => SpeechRecog;
};

function MicButton({
  onResult,
  busy,
  size = 'sm',
}: {
  onResult: (text: string) => void;
  busy: boolean;
  size?: 'sm' | 'lg';
}) {
  const [listening, setListening] = useState(false);
  const recRef = useRef<SpeechRecog | null>(null);

  useEffect(
    () => () => {
      recRef.current?.stop();
    },
    [],
  );

  const toggle = () => {
    if (busy) return;
    if (listening) {
      recRef.current?.stop();
      return;
    }
    const w = window as unknown as SpeechWindow;
    const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
    if (!Ctor) {
      alert("Voice input isn't supported in this browser. Use Chrome or Edge to talk to SARA.");
      return;
    }
    const rec = new Ctor();
    rec.lang = 'en-IN';
    rec.continuous = false;
    rec.interimResults = false;
    rec.onstart = () => setListening(true);
    rec.onend = () => {
      setListening(false);
      recRef.current = null;
    };
    rec.onerror = (e) => {
      setListening(false);
      recRef.current = null;
      if (e.error && e.error !== 'aborted' && e.error !== 'no-speech') {
        alert(`Microphone error: ${e.error}. Please try again.`);
      }
    };
    rec.onresult = (e) => {
      const text = e.results?.[0]?.[0]?.transcript?.trim();
      if (text) onResult(text);
    };
    recRef.current = rec;
    rec.start();
  };

  const dim = size === 'lg' ? 'h-10 w-10' : 'h-8 w-8';
  return (
    <button
      type="button"
      onClick={toggle}
      disabled={busy}
      aria-label={listening ? 'Stop listening' : 'Speak to SARA'}
      title={listening ? 'Listening… click to stop' : 'Hold a conversation by voice'}
      className={`grid ${dim} shrink-0 place-items-center rounded-full border transition-colors ${
        listening
          ? 'pulse-ring relative animate-pulse border-[var(--violet)]/60 bg-[var(--violet)]/20 text-[#cdb4ff]'
          : 'border-white/10 bg-[#1f2c34] text-[#8696a0] hover:border-white/25 hover:text-[#e9edef]'
      }`}
    >
      {listening ? (
        <span className="wave flex items-end gap-[2px]" aria-hidden>
          <span /><span /><span /><span /><span />
        </span>
      ) : (
        <svg
          width={size === 'lg' ? 16 : 13}
          height={size === 'lg' ? 16 : 13}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
          <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
          <line x1="12" x2="12" y1="19" y2="22" />
        </svg>
      )}
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/*  Live voice phone — WhatsApp-style chat that SPEAKS                        */
/* -------------------------------------------------------------------------- */

type ChatMsg = { role: 'caller' | 'sara'; content: string; meta?: string };

function LiveVoicePhone({ say }: { say: (ok: boolean, msg: string) => void }) {
  const [messages, setMessages] = useState<ChatMsg[]>([
    {
      role: 'sara',
      content:
        "Hi, I'm *SARA* — your hotel booking receptionist. Try: “I want to book a room for the 8th.” I check real availability, offer nearby dates, and can connect you to the receptionist.",
      meta: 'live · /voice-agent/voice/talk',
    },
  ]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [muted, setMuted] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const sessionKeyRef = useRef<string>(
    `web:${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`,
  );
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, busy, speaking]);

  const playAudio = (url: string) => {
    const base = url.startsWith('http') ? url : `${VOICE_AGENT_API_URL}${url}`;
    const audio = new Audio(base);
    audioRef.current = audio;
    setSpeaking(true);
    audio.onended = () => setSpeaking(false);
    audio.onerror = () => setSpeaking(false);
    audio.play().catch(() => setSpeaking(false));
  };

  const stopAudio = () => {
    audioRef.current?.pause();
    setSpeaking(false);
  };

  const send = useCallback(
    async (raw?: string) => {
      const text = (raw ?? input).trim();
      if (!text || busy) return;
      setInput('');
      setMessages((m) => [...m, { role: 'caller', content: text }]);
      setBusy(true);
      try {
        const res = await fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/talk`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            business_id: 'biz_demo',
            text,
            speak: !muted,
            session_key: sessionKeyRef.current,
          }),
        });
        if (!res.ok) {
          const d = await res.json().catch(() => ({}));
          throw new Error(d.detail || `HTTP ${res.status}`);
        }
        const data: TalkReply = await res.json();
        setPhase(data.phase ?? null);
        if (data.reply) {
          setMessages((m) => [
            ...m,
            {
              role: 'sara',
              content: data.reply,
              meta: `state: ${data.phase ?? 'voice'}${data.audio_error ? ` · 🔇 ${data.audio_error.slice(0, 60)}` : ''}`,
            },
          ]);
        } else {
          // Provider informational/trial message — call stays alive, nothing spoken.
          setMessages((m) => [
            ...m,
            {
              role: 'sara',
              content: '… (provider message ignored — call continues)',
              meta: `state: ${data.phase ?? 'unchanged'} · no reply needed`,
            },
          ]);
        }
        if (data.audio_url && !muted) playAudio(data.audio_url);
      } catch (e) {
        setMessages((m) => [
          ...m,
          {
            role: 'sara',
            content: `⚠️ ${(e as Error).message}. Is the unified backend running? (Looking for ${VOICE_AGENT_API_URL}/voice-agent/voice/talk)`,
          },
        ]);
      } finally {
        setBusy(false);
      }
    },
    [input, busy, muted],
  );

  return (
    <div className="shadow-device relative h-[560px] w-[272px] shrink-0 rounded-[40px] bg-[#0a0a0c] p-[9px]">
      <div className="metal-edge pointer-events-none absolute inset-0 rounded-[40px] opacity-70" aria-hidden />
      <div className="relative flex h-full w-full flex-col overflow-hidden rounded-[32px] bg-[#0b141a]">
        <div className="absolute left-1/2 top-2.5 z-10 h-[26px] w-[92px] -translate-x-1/2 rounded-full bg-black" aria-hidden />
        <div className="mono flex items-center justify-between px-6 pt-3 text-[10px] text-[#cfd6da]">
          <span>9:41</span>
          <span className="flex items-center gap-1.5">
            <span className="status-dot h-1.5 w-1.5 rounded-full bg-[#8b5cf6]" aria-hidden />
            <span className="tracking-tight">live voice</span>
          </span>
        </div>
        <div className="mt-2 flex items-center gap-2.5 border-b border-white/5 bg-[#1f2c34] px-3 py-2.5">
          <div className="grid h-8 w-8 place-items-center rounded-full bg-[var(--violet)] text-[13px] font-semibold text-white">S</div>
          <div className="min-w-0 leading-tight">
            <div className="flex items-center gap-1 text-[13px] font-medium text-[#e9edef]">
              SARA Voice
              <svg width="12" height="12" viewBox="0 0 24 24" aria-label="live"><circle cx="12" cy="12" r="11" fill="#8b5cf6" /><path d="M7 12.5l3 3 7-7" stroke="#0b141a" strokeWidth="2.4" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
            </div>
          <div className="truncate text-[10.5px] text-[#8696a0]">
            {speaking
              ? 'speaking…'
              : busy
                ? 'thinking…'
                : phase
                  ? `state: ${phase.toLowerCase().replace(/_/g, ' ')}`
                  : 'listening · replies instantly'}
          </div>
          </div>
          <button
            onClick={() => setMuted((v) => !v)}
            className="mono ml-auto rounded-md border border-white/10 px-1.5 py-1 text-[9px] text-[#8696a0] transition-colors hover:border-white/25 hover:text-[#e9edef]"
            title={muted ? 'Unmute SARA voice' : 'Mute SARA voice'}
          >
            {muted ? '🔇' : '🔊'}
          </button>
        </div>
        <div
          ref={scrollRef}
          className="flex-1 space-y-2 overflow-y-auto px-3 pt-3"
          style={{
            backgroundImage: 'radial-gradient(rgba(255,255,255,.035) 1px, transparent 1px)',
            backgroundSize: '14px 14px',
          }}
        >
          {messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === 'caller' ? 'justify-end' : 'justify-start'}`}>
              <div
                className={`max-w-[86%] whitespace-pre-line rounded-xl px-3 py-2 text-[12.5px] leading-[1.45] shadow-[0_1px_1px_rgba(0,0,0,.35)] ${
                  m.role === 'caller'
                    ? 'rounded-tr-sm bg-[#005c4b] text-[#e9fdf4]'
                    : 'rounded-tl-sm bg-[#1f2c34] text-[#e9edef]'
                }`}
              >
                {m.content}
                {m.meta && <div className="mono mt-1.5 text-[9px] text-[#8696a0]">{m.meta}</div>}
              </div>
            </div>
          ))}
          {busy && (
            <div className="flex justify-start">
              <div className="flex items-center gap-1 rounded-xl rounded-tl-sm bg-[#1f2c34] px-3 py-2.5" aria-label="SARA is thinking">
                <span className="dot h-1.5 w-1.5 rounded-full bg-[#8696a0]" />
                <span className="dot h-1.5 w-1.5 rounded-full bg-[#8696a0]" />
                <span className="dot h-1.5 w-1.5 rounded-full bg-[#8696a0]" />
              </div>
            </div>
          )}
          {speaking && (
            <div className="flex justify-start">
              <div className="flex items-center gap-2 rounded-xl rounded-tl-sm bg-[#1f2c34] px-3 py-2.5">
                <span className="wave flex items-end gap-[2px]" aria-hidden>
                  <span /><span /><span /><span /><span />
                </span>
                <button onClick={stopAudio} className="mono text-[9px] text-[#8696a0] hover:text-[#e9edef]">
                  stop
                </button>
              </div>
            </div>
          )}
        </div>
        <div className="flex items-center gap-2 bg-[#0b141a] px-3 pb-5 pt-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && send()}
            placeholder={busy ? 'SARA is replying…' : 'Try: book a room for the 8th…'}
            aria-label="Message SARA"
            className="flex-1 rounded-full bg-[#1f2c34] px-3 py-2 text-[12px] text-[#e9edef] placeholder-[#8696a0] focus:outline-none"
          />
          <MicButton onResult={(t) => send(t)} busy={busy} size="lg" />
        </div>
        <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(115deg,rgba(255,255,255,.09),rgba(255,255,255,0)_38%)]" aria-hidden />
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Run/pause chip — persisted backend switch (same UX as WhatsApp agent)     */
/* -------------------------------------------------------------------------- */

function AgentRunChip() {
  const [running, setRunning] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = () =>
    fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/status`)
      .then((r) => r.json())
      .then((d: VoiceStatus) => setRunning(Boolean(d.running)))
      .catch(() => {});

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, []);

  const toggle = async () => {
    if (busy || running === null) return;
    setBusy(true);
    try {
      const res = await fetch(
        `${VOICE_AGENT_API_URL}/voice-agent/voice/${running ? 'pause' : 'run'}`,
        { method: 'POST' },
      );
      if (res.ok) {
        const d = await res.json();
        setRunning(Boolean(d.running));
      }
    } catch {
      /* offline */
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      onClick={toggle}
      disabled={running === null || busy}
      title={
        running
          ? 'Agent is live — click to pause (callers hear nothing until you run it again)'
          : 'Agent is paused — click to run'
      }
      className={`mono inline-flex cursor-pointer items-center gap-2 rounded-full border px-3 py-1.5 text-[11px] transition-colors ${
        running === false
          ? 'border-white/15 bg-[var(--ink-3)] text-[var(--dim)]'
          : 'border-[var(--violet)]/40 bg-[var(--violet)]/10 text-[#c4a7f7]'
      } ${busy ? 'opacity-60' : ''}`}
    >
      <span
        className={`h-2 w-2 rounded-full ${
          running === null ? 'bg-[#c9a961]' : running ? 'bg-[#8b5cf6]' : 'bg-[#6b727d]'
        }`}
        aria-hidden
      />
      AGENT {running === null ? '…' : running ? 'RUNNING' : 'PAUSED'}
      <span aria-hidden>{running === null ? '' : running ? '⏸' : '▶'}</span>
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/*  Nav + Hero                                                                */
/* -------------------------------------------------------------------------- */

function Nav() {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12);
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);
  return (
    <header
      className={`fixed inset-x-0 top-0 z-50 transition-[background,border-color,backdrop-filter] duration-300 ${
        scrolled ? 'border-b border-white/5 bg-[#0b0d10]/80 backdrop-blur-md' : 'border-b border-transparent'
      }`}
    >
      <Container className="flex h-16 items-center justify-between">
        <a href="/hub" className="flex items-center gap-2.5" aria-label="Back to hub">
          <span className="grid h-7 w-7 place-items-center rounded-md bg-[var(--ivory)] text-[13px] font-semibold text-[var(--ink)]">S</span>
          <span className="text-[15px] font-medium tracking-tight">SARA</span>
          <span className="hidden text-[13px] text-[var(--dim)] sm:inline">Voice Agent · live phone</span>
        </a>
        <div className="flex items-center gap-3">
          <a href="#console" className="btn-primary rounded-full px-4 py-2 text-[13.5px] font-semibold">
            Open console
          </a>
        </div>
      </Container>
    </header>
  );
}

function Hero({
  status,
  reachable,
}: {
  status: VoiceStatus | null;
  reachable: boolean | null;
}) {
  const online = reachable && status?.status === 'ok';
  const chip = (ok: boolean | null, label: string, sub: string) => (
    <span className="mono inline-flex items-center gap-2 rounded-full border border-white/10 bg-[var(--ink-2)] px-3 py-1.5 text-[11px]">
      <span
        className={`h-2 w-2 rounded-full ${
          ok === null ? 'bg-[#c9a961]' : ok ? 'bg-[#2fbf71]' : 'bg-[#e05555]'
        }`}
        aria-hidden
      />
      {label} <span className="text-[var(--dim)]">{sub}</span>
    </span>
  );
  return (
    <section className="grain relative overflow-hidden pt-28 sm:pt-36">
      <div className="grid-bg absolute inset-0" aria-hidden />
      <div className="absolute left-1/2 top-0 h-[600px] w-[900px] -translate-x-1/2 -translate-y-1/3 rounded-full bg-[radial-gradient(closest-side,rgba(139,92,246,.12),transparent)]" aria-hidden />
      <Container className="relative grid items-center gap-12 lg:grid-cols-[1.05fr_1fr] lg:gap-6">
        <div className="max-w-[560px]">
          <div data-reveal>
            <Eyebrow>Voice agent · LLM · ElevenLabs / Sarvam · Twilio</Eyebrow>
          </div>
          <h1 className="serif mt-5 text-[2.9rem] leading-[1.02] sm:text-[3.9rem] lg:text-[4.4rem]" data-reveal data-delay="60">
            The phone line that answers <em className="text-[var(--violet)]">while your hands are full.</em>
          </h1>
          <p className="mt-6 max-w-[500px] text-[16px] leading-relaxed text-[var(--muted)]" data-reveal data-delay="120">
            Talk to SARA on the live phone — she reasons over your knowledge base,
            remembers the conversation, and replies in a real voice. Everything is
            configured from <span className="mono text-[var(--ivory)]">backend/.env</span> — no keys in code.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3" data-reveal data-delay="180">
            <a href="#console" className="btn-primary rounded-full px-5 py-3 text-[14.5px] font-semibold">
              Open the console
            </a>
          </div>
          <div className="mt-8 flex flex-wrap items-center gap-3" data-reveal data-delay="220">
            {chip(online, 'ENGINE', online ? 'ONLINE' : reachable === false ? 'UNREACHABLE' : '…')}
            {chip(status?.llm.configured ?? null, 'BRAIN', status?.llm.provider ?? 'llm')}
            {chip(status?.tts.configured ?? null, 'VOICE', status?.tts.provider ?? 'tts')}
            {chip(status?.stt.configured ?? null, 'EARS', status?.stt.provider ?? 'stt')}
            <AgentRunChip />
          </div>
          {status && !status.tts.configured && (
            <p className="mono mt-4 text-[11.5px] leading-relaxed text-[var(--brass)]" data-reveal>
              🔑 Add ELEVENLABS_API_KEY or SARVAM_API_KEY to backend/.env to give SARA her voice
              (VOICE_TTS_PROVIDER selects which one).
            </p>
          )}
        </div>
        <div className="flex justify-center" data-reveal data-delay="150">
          <LiveVoicePhone say={() => {}} />
        </div>
      </Container>
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-40 bg-gradient-to-b from-transparent to-[var(--ink)]" aria-hidden />
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/*  Owner console chat                                                        */
/* -------------------------------------------------------------------------- */

function OwnerChat() {
  const [messages, setMessages] = useState<{ role: 'you' | 'agent'; content: string }[]>([
    {
      role: 'agent',
      content:
        'Owner console ready. Ask about your voice agent — who was called, what SARA knows, or command her directly ("call Maya", "stop all calling").',
    },
  ]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, busy]);

  const send = async (raw?: string) => {
    const text = (raw ?? input).trim();
    if (!text || busy) return;
    setInput('');
    setMessages((m) => [...m, { role: 'you', content: text }]);
    setBusy(true);
    try {
      const res = await fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/owner/command`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ business_id: 'biz_demo', command: text }),
      });
      if (!res.ok) {
        // Distinguish a real backend outage from a normal "paused" refusal.
        // Showing "cannot reach the backend" for a 409 sent the user hunting a
        // server that was in fact running perfectly.
        let detail = '';
        try {
          detail = (await res.json())?.detail ?? '';
        } catch {
          /* body was not JSON */
        }
        if (res.status === 409) {
          throw new Error(
            detail || 'The voice agent is paused. Use the Run/Pause toggle to resume it.',
          );
        }
        throw new Error(`Backend returned HTTP ${res.status}${detail ? ` — ${detail}` : ''}`);
      }
      const d = await res.json();
      const reply =
        d.message ??
        (d.calls
          ? `Calls in the last 24h: ${d.calls.length}${d.calls.length ? ` — latest: ${d.calls[0].state}` : ''}`
          : d.files
            ? d.files.length
              ? `Knowledge files: ${d.files.map((f: { filename: string }) => f.filename).join(', ')}`
              : 'No knowledge files uploaded yet — add some in the Knowledge tab.'
            : JSON.stringify(d).slice(0, 300));
      setMessages((m) => [...m, { role: 'agent', content: reply }]);
    } catch (e) {
      const reason = e instanceof Error ? e.message : String(e);
      const offline =
        reason.includes('Failed to fetch') || reason.includes('NetworkError');
      setMessages((m) => [
        ...m,
        {
          role: 'agent',
          content: offline
            ? `⚠️ Cannot reach the backend at ${VOICE_AGENT_API_URL}. Start it with:\n  cd backend\n  .\\venv\\Scripts\\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000`
            : `⚠️ ${reason}`,
        },
      ]);
    } finally {
      setBusy(false);
    }
  };

  const suggestions = [
    'Who was called today?',
    'What knowledge is loaded?',
    'Stop all calling',
    'Resume calling',
  ];

  return (
    <div className="overflow-hidden rounded-2xl border border-white/10 bg-[#0b0d10]">
      <div className="flex items-center gap-2.5 border-b border-white/5 bg-[var(--ink-2)] px-5 py-3.5">
        <span className="grid h-7 w-7 place-items-center rounded-md bg-[var(--violet)]/15 text-[13px]">🛠️</span>
        <div className="leading-tight">
          <div className="text-[13.5px] font-medium">Owner console</div>
          <div className="mono text-[10.5px] text-[var(--dim)]">POST /voice-agent/voice/owner/command</div>
        </div>
      </div>
      <div ref={scrollRef} className="h-[340px] space-y-3 overflow-y-auto px-5 py-4">
        {messages.map((m, i) => (
          <div key={i} className={`flex ${m.role === 'you' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-[85%] whitespace-pre-line rounded-2xl px-4 py-2.5 text-[13.5px] leading-relaxed ${
                m.role === 'you'
                  ? 'rounded-br-sm bg-[var(--violet)] text-white'
                  : 'rounded-bl-sm border border-white/10 bg-[var(--ink-2)] text-[#e9edef]'
              }`}
            >
              {m.content}
            </div>
          </div>
        ))}
        {busy && (
          <div className="flex items-center gap-1 px-1" aria-label="SARA is typing">
            <span className="dot h-1.5 w-1.5 rounded-full bg-[#8696a0]" />
            <span className="dot h-1.5 w-1.5 rounded-full bg-[#8696a0]" />
            <span className="dot h-1.5 w-1.5 rounded-full bg-[#8696a0]" />
          </div>
        )}
      </div>
      <div className="flex flex-wrap gap-2 border-t border-white/5 px-5 py-3">
        {suggestions.map((s) => (
          <button
            key={s}
            onClick={() => send(s)}
            disabled={busy}
            className="rounded-full border border-white/10 px-3 py-1.5 text-[12px] text-[var(--muted)] transition-colors hover:border-white/25 hover:text-[var(--ivory)] disabled:opacity-50"
          >
            {s}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2 border-t border-white/5 bg-[var(--ink-2)] px-5 py-3.5">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder="Command your voice agent…"
          aria-label="Owner command"
          className="flex-1 rounded-full border border-white/10 bg-[#0a0c10] px-4 py-2.5 text-[13px] text-[#e9edef] placeholder-[#6b727d] focus:border-[var(--violet)]/50 focus:outline-none"
        />
        <MicButton onResult={(t) => send(t)} busy={busy} />
        <button
          onClick={() => send()}
          disabled={busy}
          aria-label="Send"
          className="btn-primary grid h-9 w-9 shrink-0 place-items-center rounded-full transition-opacity disabled:opacity-50"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M3 11l18-8-8 18-2-8-8-2z" /></svg>
        </button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Knowledge panel — real upload + search against the voice engine           */
/* -------------------------------------------------------------------------- */

function KnowledgePanel({ say }: { say: (ok: boolean, msg: string) => void }) {
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]);
  const [searchQ, setSearchQ] = useState('');
  const [results, setResults] = useState<{ filename: string; content: string; score: number }[] | null>(null);
  const [uploading, setUploading] = useState(false);

  const loadDocs = () => {
    // No term lists the corpus (the engine's search scores tokens, so an empty
    // query would otherwise return nothing to render).
    fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/knowledge/search?business_id=biz_demo`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => {
        const seen = new Set<string>();
        const list: KnowledgeDoc[] = [];
        for (const r of d.results ?? []) {
          if (!seen.has(r.document_id)) {
            seen.add(r.document_id);
            list.push({
              id: r.document_id,
              filename: r.filename,
              classification: r.classification,
              confidence: 1,
              processing_status: 'PROCESSED',
              metadata_json: {},
              created_at: '',
            });
          }
        }
        setDocs(list);
      })
      .catch((e) => say(false, `Could not load knowledge: ${e}`));
  };

  useEffect(() => {
    loadDocs();
  }, []);

  const uploadFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const form = new FormData();
      form.append('business_id', 'biz_demo');
      form.append('uploaded_file', file);
      const res = await fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/files/upload`, {
        method: 'POST',
        body: form,
      });
      const d = await res.json();
      if (res.ok) {
        say(true, `Added "${d.filename}" — classified as ${d.classification}.`);
        loadDocs();
      } else say(false, d.detail || 'Upload failed');
    } catch {
      say(false, 'Cannot reach the backend.');
    } finally {
      setUploading(false);
      e.target.value = '';
    }
  };

  const search = async () => {
    const term = searchQ.trim();
    if (!term) return;
    try {
      const res = await fetch(
        `${VOICE_AGENT_API_URL}/voice-agent/voice/knowledge/search?business_id=biz_demo&q=${encodeURIComponent(term)}`,
      );
      const d = await res.json();
      if (!res.ok) {
        say(false, d.detail ? JSON.stringify(d.detail) : `Search failed (${res.status})`);
        return;
      }
      setResults(d.results ?? []);
    } catch {
      say(false, 'Cannot reach the backend.');
    }
  };

  const inputCls =
    'w-full rounded-xl border border-white/10 bg-[var(--ink)] px-3.5 py-2.5 text-[13.5px] text-[var(--ivory)] outline-none transition-colors placeholder:text-[var(--dim)] focus:border-[var(--violet)]';

  return (
    <div className="grid gap-8 lg:grid-cols-2">
      <div>
        <h3 className="text-[16px] font-medium">Teach SARA your business</h3>
        <p className="mt-1.5 text-[13px] leading-relaxed text-[var(--muted)]">
          Upload PDF, CSV, TXT, JSON or Excel — prices, policies, FAQs. SARA searches this
          on every voice turn and grounds her replies in it.
        </p>
        <label className="btn-primary mt-4 inline-flex cursor-pointer rounded-full px-5 py-2.5 text-[13.5px] font-semibold">
          {uploading ? 'Uploading…' : '⬆ Upload file'}
          <input type="file" accept=".pdf,.csv,.txt,.json,.xlsx,.xlsm" onChange={uploadFile} className="hidden" disabled={uploading} />
        </label>
        <div className="mt-8">
          <h4 className="mono text-[11px] uppercase tracking-[0.16em] text-[var(--dim)]">Test retrieval</h4>
          <div className="mt-3 flex gap-2">
            <input
              value={searchQ}
              onChange={(e) => setSearchQ(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && search()}
              placeholder="Search what SARA knows…"
              className={inputCls}
            />
            <button onClick={search} className="btn-ghost shrink-0 rounded-xl px-4 text-[13px]">Search</button>
          </div>
          {results && (
            <div className="mt-4 space-y-2">
              {results.length === 0 && <p className="text-[13px] text-[var(--dim)]">No matches.</p>}
              {results.map((r, i) => (
                <div key={i} className="rounded-xl border border-white/10 bg-[var(--ink)] p-3">
                  <div className="mono text-[10px] text-[var(--dim)]">{r.filename} · score {r.score}</div>
                  <div className="mt-1 line-clamp-3 text-[13px] text-[#d7dbe0]">{r.content.slice(0, 300)}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <div>
        <h3 className="text-[16px] font-medium">Stored documents</h3>
        <div className="mt-4 space-y-2">
          {docs.length === 0 && (
            <p className="text-[13px] text-[var(--dim)]">
              Nothing found yet — uploaded documents appear here.
            </p>
          )}
          {docs.map((d) => (
            <div key={d.id} className="flex items-center justify-between rounded-xl border border-white/10 bg-[var(--ink)] px-4 py-3">
              <div className="min-w-0">
                <div className="truncate text-[13.5px] text-[#e9edef]">{d.filename}</div>
                <div className="mono mt-0.5 text-[10.5px] text-[var(--dim)]">{d.classification}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Connect tab — connect-your-own provider (Twilio / Meta WhatsApp / generic) */
/*  Mirrors the WhatsApp agent's ChannelsPanel one-for-one.                    */
/* -------------------------------------------------------------------------- */

interface VoiceChannel {
  id: string;
  name: string;
  provider: string;
  phone_number: string;
  status: string;
  enabled: boolean;
  is_primary: boolean;
  last_error: string;
  credentials_set: string[];
  webhook_url: string;
  created_at: string | null;
}

interface VoiceProviderInfo {
  id: string;
  label: string;
  icon: string;
  fields: string[];
  help: string;
}

const PROVIDER_ICON: Record<string, string> = {
  meta: '🟦',
  twilio: '🟥',
  generic: '🔗',
};

const FIELD_PLACEHOLDERS: Record<string, string> = {
  account_sid: 'ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
  auth_token: 'your Twilio auth token',
  phone_number: '+15551234567',
  access_token: 'EAAG... (Meta access token)',
  phone_number_id: '123456789012345',
  app_secret: 'your Meta app secret',
  verify_token: 'any string you choose',
  webhook_secret: 'shared secret for inbound webhooks',
  send_url: 'https://your-provider.example/send',
  send_bearer_token: 'bearer token (optional)',
};

const STATUS_DOT: Record<string, string> = {
  connected: 'bg-[var(--green)]',
  connecting: 'bg-[#c9a961]',
  error: 'bg-[var(--red)]',
  disconnected: 'bg-[#6b727d]',
};

function VoiceConnectPanel({ say }: { say: (ok: boolean, msg: string) => void }) {
  const [channels, setChannels] = useState<VoiceChannel[]>([]);
  const [providers, setProviders] = useState<VoiceProviderInfo[]>([]);
  const [loading, setLoading] = useState(true);

  /* wizard state */
  const [showWizard, setShowWizard] = useState(false);
  const [wProvider, setWProvider] = useState('twilio');
  const [wName, setWName] = useState('');
  const [wPhone, setWPhone] = useState('');
  const [wPrimary, setWPrimary] = useState(false);
  const [wCreds, setWCreds] = useState<Record<string, string>>({});
  const [wBusy, setWBusy] = useState(false);

  /* inline rename */
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameVal, setRenameVal] = useState('');

  const [testing, setTesting] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const load = () => {
    fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/channels`)
      .then((r) => r.json())
      .then((d) => {
        setChannels(d.channels || []);
        setProviders(d.providers || []);
      })
      .catch(() => say(false, 'Cannot reach the backend for channels.'))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const api = async (path: string, init?: RequestInit) => {
    const res = await fetch(`${VOICE_AGENT_API_URL}/voice-agent${path}`, init);
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((d as { detail?: string }).detail || `HTTP ${res.status}`);
    return d;
  };

  const create = async () => {
    setWBusy(true);
    try {
      await api('/voice/channels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: wName || providers.find((p) => p.id === wProvider)?.label || wProvider,
          provider: wProvider,
          phone_number: wPhone,
          primary: wPrimary,
          credentials: wCreds,
        }),
      });
      say(true, 'Channel added. Hit Test to validate it against the live provider.');
      setShowWizard(false);
      setWName('');
      setWPhone('');
      setWCreds({});
      setWPrimary(false);
      load();
    } catch (e) {
      say(false, `Create failed: ${(e as Error).message}`);
    } finally {
      setWBusy(false);
    }
  };

  const testChannel = async (id: string) => {
    setTesting(id);
    try {
      const d = (await api(`/voice/channels/${id}/test`, { method: 'POST' })) as {
        ok: boolean;
        detail?: string;
        error?: string;
      };
      say(d.ok, d.ok ? `Test OK — ${d.detail}` : `Test failed — ${d.error}`);
      load();
    } catch (e) {
      say(false, `Test failed — ${(e as Error).message}`);
    } finally {
      setTesting(null);
    }
  };

  const setPrimary = async (id: string) => {
    try {
      await api(`/voice/channels/${id}/primary`, { method: 'POST' });
      say(true, 'Primary line switched — calls and messages now flow through this connection.');
      load();
    } catch (e) {
      say(false, (e as Error).message);
    }
  };

  const toggleEnabled = async (ch: VoiceChannel) => {
    try {
      await api(`/voice/channels/${ch.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !ch.enabled }),
      });
      say(true, ch.enabled ? `“${ch.name}” disconnected.` : `“${ch.name}” connected.`);
      load();
    } catch (e) {
      say(false, (e as Error).message);
    }
  };

  const doRename = async (id: string) => {
    try {
      await api(`/voice/channels/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: renameVal.trim() }),
      });
      setRenaming(null);
      load();
    } catch (e) {
      say(false, (e as Error).message);
    }
  };

  const remove = async (ch: VoiceChannel) => {
    if (!window.confirm(`Delete “${ch.name}”? This permanently removes the connection (credentials included).`)) return;
    try {
      await api(`/voice/channels/${ch.id}`, { method: 'DELETE' });
      say(true, `“${ch.name}” deleted.`);
      load();
    } catch (e) {
      say(false, (e as Error).message);
    }
  };

  const copyWebhook = (ch: VoiceChannel) => {
    navigator.clipboard?.writeText(ch.webhook_url).catch(() => {});
    setCopied(ch.id);
    setTimeout(() => setCopied(null), 1600);
  };

  const wizard = providers.find((p) => p.id === wProvider);
  const inputCls =
    'w-full rounded-xl border border-white/10 bg-[var(--ink)] px-3.5 py-2.5 text-[13.5px] text-[var(--ivory)] outline-none transition-colors placeholder:text-[var(--dim)] focus:border-[var(--violet)]';

  return (
    <div className="space-y-6">
      {/* header row */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-[18px] font-medium">Lines & Connections</h3>
          <p className="mt-1 text-[13px] leading-relaxed text-[var(--muted)]">
            Connect your own Twilio number (calls + SMS/WhatsApp), a Meta WhatsApp Cloud number, or any
            generic provider. The <span className="text-[var(--violet)]">Primary line</span> answers every
            inbound call and message through SARA and replies from that exact number.
          </p>
        </div>
        <button onClick={() => setShowWizard((v) => !v)} className="rounded-full bg-[var(--violet)] px-5 py-2.5 text-[13.5px] font-semibold text-white transition-opacity hover:opacity-90">
          {showWizard ? '✕ Close wizard' : '＋ Add a number'}
        </button>
      </div>

      {/* setup wizard */}
      {showWizard && (
        <div className="rounded-2xl border border-white/10 bg-[var(--ink-2)] p-5 sm:p-6">
          <div className="mono text-[11px] uppercase tracking-[0.16em] text-[var(--dim)]">Setup wizard — no code, no .env, no restart</div>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <label className="text-[12.5px]">
              <span className="mb-1.5 block text-[var(--muted)]">Provider</span>
              <select
                value={wProvider}
                onChange={(e) => {
                  setWProvider(e.target.value);
                  setWCreds({});
                }}
                className={inputCls}
              >
                {providers.map((p) => (
                  <option key={p.id} value={p.id}>
                    {PROVIDER_ICON[p.id] ?? p.icon} {p.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-[12.5px]">
              <span className="mb-1.5 block text-[var(--muted)]">Display name (optional)</span>
              <input value={wName} onChange={(e) => setWName(e.target.value)} placeholder="e.g. Sales line" className={inputCls} />
            </label>
            <label className="text-[12.5px]">
              <span className="mb-1.5 block text-[var(--muted)]">Phone number (optional)</span>
              <input value={wPhone} onChange={(e) => setWPhone(e.target.value)} placeholder="+15551234567" className={inputCls} />
            </label>
            <label className="mt-1 flex items-center gap-3 text-[12.5px]">
              <input type="checkbox" checked={wPrimary} onChange={(e) => setWPrimary(e.target.checked)} className="h-4 w-4 accent-[var(--violet)]" />
              <span className="text-[var(--muted)]">Make primary immediately</span>
            </label>
          </div>

          {wizard?.fields?.length ? (
            <div className="mt-5 grid gap-4 sm:grid-cols-2">
              {wizard.fields.map((f) => (
                <label key={f} className="text-[12.5px]">
                  <span className="mb-1.5 block text-[var(--muted)]">{f.replaceAll('_', ' ')}</span>
                  <input
                    type={f.includes('secret') || f.includes('token') ? 'password' : 'text'}
                    value={wCreds[f] ?? ''}
                    onChange={(e) => setWCreds((c) => ({ ...c, [f]: e.target.value }))}
                    placeholder={FIELD_PLACEHOLDERS[f] ?? f}
                    className={inputCls}
                    autoComplete="off"
                  />
                </label>
              ))}
            </div>
          ) : null}

          {wizard && (
            <p className="mt-4 rounded-xl border border-white/10 bg-[var(--ink)] px-4 py-3 text-[12.5px] leading-relaxed text-[var(--muted)]">
              {wizard.help}
            </p>
          )}

          <div className="mt-5 flex items-center justify-end gap-3">
            <button onClick={() => setShowWizard(false)} className="rounded-full border border-white/10 px-5 py-2.5 text-[13px] text-[var(--muted)] hover:text-[var(--ivory)]">
              Cancel
            </button>
            <button
              onClick={create}
              disabled={wBusy}
              className="rounded-full bg-[var(--violet)] px-6 py-2.5 text-[13.5px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {wBusy ? 'Adding…' : 'Add channel'}
            </button>
          </div>
        </div>
      )}

      {/* channel cards */}
      {loading ? (
        <p className="text-[13px] text-[var(--dim)]">Loading connections…</p>
      ) : channels.length === 0 ? (
        <p className="rounded-xl border border-dashed border-white/15 px-5 py-8 text-center text-[13px] text-[var(--dim)]">
          No lines connected yet. Add your Twilio or Meta WhatsApp number to go live.
        </p>
      ) : (
        <div className="space-y-3">
          {channels.map((ch) => (
            <div key={ch.id} className="rounded-2xl border border-white/10 bg-[var(--ink-2)] p-4 sm:p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  {renaming === ch.id ? (
                    <div className="flex items-center gap-2">
                      <input value={renameVal} onChange={(e) => setRenameVal(e.target.value)} className={`${inputCls} max-w-[220px]`} autoFocus />
                      <button onClick={() => doRename(ch.id)} className="rounded-full bg-[var(--violet)] px-3.5 py-1.5 text-[12px] font-semibold text-white">Save</button>
                      <button onClick={() => setRenaming(null)} className="rounded-full border border-white/10 px-3.5 py-1.5 text-[12px] text-[var(--muted)]">Cancel</button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2">
                      <span>{PROVIDER_ICON[ch.provider] ?? '🔗'}</span>
                      <span className="text-[15px] font-medium text-[#e9edef]">{ch.name}</span>
                      {ch.is_primary && (
                        <span className="mono rounded-full bg-[var(--violet)]/20 px-2.5 py-0.5 text-[10px] uppercase tracking-wider text-[#c4a7f7]">Primary</span>
                      )}
                    </div>
                  )}
                  <div className="mono mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[var(--dim)]">
                    <span className="inline-flex items-center gap-1.5">
                      <span className={`inline-block h-2 w-2 rounded-full ${STATUS_DOT[ch.status] ?? 'bg-[#6b727d]'}`} />
                      {ch.status}
                    </span>
                    {ch.phone_number && <span>{ch.phone_number}</span>}
                    <span>{ch.provider}</span>
                    {ch.credentials_set.length > 0 && <span>🔑 {ch.credentials_set.length} credential(s) stored</span>}
                  </div>
                  {ch.last_error && (
                    <div className="mt-2 rounded-lg border border-[var(--red)]/25 bg-[var(--red)]/10 px-3 py-1.5 text-[11.5px] text-[var(--red)]">{ch.last_error}</div>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <button onClick={() => testChannel(ch.id)} disabled={testing === ch.id} className="rounded-full border border-white/10 px-4 py-1.5 text-[12.5px] text-[var(--muted)] hover:text-[var(--ivory)] disabled:opacity-50">
                    {testing === ch.id ? 'Testing…' : 'Test'}
                  </button>
                  <button onClick={() => copyWebhook(ch)} className="rounded-full border border-white/10 px-4 py-1.5 text-[12.5px] text-[var(--muted)] hover:text-[var(--ivory)]">
                    {copied === ch.id ? 'Copied ✓' : 'Copy webhook'}
                  </button>
                  {!ch.is_primary && (
                    <button onClick={() => setPrimary(ch.id)} className="rounded-full border border-white/10 px-4 py-1.5 text-[12.5px] text-[var(--muted)] hover:text-[var(--ivory)]">
                      Make primary
                    </button>
                  )}
                  <button onClick={() => toggleEnabled(ch)} className="rounded-full border border-white/10 px-4 py-1.5 text-[12.5px] text-[var(--muted)] hover:text-[var(--ivory)]">
                    {ch.enabled ? 'Disconnect' : 'Connect'}
                  </button>
                  <button
                    onClick={() => {
                      setRenaming(ch.id);
                      setRenameVal(ch.name);
                    }}
                    className="rounded-full border border-white/10 px-4 py-1.5 text-[12.5px] text-[var(--muted)] hover:text-[var(--ivory)]"
                  >
                    Rename
                  </button>
                  <button onClick={() => remove(ch)} className="rounded-full border border-[var(--red)]/30 px-4 py-1.5 text-[12.5px] text-[var(--red)] hover:bg-[var(--red)]/10">
                    Delete
                  </button>
                </div>
              </div>
              <div className="mono mt-3 truncate border-t border-white/5 pt-3 text-[11px] text-[var(--dim)]" title={ch.webhook_url}>
                Webhook: {ch.webhook_url}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Config tab — persona + business context (injected into every voice turn)  */
/* -------------------------------------------------------------------------- */

function VoiceConfigPanel({ say }: { say: (ok: boolean, msg: string) => void }) {
  const [persona, setPersona] = useState('');
  const [businessContext, setBusinessContext] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/config`)
      .then((r) => r.json())
      .then((d) => {
        setPersona(d.persona || '');
        setBusinessContext(d.business_context || '');
      })
      .catch(() => say(false, 'Cannot reach the backend for config.'))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async () => {
    setBusy(true);
    try {
      const p = await fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/config/persona`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ persona }),
      });
      const b = await fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/config/business-context`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ business_context: businessContext }),
      });
      if (!p.ok || !b.ok) throw new Error(`HTTP ${p.status}/${b.status}`);
      say(true, 'Saved — SARA now speaks with this persona and business knowledge on every call.');
    } catch (e) {
      say(false, `Save failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const taCls =
    'min-h-[140px] w-full resize-y rounded-xl border border-white/10 bg-[var(--ink)] px-3.5 py-2.5 text-[13.5px] leading-relaxed text-[var(--ivory)] outline-none transition-colors placeholder:text-[var(--dim)] focus:border-[var(--violet)]';

  if (loading) return <p className="text-[13px] text-[var(--dim)]">Loading configuration…</p>;

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-[18px] font-medium">Persona & Business Context</h3>
        <p className="mt-1 text-[13px] leading-relaxed text-[var(--muted)]">
          Who SARA is on the phone, and what she knows about your business. Both are injected into every
          conversation turn — no code, no restart.
        </p>
      </div>

      <label className="block text-[12.5px]">
        <span className="mb-1.5 block text-[var(--muted)]">Agent persona</span>
        <textarea
          value={persona}
          onChange={(e) => setPersona(e.target.value)}
          placeholder="You are Priya, a warm and professional appointment coordinator for a dental clinic. Keep replies short and natural for a phone call."
          className={taCls}
        />
      </label>

      <label className="block text-[12.5px]">
        <span className="mb-1.5 block text-[var(--muted)]">Business context</span>
        <textarea
          value={businessContext}
          onChange={(e) => setBusinessContext(e.target.value)}
          placeholder="Nexora Health sells wellness plans. Pricing starts at $49/mo. Demos can be booked Tue–Fri. Escalate billing disputes to a human."
          className={taCls}
        />
      </label>

      <div className="flex justify-end">
        <button
          onClick={save}
          disabled={busy}
          className="rounded-full bg-[var(--violet)] px-6 py-2.5 text-[13.5px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {busy ? 'Saving…' : 'Save configuration'}
        </button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Memory tab — persistent customer memory                                    */
/* -------------------------------------------------------------------------- */

interface MemoryCustomer {
  id: string;
  name: string | null;
  phone: string;
  intent: string | null;
  sentiment: string | null;
  lifecycle_stage: string;
  qualification_score: number | null;
  next_action: string | null;
  memory: Record<string, unknown>;
  call_count: number;
  message_count: number;
  last_call_at: string | null;
  updated_at: string | null;
}

interface MemoryEvent {
  id: string;
  event_type: string;
  actor: string | null;
  timestamp: string | null;
}

function VoiceMemoryPanel() {
  const [customers, setCustomers] = useState<MemoryCustomer[]>([]);
  const [events, setEvents] = useState<MemoryEvent[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/memory?limit=50`)
      .then((r) => r.json())
      .then((d) => {
        setCustomers(d.customers || []);
        setEvents(d.recent_events || []);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p className="text-[13px] text-[var(--dim)]">Loading memory…</p>;

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-[18px] font-medium">Customer Memory</h3>
        <p className="mt-1 text-[13px] leading-relaxed text-[var(--muted)]">
          Everything SARA remembers about every caller — facts, intent, sentiment, next actions. She picks
          up exactly where the last conversation left off.
        </p>
      </div>

      {customers.length === 0 ? (
        <p className="rounded-xl border border-dashed border-white/15 px-5 py-8 text-center text-[13px] text-[var(--dim)]">
          No callers yet — SARA builds memory automatically after the first call.
        </p>
      ) : (
        <div className="space-y-3">
          {customers.map((c) => (
            <div key={c.id} className="rounded-2xl border border-white/10 bg-[var(--ink-2)] p-4 sm:p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-[15px] font-medium text-[#e9edef]">{c.name || 'Unknown caller'}</div>
                  <div className="mono mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[var(--dim)]">
                    <span>{c.phone}</span>
                    {c.intent && <span>intent: {c.intent}</span>}
                    {c.sentiment && <span>sentiment: {c.sentiment}</span>}
                    <span>{c.lifecycle_stage}</span>
                    {c.qualification_score != null && <span>score: {c.qualification_score}</span>}
                  </div>
                  {c.next_action && (
                    <div className="mt-2 text-[12.5px] text-[var(--muted)]">Next: {c.next_action}</div>
                  )}
                  {Object.keys(c.memory || {}).length > 0 && (
                    <div className="mono mt-2 flex flex-wrap gap-1.5">
                      {Object.entries(c.memory).slice(0, 8).map(([k, v]) => (
                        <span key={k} className="rounded-full border border-white/10 px-2.5 py-0.5 text-[10.5px] text-[var(--muted)]">
                          {k}: {String(v).slice(0, 40)}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <div className="mono shrink-0 text-right text-[11px] text-[var(--dim)]">
                  <div>{c.call_count} calls · {c.message_count} msgs</div>
                  {c.last_call_at && <div className="mt-1">last: {new Date(c.last_call_at).toLocaleDateString()}</div>}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {events.length > 0 && (
        <div>
          <div className="mono mb-2 text-[11px] uppercase tracking-[0.16em] text-[var(--dim)]">Recent events</div>
          <div className="space-y-1.5">
            {events.slice(0, 12).map((e) => (
              <div key={e.id} className="mono flex items-center justify-between rounded-lg border border-white/5 bg-[var(--ink)] px-3.5 py-2 text-[11.5px] text-[var(--muted)]">
                <span>{e.event_type}</span>
                <span className="text-[var(--dim)]">{e.timestamp ? new Date(e.timestamp).toLocaleString() : '—'}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Control panel with tabs                                                   */
/* -------------------------------------------------------------------------- */

function ControlPanel() {
  const [tab, setTab] = useState<'chat' | 'knowledge' | 'calls' | 'connect' | 'config' | 'campaign' | 'memory'>('chat');
  const [flash, setFlash] = useState<{ ok: boolean; msg: string } | null>(null);
  const say = (ok: boolean, msg: string) => {
    setFlash({ ok, msg });
    setTimeout(() => setFlash(null), 4000);
  };

  /* live calls */
  const [calls, setCalls] = useState<VoiceCall[]>([]);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const loadCalls = () => {
    fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/calls?business_id=biz_demo&limit=25`)
      .then((r) => r.json())
      .then((d) => setCalls(d.calls ?? []))
      .catch(() => {});
    fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/analytics?business_id=biz_demo`)
      .then((r) => r.json())
      .then(setAnalytics)
      .catch(() => {});
  };
  useEffect(() => {
    loadCalls();
    const t = setInterval(loadCalls, 10000);
    return () => clearInterval(t);
  }, []);

  const tabBtn = (t: 'chat' | 'knowledge' | 'calls' | 'connect' | 'config' | 'campaign' | 'memory', label: string) => (
    <button
      key={t}
      onClick={() => setTab(t)}
      className={`rounded-full px-5 py-2.5 text-[13.5px] font-medium transition-colors ${
        tab === t ? 'bg-[var(--violet)] text-white' : 'border border-white/10 text-[var(--muted)] hover:text-[var(--ivory)]'
      }`}
    >
      {label}
    </button>
  );

  /* Deep link: /hub/voice-agent#campaign opens the election campaign tab. */
  useEffect(() => {
    const applyHash = () => {
      const hash = window.location.hash.replace('#', '');
      if (
        hash === 'chat' ||
        hash === 'knowledge' ||
        hash === 'calls' ||
        hash === 'connect' ||
        hash === 'config' ||
        hash === 'campaign' ||
        hash === 'memory'
      ) {
        setTab(hash);
      }
    };
    applyHash();
    window.addEventListener('hashchange', applyHash);
    return () => window.removeEventListener('hashchange', applyHash);
  }, []);

  return (
    <section id="console" className="relative border-t border-white/5 bg-[var(--ink-2)] py-24 sm:py-32">
      <div className="absolute left-1/2 top-0 h-[400px] w-[700px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[radial-gradient(closest-side,rgba(139,92,246,.10),transparent)]" aria-hidden />
      <Container className="relative">
        <SectionHead
          eyebrow="Agent console"
          title={<>Run your voice agent from one console.</>}
          body="Talk to SARA on the live phone, teach her your business, and command the whole operation from the owner chat."
        />

        <div className="mt-10 flex flex-wrap items-center gap-2" data-reveal>
          {tabBtn('chat', '🛠️ Owner chat')}
          {tabBtn('knowledge', '📚 Knowledge')}
          {tabBtn('calls', '📞 Live calls')}
          {tabBtn('connect', '🔌 Connect')}
          {tabBtn('config', '⚙️ Persona & Business')}
          {tabBtn('campaign', '🗳️ Election Campaign')}
          {tabBtn('memory', '🧠 Memory')}
        </div>

        <div className="mt-6 rounded-3xl border border-white/10 bg-[#0a0c10] p-6 sm:p-8" data-reveal data-delay="80">
          {tab === 'chat' && <OwnerChat />}

          {tab === 'knowledge' && <KnowledgePanel say={say} />}

          {tab === 'connect' && <VoiceConnectPanel say={say} />}

          {tab === 'config' && <VoiceConfigPanel say={say} />}

          {tab === 'campaign' && <ElectionCampaignPanel say={say} />}

          {tab === 'memory' && <VoiceMemoryPanel />}

          {tab === 'calls' && (
            <div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                {[
                  ['TOTAL CALLS', analytics?.total_calls],
                  ['COMPLETED', analytics?.completed_calls],
                  ['FAILED', analytics?.failed_calls],
                  ['MESSAGES', analytics?.total_messages],
                ].map(([label, value]) => (
                  <div key={label as string} className="rounded-xl border border-white/10 bg-[var(--ink)] p-4">
                    <div className="mono text-[10px] text-[var(--dim)]">{label as string}</div>
                    <div className="mt-1 text-2xl font-light text-[var(--ivory)]">{(value ?? 0) as number}</div>
                  </div>
                ))}
              </div>
              <div className="mt-6 space-y-2">
                {calls.length === 0 && (
                  <p className="rounded-xl border border-dashed border-white/15 px-5 py-8 text-center text-[13px] text-[var(--dim)]">
                    No calls yet — talk to SARA on the phone above, or point Twilio at the webhook.
                  </p>
                )}
                {calls.map((c) => (
                  <div key={c.id} className="flex items-center justify-between rounded-xl border border-white/10 bg-[var(--ink)] px-4 py-3">
                    <div className="min-w-0">
                      <div className="text-[13.5px] text-[#e9edef]">{c.customer_name ?? 'Unknown caller'}</div>
                      <div className="mono mt-0.5 text-[10.5px] text-[var(--dim)]">
                        {c.phone ?? '—'} · {c.objective ?? 'general'}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-3">
                      <span className="mono text-[10.5px] text-[var(--dim)]">{c.direction}</span>
                      <span
                        className={`mono rounded-full px-2.5 py-0.5 text-[10.5px] ${
                          c.state === 'COMPLETED'
                            ? 'bg-[var(--green)]/15 text-[var(--green)]'
                            : c.state === 'FAILED'
                              ? 'bg-[var(--red)]/15 text-[var(--red)]'
                              : 'bg-[var(--violet)]/15 text-[#c4a7f7]'
                        }`}
                      >
                        {c.state}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {flash && (
          <div
            className={`mt-5 rounded-xl border px-4 py-3 text-[13px] ${
              flash.ok
                ? 'border-[var(--green)]/30 bg-[var(--green)]/10 text-[var(--green)]'
                : 'border-red-500/30 bg-red-500/10 text-red-300'
            }`}
          >
            {flash.msg}
          </div>
        )}
      </Container>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */

export default function VoiceAgentPage() {
  const { status, reachable } = useEngineHealth();
  useReveal();

  return (
    <div className="sara-voice relative">
      <style dangerouslySetInnerHTML={{ __html: css }} />
      <Nav />
      <main>
        <Hero status={status} reachable={reachable} />
        <ControlPanel />
      </main>
    </div>
  );
}
