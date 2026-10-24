'use client';

import React, { useEffect, useRef, useState, type ReactNode } from "react";
import { WHATSAPP_API_URL } from "@/lib/api";

/* -------------------------------------------------------------------------- */
/*  Design tokens + global styles (scoped by the .sara root)                   */
/* -------------------------------------------------------------------------- */
const css = `
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

.sara{
  --ink:#0b0d10; --ink-2:#10131a; --ink-3:#161a22; --line:rgba(255,255,255,.08);
  --ivory:#f3efe6; --muted:#9aa1ab; --dim:#6b727d;
  --green:#2fbf71; --green-deep:#1f8f55; --brass:#c9a961;
  --font-sans:'Inter',ui-sans-serif,system-ui,sans-serif;
  --font-serif:'Instrument Serif',Georgia,serif;
  --font-mono:'JetBrains Mono',ui-monospace,SFMono-Regular,monospace;
  --mx:0; --my:0;
  background:var(--ink); color:var(--ivory); font-family:var(--font-sans);
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
  min-height:100vh;
}
.sara .serif{font-family:var(--font-serif);letter-spacing:-0.01em}
.sara .mono{font-family:var(--font-mono)}
.sara ::selection{background:rgba(47,191,113,.28)}

/* film grain, keeps large dark surfaces from looking flat */
.sara .grain::before{
  content:"";position:absolute;inset:0;pointer-events:none;opacity:.05;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' stitchTiles='stitch'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>");
}

/* hairline grid used behind the hero, fades with a radial mask */
.sara .grid-bg{
  background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
  background-size:64px 64px;
  -webkit-mask-image:radial-gradient(ellipse 70% 60% at 50% 30%,#000 30%,transparent 75%);
  mask-image:radial-gradient(ellipse 70% 60% at 50% 30%,#000 30%,transparent 75%);
}

/* 3D scene */
.sara .scene{perspective:1600px;perspective-origin:60% 40%}
.sara .stage{
  transform-style:preserve-3d;
  transform:rotateX(calc(6deg + var(--my) * -4deg)) rotateY(calc(-14deg + var(--mx) * 6deg));
  transition:transform .35s cubic-bezier(.2,.8,.2,1);
}
.sara .layer{transform-style:preserve-3d;will-change:transform}
.sara .float-a{animation:floatA 9s ease-in-out infinite}
.sara .float-b{animation:floatB 11s ease-in-out infinite}
.sara .float-c{animation:floatC 13s ease-in-out infinite}
@keyframes floatA{0%,100%{transform:translate3d(0,0,140px)}50%{transform:translate3d(0,-10px,140px)}}
@keyframes floatB{0%,100%{transform:translate3d(0,0,220px)}50%{transform:translate3d(0,-14px,220px)}}
@keyframes floatC{0%,100%{transform:translate3d(0,0,90px)}50%{transform:translate3d(0,-8px,90px)}}

/* physically plausible shadows: contact + ambient + key light */
.sara .shadow-device{
  box-shadow:0 1px 0 rgba(255,255,255,.06) inset,0 0 0 1px rgba(0,0,0,.6),
  0 2px 4px rgba(0,0,0,.4),0 18px 40px -10px rgba(0,0,0,.7),0 60px 120px -30px rgba(0,0,0,.8);
}
.sara .shadow-card{
  box-shadow:0 1px 0 rgba(255,255,255,.05) inset,0 0 0 1px rgba(0,0,0,.5),
  0 10px 24px -8px rgba(0,0,0,.6),0 30px 60px -20px rgba(0,0,0,.7);
}
.sara .metal{
  background:linear-gradient(160deg,#2a2f38 0%,#151920 45%,#1d222b 100%);
}
.sara .metal-edge{
  background:linear-gradient(180deg,rgba(255,255,255,.14),rgba(255,255,255,0) 40%,rgba(0,0,0,.25));
}

/* typing indicator */
.sara .dot{animation:blink 1.4s infinite both}
.sara .dot:nth-child(2){animation-delay:.2s}.sara .dot:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.25}40%{opacity:1}}

/* pulse on the accepted webhook */
.sara .pulse::after{
  content:"";position:absolute;inset:-4px;border-radius:9999px;border:1px solid var(--green);
  animation:pulse 2.4s ease-out infinite;opacity:0;
}
@keyframes pulse{0%{transform:scale(.6);opacity:.8}100%{transform:scale(1.8);opacity:0}}

/* scroll reveal */
.sara [data-reveal]{opacity:0;transform:translateY(18px);transition:opacity .7s cubic-bezier(.2,.8,.2,1),transform .7s cubic-bezier(.2,.8,.2,1)}
.sara [data-reveal].is-visible{opacity:1;transform:none}

/* marquee */
.sara .marquee{animation:marquee 34s linear infinite}
@keyframes marquee{from{transform:translateX(0)}to{transform:translateX(-50%)}}

/* buttons */
.sara .btn-primary{
  background:linear-gradient(180deg,#3ccf80,#27ad66);color:#06130c;
  box-shadow:0 1px 0 rgba(255,255,255,.35) inset,0 -1px 0 rgba(0,0,0,.25) inset,0 8px 20px -8px rgba(47,191,113,.6);
  transition:transform .2s,box-shadow .2s,filter .2s;
}
.sara .btn-primary:hover{transform:translateY(-1px);filter:brightness(1.04);box-shadow:0 1px 0 rgba(255,255,255,.35) inset,0 -1px 0 rgba(0,0,0,.25) inset,0 14px 28px -10px rgba(47,191,113,.7)}
.sara .btn-primary:active{transform:translateY(0)}
.sara .btn-ghost{border:1px solid var(--line);background:rgba(255,255,255,.02);transition:border-color .2s,background .2s,transform .2s}
.sara .btn-ghost:hover{border-color:rgba(255,255,255,.2);background:rgba(255,255,255,.05);transform:translateY(-1px)}
.sara :focus-visible{outline:2px solid var(--green);outline-offset:3px;border-radius:6px}

/* feature tile hover */
.sara .tile{transition:transform .35s cubic-bezier(.2,.8,.2,1),border-color .35s,background .35s}
.sara .tile:hover{transform:translateY(-3px);border-color:rgba(255,255,255,.16);background:var(--ink-3)}

/* live status chip */
.sara .status-dot{animation:statusPulse 2s ease-in-out infinite}
@keyframes statusPulse{0%,100%{opacity:1}50%{opacity:.45}}

@media (prefers-reduced-motion:reduce){
  .sara *{animation:none!important;transition:none!important}
  .sara [data-reveal]{opacity:1;transform:none}
}
`;

/* -------------------------------------------------------------------------- */
/*  Live backend wiring                                                        */
/* -------------------------------------------------------------------------- */
interface EngineHealth {
  status: string;
  configured: boolean;
  business_context_loaded: boolean;
}

interface DemoReply {
  reply: string;
  intent: string;
  escalate_to_human: boolean;
  state: Record<string, unknown>;
  ui_options: string[];
}

function useEngineHealth() {
  const [health, setHealth] = useState<EngineHealth | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);

  const check = () => {
    fetch(`${WHATSAPP_API_URL}/whatsapp/health`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((h: EngineHealth) => {
        setHealth(h);
        setReachable(true);
      })
      .catch(() => {
        setHealth(null);
        setReachable(false);
      });
  };

  useEffect(() => {
    check();
    const t = setInterval(check, 10000);
    return () => clearInterval(t);
  }, []);

  return { health, reachable, refresh: check };
}

/* -------------------------------------------------------------------------- */
/*  Hooks                                                                      */
/* -------------------------------------------------------------------------- */
function useReveal() {
  useEffect(() => {
    const els = document.querySelectorAll<HTMLElement>(".sara [data-reveal]");
    const io = new IntersectionObserver(
      (entries) =>
        entries.forEach((e) => {
          if (e.isIntersecting) {
            const el = e.target as HTMLElement;
            const delay = Number(el.dataset.delay ?? 0);
            el.style.transitionDelay = `${delay}ms`;
            el.classList.add("is-visible");
            io.unobserve(el);
          }
        }),
      { threshold: 0.15, rootMargin: "0px 0px -8% 0px" }
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);
}

const Container = ({ children, className = "" }: { children: ReactNode; className?: string }) => (
  <div className={`mx-auto w-full max-w-[1200px] px-5 sm:px-8 ${className}`}>{children}</div>
);

const Eyebrow = ({ children }: { children: ReactNode }) => (
  <span className="mono inline-flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-[var(--green)]">
    <span className="h-1 w-1 rounded-full bg-[var(--green)]" aria-hidden />
    {children}
  </span>
);

const SectionHead = ({
  eyebrow,
  title,
  body,
  align = "left",
}: {
  eyebrow: string;
  title: ReactNode;
  body?: string;
  align?: "left" | "center";
}) => (
  <div className={`max-w-2xl ${align === "center" ? "mx-auto text-center" : ""}`} data-reveal>
    <Eyebrow>{eyebrow}</Eyebrow>
    <h2 className="serif mt-4 text-[2.1rem] leading-[1.05] sm:text-[2.75rem]">{title}</h2>
    {body && <p className="mt-4 text-[15.5px] leading-relaxed text-[var(--muted)]">{body}</p>}
  </div>
);

/* -------------------------------------------------------------------------- */
/*  Live WhatsApp-style chat (the "phone" is real: it talks to the backend)    */
/* -------------------------------------------------------------------------- */
type ChatMsg = { role: "guest" | "sara"; content: string; meta?: string };

/* -------------------------------------------------------------------------- */
/*  Voice input — browser SpeechRecognition (best in Chrome/Edge)              */
/* -------------------------------------------------------------------------- */
type SpeechRecogEvent = {
  results: ArrayLike<ArrayLike<{ transcript: string }>>;
};
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
}: {
  onResult: (text: string) => void;
  busy: boolean;
}) {
  const [listening, setListening] = useState(false);
  const recRef = useRef<SpeechRecog | null>(null);

  useEffect(
    () => () => {
      recRef.current?.stop();
    },
    []
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
      alert("Voice input isn't supported in this browser. Use Chrome or Edge to talk to the agent.");
      return;
    }
    const rec = new Ctor();
    rec.lang = "en-US";
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
      if (e.error && e.error !== "aborted" && e.error !== "no-speech") {
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

  return (
    <button
      type="button"
      onClick={toggle}
      disabled={busy}
      aria-label={listening ? "Stop listening" : "Voice input"}
      title={listening ? "Listening… click to stop" : "Speak instead of typing"}
      className={`grid h-8 w-8 shrink-0 place-items-center rounded-full border transition-colors ${
        listening
          ? "animate-pulse border-red-400/60 bg-red-500/20 text-red-300"
          : "border-white/10 bg-[#1f2c34] text-[#8696a0] hover:border-white/25 hover:text-[#e9edef]"
      }`}
    >
      {listening ? (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
          <rect x="6" y="6" width="12" height="12" rx="2" />
        </svg>
      ) : (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
          <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
          <line x1="12" x2="12" y1="19" y2="22" />
        </svg>
      )}
    </button>
  );
}

function LivePhoneChat() {
  const [messages, setMessages] = useState<ChatMsg[]>([
    {
      role: "sara",
      content:
        "Hi! I'm *SARA*, the Grand Stay Hotel concierge. I can check rooms, prices and availability, or start a booking. What can I help you with?\n\n1. Check availability\n2. View prices\n3. Book a room",
      meta: "live · /whatsapp/api/demo",
    },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  const send = async (raw?: string) => {
    const text = (raw ?? input).trim();
    if (!text || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "guest", content: text }]);
    setBusy(true);
    try {
      const res = await fetch(`${WHATSAPP_API_URL}/whatsapp/api/demo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: DemoReply = await res.json();
      setMessages((m) => [
        ...m,
        {
          role: "sara",
          content: data.reply,
          meta: `intent: ${data.intent}${data.escalate_to_human ? " · ⚠️ escalate_to_human" : ""}`,
        },
      ]);
    } catch {
      setMessages((m) => [
        ...m,
        {
          role: "sara",
          content:
            "⚠️ Cannot reach the agent backend. Is `python run_backend.py` running? (Looking for " +
            `${WHATSAPP_API_URL}/whatsapp/api/demo)`,
        },
      ]);
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    try {
      await fetch(`${WHATSAPP_API_URL}/whatsapp/admin/sessions/web-demo`, { method: "DELETE" });
    } catch {
      /* backend down — still clear locally */
    }
    setMessages([
      {
        role: "sara",
        content: "Session reset. New conversation — how can I help?",
      },
    ]);
  };

  return (
    <div className="shadow-device relative h-[560px] w-[272px] shrink-0 rounded-[40px] bg-[#0a0a0c] p-[9px]">
      <div className="metal-edge pointer-events-none absolute inset-0 rounded-[40px] opacity-70" aria-hidden />
      <div className="relative flex h-full w-full flex-col overflow-hidden rounded-[32px] bg-[#0b141a]">
        <div className="absolute left-1/2 top-2.5 z-10 h-[26px] w-[92px] -translate-x-1/2 rounded-full bg-black" aria-hidden />
        <div className="mono flex items-center justify-between px-6 pt-3 text-[10px] text-[#cfd6da]">
          <span>9:41</span>
          <span className="flex items-center gap-1.5">
            <span className="status-dot h-1.5 w-1.5 rounded-full bg-[#2fbf71]" aria-hidden />
            <span className="tracking-tight">live</span>
          </span>
        </div>
        <div className="mt-2 flex items-center gap-2.5 border-b border-white/5 bg-[#1f2c34] px-3 py-2.5">
          <div className="grid h-8 w-8 place-items-center rounded-full bg-[var(--brass)] text-[13px] font-semibold text-[#1d1608]">GS</div>
          <div className="min-w-0 leading-tight">
            <div className="flex items-center gap-1 text-[13px] font-medium text-[#e9edef]">
              Grand Stay Hotel
              <svg width="12" height="12" viewBox="0 0 24 24" aria-label="verified"><circle cx="12" cy="12" r="11" fill="#25d366" /><path d="M7 12.5l3 3 7-7" stroke="#0b141a" strokeWidth="2.4" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
            </div>
            <div className="truncate text-[10.5px] text-[#8696a0]">SARA · replies instantly</div>
          </div>
          <button
            onClick={reset}
            className="mono ml-auto rounded-md border border-white/10 px-1.5 py-1 text-[9px] text-[#8696a0] transition-colors hover:border-white/25 hover:text-[#e9edef]"
            title="Reset demo session"
          >
            reset
          </button>
        </div>
        <div
          ref={scrollRef}
          className="flex-1 space-y-2 overflow-y-auto px-3 pt-3"
          style={{
            backgroundImage: "radial-gradient(rgba(255,255,255,.035) 1px, transparent 1px)",
            backgroundSize: "14px 14px",
          }}
        >
          {messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === "guest" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[86%] rounded-xl px-3 py-2 text-[12.5px] leading-[1.45] shadow-[0_1px_1px_rgba(0,0,0,.35)] whitespace-pre-line ${
                  m.role === "guest"
                    ? "rounded-tr-sm bg-[#005c4b] text-[#e9fdf4]"
                    : "rounded-tl-sm bg-[#1f2c34] text-[#e9edef]"
                }`}
              >
                {m.content}
                {m.meta && (
                  <div className="mono mt-1.5 text-[9px] text-[#8696a0]">{m.meta}</div>
                )}
              </div>
            </div>
          ))}
          {busy && (
            <div className="flex justify-start">
              <div className="flex items-center gap-1 rounded-xl rounded-tl-sm bg-[#1f2c34] px-3 py-2.5" aria-label="SARA is typing">
                <span className="dot h-1.5 w-1.5 rounded-full bg-[#8696a0]" />
                <span className="dot h-1.5 w-1.5 rounded-full bg-[#8696a0]" />
                <span className="dot h-1.5 w-1.5 rounded-full bg-[#8696a0]" />
              </div>
            </div>
          )}
        </div>
        <div className="flex items-center gap-2 bg-[#0b141a] px-3 pb-5 pt-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
            placeholder="Type as a guest…"
            aria-label="Guest message"
            className="flex-1 rounded-full bg-[#1f2c34] px-3 py-2 text-[12px] text-[#e9edef] placeholder-[#8696a0] focus:outline-none"
          />
          <MicButton onResult={(t) => send(t)} busy={busy} />
          <button
            onClick={() => send()}
            disabled={busy}
            aria-label="Send"
            className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-[#00a884] transition-opacity disabled:opacity-50"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="#06130c"><path d="M3 11l18-8-8 18-2-8-8-2z" /></svg>
          </button>
        </div>
        <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(115deg,rgba(255,255,255,.09),rgba(255,255,255,0)_38%)]" aria-hidden />
      </div>
    </div>
  );
}

/* Colored span helpers for the JSON mockup — plain functions, not components,
   so React never has to create components during render. */
function Nav() {
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  const links: [string, string][] = [["Console", "#console"]];
  return (
    <header
      className={`sticky top-0 z-30 transition-[background,border-color,backdrop-filter] duration-300 ${
        scrolled ? "border-b border-white/5 bg-[#0b0d10]/80 backdrop-blur-md" : "border-b border-transparent"
      }`}
    >
      <Container className="flex h-16 items-center justify-between">
        {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- design anchor */}
        <a href="/hub" className="flex items-center gap-2.5" aria-label="Back to hub">
          <span className="grid h-7 w-7 place-items-center rounded-md bg-[var(--ivory)] text-[13px] font-semibold text-[var(--ink)]">S</span>
          <span className="text-[15px] font-medium tracking-tight">SARA</span>
          <span className="hidden text-[13px] text-[var(--dim)] sm:inline">WhatsApp Agent · Grand Stay Hotel</span>
        </a>
        <nav className="hidden items-center gap-7 md:flex" aria-label="Primary">
          {links.map(([l, h]) => (
            <a key={h} href={h} className="text-[13.5px] text-[var(--muted)] transition-colors hover:text-[var(--ivory)]">
              {l}
            </a>
          ))}
        </nav>
        <div className="hidden items-center gap-3 md:flex">
          <a href="#console" className="btn-primary rounded-full px-4 py-2 text-[13.5px] font-semibold">
            Open console
          </a>
        </div>
        <button
          className="grid h-9 w-9 place-items-center rounded-md border border-white/10 md:hidden"
          aria-expanded={open}
          aria-label="Toggle menu"
          onClick={() => setOpen((v) => !v)}
        >
          <span className="relative block h-[10px] w-4">
            <span className={`absolute left-0 top-0 h-[1.5px] w-4 bg-current transition-transform ${open ? "translate-y-[4.5px] rotate-45" : ""}`} />
            <span className={`absolute bottom-0 left-0 h-[1.5px] w-4 bg-current transition-transform ${open ? "-translate-y-[4.5px] -rotate-45" : ""}`} />
          </span>
        </button>
      </Container>
      {open && (
        <div className="border-t border-white/5 bg-[#0b0d10] md:hidden">
          <Container className="flex flex-col gap-1 py-3">
            {links.map(([l, h]) => (
              <a key={h} href={h} onClick={() => setOpen(false)} className="rounded-md px-2 py-2.5 text-[15px] text-[var(--muted)] hover:bg-white/5 hover:text-[var(--ivory)]">
                {l}
              </a>
            ))}
            <a href="#console" onClick={() => setOpen(false)} className="btn-primary mt-2 rounded-full px-4 py-2.5 text-center text-[14px] font-semibold">
              Open console
            </a>
          </Container>
        </div>
      )}
    </header>
  );
}

function AgentRunChip() {
  /* RUN / PAUSE the WhatsApp agent — state lives on the backend and survives restarts. */
  const [running, setRunning] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const refresh = () =>
    fetch(`${WHATSAPP_API_URL}/whatsapp/messenger`)
      .then((r) => r.json())
      .then((d: { running?: boolean }) => setRunning(Boolean(d.running)))
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
        `${WHATSAPP_API_URL}/whatsapp/${running ? "pause" : "run"}`,
        { method: "POST" }
      );
      if (res.ok) {
        const d = (await res.json()) as { running?: boolean };
        setRunning(Boolean(d.running));
      }
    } catch {
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
          ? "Agent is live — click to pause (guests get no replies until you run it again)"
          : "Agent is paused — click to run (guests are answered automatically)"
      }
      className={`mono inline-flex cursor-pointer items-center gap-2 rounded-full border px-3 py-1.5 text-[11px] transition-colors ${
        running === false
          ? "border-white/15 bg-[var(--ink-3)] text-[var(--dim)]"
          : "border-[var(--green)]/40 bg-[var(--green)]/10 text-[var(--green)]"
      } ${busy ? "opacity-60" : ""}`}
    >
      <span
        className={`h-2 w-2 rounded-full ${
          running === null ? "bg-[#c9a961]" : running ? "bg-[#2fbf71]" : "bg-[#6b727d]"
        }`}
        aria-hidden
      />
      AGENT {running === null ? "…" : running ? "RUNNING" : "PAUSED"}
      <span aria-hidden>{running === null ? "" : running ? "⏸" : "▶"}</span>
    </button>
  );
}

function Hero({ health, reachable }: { health: EngineHealth | null; reachable: boolean | null }) {
  return (
    <section className="grain relative overflow-hidden pt-10 sm:pt-14">
      <div className="grid-bg absolute inset-0" aria-hidden />
      <div className="absolute left-1/2 top-0 h-[600px] w-[900px] -translate-x-1/2 -translate-y-1/3 rounded-full bg-[radial-gradient(closest-side,rgba(201,169,97,.10),transparent)]" aria-hidden />
      <Container className="relative grid items-center gap-12 lg:grid-cols-[1.05fr_1fr] lg:gap-6">
        <div className="max-w-[560px]">
          <div data-reveal>
            <Eyebrow>WhatsApp concierge · Unipile · strict JSON</Eyebrow>
          </div>
          <h1 className="serif mt-5 text-[2.9rem] leading-[1.02] sm:text-[3.9rem] lg:text-[4.4rem]" data-reveal data-delay="60">
            The front desk that answers on WhatsApp <em className="text-[var(--brass)]">before the guest looks up.</em>
          </h1>
          <p className="mt-6 max-w-[500px] text-[16px] leading-relaxed text-[var(--muted)]" data-reveal data-delay="120">
            One WhatsApp agent, two seats: guests book through the phone on the right, and you run the
            whole thing from the console below — owner chat, knowledge base and Google Sheets.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3" data-reveal data-delay="180">
            <a href="#console" className="btn-primary rounded-full px-5 py-3 text-[14.5px] font-semibold">
              Open the console
            </a>
          </div>

          {/* Live engine status — real data from /whatsapp/health */}
          <div className="mt-8 flex flex-wrap items-center gap-3" data-reveal data-delay="220">
            <span className="mono inline-flex items-center gap-2 rounded-full border border-white/10 bg-[var(--ink-2)] px-3 py-1.5 text-[11px]">
              <span
                className={`status-dot h-2 w-2 rounded-full ${
                  reachable === null ? "bg-[#c9a961]" : reachable && health?.status === "ok" ? "bg-[#2fbf71]" : "bg-[#e05555]"
                }`}
                aria-hidden
              />
              ENGINE {reachable === null ? "CHECKING…" : reachable && health?.status === "ok" ? "ONLINE" : "UNREACHABLE"}
            </span>
            <span className="mono inline-flex items-center gap-2 rounded-full border border-white/10 bg-[var(--ink-2)] px-3 py-1.5 text-[11px]">
              <span className={`h-2 w-2 rounded-full ${health?.business_context_loaded ? "bg-[#2fbf71]" : "bg-[#e05555]"}`} aria-hidden />
              BUSINESS DATA {health?.business_context_loaded ? "LOADED" : "MISSING"}
            </span>
            <span className="mono inline-flex items-center gap-2 rounded-full border border-white/10 bg-[var(--ink-2)] px-3 py-1.5 text-[11px]">
              <span className={`h-2 w-2 rounded-full ${health?.configured ? "bg-[#2fbf71]" : "bg-[#e05555]"}`} aria-hidden />
              {health?.configured ? "CONFIGURED" : "ENV KEYS PENDING"}
            </span>
            <AgentRunChip />
          </div>
        </div>
        <div className="flex justify-center" data-reveal data-delay="150">
          <LivePhoneChat />
        </div>
      </Container>
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-40 bg-gradient-to-b from-transparent to-[var(--ink)]" aria-hidden />
    </section>
  );
}

function OwnerChat() {
  const [messages, setMessages] = useState<{ role: "you" | "agent"; content: string }[]>([
    {
      role: "agent",
      content:
        "Owner console ready. Ask me anything about your WhatsApp agent — who has messaged, what I know about guests, what's loaded in the knowledge base, or how the agent is set up.",
    },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  const send = async (raw?: string) => {
    const text = (raw ?? input).trim();
    if (!text || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "you", content: text }]);
    setBusy(true);
    try {
      const res = await fetch(`${WHATSAPP_API_URL}/whatsapp/api/owner`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: DemoReply = await res.json();
      setMessages((m) => [...m, { role: "agent", content: data.reply }]);
    } catch {
      setMessages((m) => [
        ...m,
        {
          role: "agent",
          content:
            "⚠️ Cannot reach the agent backend. Is the unified backend running? (Looking for " +
            `${WHATSAPP_API_URL}/whatsapp/api/owner)`,
        },
      ]);
    } finally {
      setBusy(false);
    }
  };

  const suggestions = [
    "Who has messaged recently?",
    "What do you know about the guests?",
    "What knowledge and business data is loaded?",
    "How do I take this live on WhatsApp?",
  ];

  return (
    <div className="mx-auto max-w-3xl">
      <div className="overflow-hidden rounded-2xl border border-white/10 bg-[#0b0d10]">
        <div className="flex items-center gap-2.5 border-b border-white/5 bg-[var(--ink-2)] px-5 py-3.5">
          <span className="grid h-7 w-7 place-items-center rounded-md bg-[var(--brass)]/15 text-[13px]">🛠️</span>
          <div className="leading-tight">
            <div className="text-[13.5px] font-medium">Owner console</div>
            <div className="mono text-[10.5px] text-[var(--dim)]">POST /whatsapp/api/owner · business-owner mode</div>
          </div>
        </div>
        <div ref={scrollRef} className="h-[380px] space-y-3 overflow-y-auto px-5 py-4">
          {messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === "you" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[85%] whitespace-pre-line rounded-2xl px-4 py-2.5 text-[13.5px] leading-relaxed ${
                  m.role === "you"
                    ? "rounded-br-sm bg-[var(--green)] text-[#06130c]"
                    : "rounded-bl-sm border border-white/10 bg-[var(--ink-2)] text-[#e9edef]"
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
            onKeyDown={(e) => e.key === "Enter" && send()}
            placeholder="Ask as the business owner…"
            aria-label="Owner message"
            className="flex-1 rounded-full border border-white/10 bg-[#0a0c10] px-4 py-2.5 text-[13px] text-[#e9edef] placeholder-[#6b727d] focus:border-[var(--green)]/50 focus:outline-none"
          />
          <MicButton onResult={(t) => send(t)} busy={busy} />
          <button
            onClick={() => send()}
            disabled={busy}
            aria-label="Send"
            className="btn-primary grid h-9 w-9 shrink-0 place-items-center rounded-full text-[#06130c] transition-opacity disabled:opacity-50"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M3 11l18-8-8 18-2-8-8-2z" /></svg>
          </button>
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  WhatsApp Numbers / Channels — multi-number command center                  */
/* -------------------------------------------------------------------------- */

interface WaChannel {
  id: string;
  name: string;
  provider: string;
  phone_number: string;
  status: string;
  enabled: boolean;
  is_primary: boolean;
  last_error: string;
  last_checked_at: string | null;
  webhook_url: string;
  credentials_set: string[];
}

interface WaProviderInfo {
  id: string;
  label: string;
  fields: string[];
  help: string;
}

const PROVIDER_ICON: Record<string, string> = {
  meta: "🟦",
  zernio: "🟨",
  unipile: "🟪",
  twilio: "🟥",
  generic: "🔗",
};

const FIELD_LABEL: Record<string, string> = {
  access_token: "Access token",
  phone_number_id: "Phone number ID",
  app_id: "App ID",
  app_secret: "App secret",
  business_account_id: "Business account ID (WABA)",
  verify_token: "Verify token (your choice — used by Meta webhook)",
  api_key: "API key",
  account_id: "Account ID",
  base_url: "Base URL",
  webhook_secret: "Webhook secret (signature)",
  dsn: "DSN URL (e.g. https://api64.unipile.com:19437)",
  account_sid: "Account SID",
  auth_token: "Auth token",
  phone_number: "WhatsApp number (E.164, e.g. +15551234567)",
  send_url: "Outbound send URL (optional — POST {to,text})",
  send_bearer_token: "Bearer token for send_url (optional)",
  secret_header: "Secret header name (default x-webhook-secret)",
};

const STATUS_DOT: Record<string, string> = {
  connected: "bg-[#2fbf71]",
  connecting: "bg-[#c9a961]",
  error: "bg-[#e05555]",
  disconnected: "bg-[#6b727d]",
};

function ChannelsPanel({ say }: { say: (ok: boolean, msg: string) => void }) {
  const [channels, setChannels] = useState<WaChannel[]>([]);
  const [providers, setProviders] = useState<WaProviderInfo[]>([]);
  const [loading, setLoading] = useState(true);

  /* wizard state */
  const [showWizard, setShowWizard] = useState(false);
  const [wProvider, setWProvider] = useState("meta");
  const [wName, setWName] = useState("");
  const [wPhone, setWPhone] = useState("");
  const [wPrimary, setWPrimary] = useState(false);
  const [wCreds, setWCreds] = useState<Record<string, string>>({});
  const [wBusy, setWBusy] = useState(false);

  /* inline rename */
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameVal, setRenameVal] = useState("");

  const [testing, setTesting] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const load = () => {
    Promise.all([
      fetch(`${WHATSAPP_API_URL}/whatsapp/channels`).then((r) => r.json()),
      fetch(`${WHATSAPP_API_URL}/whatsapp/channels/providers`).then((r) => r.json()),
    ])
      .then(([c, p]) => {
        setChannels(c.channels || []);
        setProviders(p.providers || []);
      })
      .catch(() => say(false, "Cannot reach the backend for channels."))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const api = async (path: string, init?: RequestInit) => {
    const res = await fetch(`${WHATSAPP_API_URL}${path}`, init);
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((d as { detail?: string }).detail || `HTTP ${res.status}`);
    return d;
  };

  const create = async () => {
    setWBusy(true);
    try {
      const d = (await api("/whatsapp/channels", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: wName || providers.find((p) => p.id === wProvider)?.label || wProvider,
          provider: wProvider,
          phone_number: wPhone,
          primary: wPrimary,
          credentials: wCreds,
        }),
      })) as { channel: WaChannel };
      say(true, `“${d.channel.name}” added. Test the connection to validate it.`);
      setShowWizard(false);
      setWName("");
      setWPhone("");
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
      const d = (await api(`/whatsapp/channels/${id}/test`, { method: "POST" })) as {
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
      await api(`/whatsapp/channels/${id}/primary`, { method: "POST" });
      say(true, "Primary command center switched — incoming messages now route here instantly.");
      load();
    } catch (e) {
      say(false, (e as Error).message);
    }
  };

  const toggleEnabled = async (ch: WaChannel) => {
    try {
      await api(`/whatsapp/channels/${ch.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
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
      await api(`/whatsapp/channels/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: renameVal.trim() }),
      });
      setRenaming(null);
      load();
    } catch (e) {
      say(false, (e as Error).message);
    }
  };

  const remove = async (ch: WaChannel) => {
    if (!window.confirm(`Delete “${ch.name}”? This permanently removes the connection (credentials included).`)) return;
    try {
      await api(`/whatsapp/channels/${ch.id}`, { method: "DELETE" });
      say(true, `“${ch.name}” deleted.`);
      load();
    } catch (e) {
      say(false, (e as Error).message);
    }
  };

  const copyWebhook = (ch: WaChannel) => {
    navigator.clipboard?.writeText(ch.webhook_url).catch(() => {});
    setCopied(ch.id);
    setTimeout(() => setCopied(null), 1600);
  };

  const wizard = providers.find((p) => p.id === wProvider);
  const inputCls =
    "w-full rounded-xl border border-white/10 bg-[var(--ink)] px-3.5 py-2.5 text-[13.5px] text-[var(--ivory)] outline-none transition-colors placeholder:text-[var(--dim)] focus:border-[var(--green)]";

  return (
    <div className="space-y-6">
      {/* header row */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-[18px] font-medium">WhatsApp Numbers / Channels</h3>
          <p className="mt-1 text-[13px] leading-relaxed text-[var(--muted)]">
            Connect any number — Meta Cloud API, Zernio, Unipile, Twilio or a generic webhook. The{" "}
            <span className="text-[var(--brass)]">Primary Command Center</span> answers every message
            through Sara and sends replies from that exact number.
          </p>
        </div>
        <button onClick={() => setShowWizard((v) => !v)} className="btn-primary rounded-full px-5 py-2.5 text-[13.5px] font-semibold">
          {showWizard ? "✕ Close wizard" : "＋ Add WhatsApp number"}
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
                    {PROVIDER_ICON[p.id]} {p.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-[12.5px]">
              <span className="mb-1.5 block text-[var(--muted)]">Display name</span>
              <input value={wName} onChange={(e) => setWName(e.target.value)} placeholder="e.g. Reception WhatsApp" className={inputCls} />
            </label>
            {(wProvider === "twilio" || wProvider === "meta") && (
              <label className="text-[12.5px]">
                <span className="mb-1.5 block text-[var(--muted)]">WhatsApp number</span>
                <input value={wPhone} onChange={(e) => setWPhone(e.target.value)} placeholder="+15551234567" className={inputCls} />
              </label>
            )}
          </div>
          {wizard && (
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              {wizard.fields.map((f) => (
                <label key={f} className="text-[12.5px]">
                  <span className="mb-1.5 block text-[var(--muted)]">{FIELD_LABEL[f] || f}</span>
                  <input
                    type={f.includes("secret") || f.includes("token") ? "password" : "text"}
                    value={wCreds[f] || ""}
                    onChange={(e) => setWCreds((c) => ({ ...c, [f]: e.target.value }))}
                    className={inputCls}
                    autoComplete="off"
                  />
                </label>
              ))}
            </div>
          )}
          {wizard && wizard.help && (
            <p className="mt-4 whitespace-pre-line rounded-xl border border-white/5 bg-[var(--ink)] px-4 py-3 text-[12.5px] leading-relaxed text-[var(--muted)]">
              {wizard.help}
            </p>
          )}
          <label className="mt-4 flex items-center gap-2 text-[13px]">
            <input type="checkbox" checked={wPrimary} onChange={(e) => setWPrimary(e.target.checked)} className="accent-[var(--green)]" />
            Make this the Primary Command Center now
          </label>
          <div className="mt-5 flex gap-3">
            <button onClick={create} disabled={wBusy} className="btn-primary rounded-full px-5 py-2.5 text-[13.5px] font-semibold disabled:opacity-50">
              {wBusy ? "Adding…" : "Add channel"}
            </button>
            <button onClick={() => setShowWizard(false)} className="btn-ghost rounded-full px-5 py-2.5 text-[13.5px]">
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* channel list */}
      {loading ? (
        <div className="mono rounded-2xl border border-white/10 bg-[var(--ink-2)] px-5 py-8 text-center text-[12.5px] text-[var(--dim)]">
          LOADING CHANNELS…
        </div>
      ) : channels.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-white/15 bg-[var(--ink-2)] px-5 py-10 text-center">
          <div className="text-[26px]">📱</div>
          <div className="mt-2 text-[14px]">No WhatsApp numbers connected yet.</div>
          <p className="mt-1 text-[12.5px] text-[var(--muted)]">
            Click “＋ Add WhatsApp number” and follow the wizard — Sara will answer on the primary number
            the moment it is connected.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {channels.map((ch) => (
            <div
              key={ch.id}
              className={`rounded-2xl border p-4 sm:p-5 ${ch.is_primary ? "border-[var(--green)]/40 bg-[var(--green)]/[0.05]" : "border-white/10 bg-[var(--ink-2)]"}`}
            >
              <div className="flex flex-wrap items-center gap-3">
                <span className="grid h-9 w-9 place-items-center rounded-lg border border-white/10 bg-[var(--ink)] text-[16px]">
                  {PROVIDER_ICON[ch.provider] || "📱"}
                </span>
                <div className="min-w-0 flex-1">
                  {renaming === ch.id ? (
                    <div className="flex items-center gap-2">
                      <input
                        value={renameVal}
                        onChange={(e) => setRenameVal(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && doRename(ch.id)}
                        className={`${inputCls} max-w-[240px] py-1.5`}
                        autoFocus
                      />
                      <button onClick={() => doRename(ch.id)} className="btn-primary rounded-lg px-3 py-1.5 text-[12px]">Save</button>
                      <button onClick={() => setRenaming(null)} className="btn-ghost rounded-lg px-3 py-1.5 text-[12px]">✕</button>
                    </div>
                  ) : (
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[15px] font-medium">{ch.name}</span>
                      {ch.is_primary && (
                        <span className="mono rounded-full bg-[var(--green)] px-2.5 py-0.5 text-[10.5px] font-bold text-[#06130c]">
                          PRIMARY COMMAND CENTER
                        </span>
                      )}
                      {!ch.enabled && (
                        <span className="mono rounded-full border border-white/15 px-2.5 py-0.5 text-[10.5px] text-[var(--dim)]">DISCONNECTED</span>
                      )}
                    </div>
                  )}
                  <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-[var(--muted)]">
                    <span className="mono">{providers.find((p) => p.id === ch.provider)?.label || ch.provider}</span>
                    {ch.phone_number && <span className="mono">{ch.phone_number}</span>}
                    <span className="flex items-center gap-1.5">
                      <span className={`h-2 w-2 rounded-full ${STATUS_DOT[ch.status] || STATUS_DOT.disconnected}`} aria-hidden />
                      {ch.status.toUpperCase()}
                    </span>
                    {ch.last_error && <span className="text-[var(--e05555,#e05555)]">⚠ {ch.last_error.slice(0, 90)}</span>}
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {!ch.is_primary && (
                    <button
                      onClick={() => setPrimary(ch.id)}
                      disabled={!ch.enabled}
                      className="btn-primary rounded-full px-4 py-2 text-[12.5px] font-semibold disabled:opacity-40"
                    >
                      ⭐ Make primary
                    </button>
                  )}
                  <button onClick={() => testChannel(ch.id)} disabled={testing === ch.id} className="btn-ghost rounded-full px-4 py-2 text-[12.5px]">
                    {testing === ch.id ? "Testing…" : "↻ Test"}
                  </button>
                  <button onClick={() => toggleEnabled(ch)} className="btn-ghost rounded-full px-4 py-2 text-[12.5px]">
                    {ch.enabled ? "⏸ Disconnect" : "▶ Connect"}
                  </button>
                  <button
                    onClick={() => {
                      setRenaming(ch.id);
                      setRenameVal(ch.name);
                    }}
                    className="btn-ghost rounded-full px-4 py-2 text-[12.5px]"
                  >
                    ✏️ Rename
                  </button>
                  <button onClick={() => remove(ch)} className="rounded-full border border-[#e05555]/30 px-4 py-2 text-[12.5px] text-[#e05555] transition-colors hover:bg-[#e05555]/10">
                    🗑 Delete
                  </button>
                </div>
              </div>

              {/* webhook row */}
              <div className="mt-3 flex flex-wrap items-center gap-2 rounded-xl border border-white/5 bg-[var(--ink)] px-3 py-2.5">
                <span className="mono text-[10.5px] uppercase tracking-[0.14em] text-[var(--dim)]">Webhook</span>
                <code className="mono min-w-0 flex-1 truncate text-[11.5px] text-[var(--brass)]">{ch.webhook_url}</code>
                <button onClick={() => copyWebhook(ch)} className="btn-ghost rounded-lg px-3 py-1 text-[11.5px]">
                  {copied === ch.id ? "✓ Copied" : "Copy"}
                </button>
              </div>
              {ch.is_primary && ch.provider === "meta" && (
                <p className="mt-2 text-[12px] leading-relaxed text-[var(--muted)]">
                  🔔 Meta only delivers via webhook — put the URL above into Meta Developer App → WhatsApp →
                  Configuration (verify token = the one you entered), subscribe to <code className="mono">messages</code>, and it starts instantly.
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      <p className="mono text-[11px] leading-relaxed text-[var(--dim)]">
        Credentials are encrypted at rest (Fernet). Only the primary number routes to Sara — switching
        primary is instant, no restart needed.
      </p>
    </div>
  );
}

function ControlPanel() {
  const [tab, setTab] = useState<"chat" | "knowledge" | "sheets" | "channels">("chat");
  const [flash, setFlash] = useState<{ ok: boolean; msg: string } | null>(null);
  const say = (ok: boolean, msg: string) => {
    setFlash({ ok, msg });
    setTimeout(() => setFlash(null), 4000);
  };

  /* ---- knowledge state ---- */
  interface KnowledgeDoc {
    id: string;
    filename: string;
    ext: string;
    size: number;
    status: string;
  }
  interface SearchHit {
    text: string;
    score: number;
    source_ref: string;
  }
  interface SheetsStatus {
    connected: boolean;
    label?: string;
    default_spreadsheet_id?: string | null;
  }
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]);
  const [searchQ, setSearchQ] = useState("");
  const [searchResults, setSearchResults] = useState<SearchHit[] | null>(null);

  const loadDocs = () => {
    fetch(`${WHATSAPP_API_URL}/whatsapp/knowledge/documents`)
      .then((r) => r.json())
      .then((d) => setDocs(d.documents || []))
      .catch(() => {});
  };

  const uploadFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async (ev) => {
      const base64 = (ev.target?.result as string).split(",")[1];
      try {
        const res = await fetch(`${WHATSAPP_API_URL}/whatsapp/knowledge/upload`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: file.name, mime: file.type, content_base64: base64 }),
        });
        const d = await res.json();
        if (res.ok) {
          say(true, `Added “${d.filename}” — ${d.chunks} chunks, ${d.facts} facts.`);
          loadDocs();
        } else say(false, d.detail || "Upload failed");
      } catch {
        say(false, "Cannot reach the backend.");
      }
    };
    reader.readAsDataURL(file);
    e.target.value = "";
  };

  const deleteDoc = async (docId: string) => {
    await fetch(`${WHATSAPP_API_URL}/whatsapp/knowledge/documents/${docId}`, { method: "DELETE" });
    loadDocs();
  };

  const searchKnowledge = async () => {
    if (!searchQ.trim()) return;
    const res = await fetch(`${WHATSAPP_API_URL}/whatsapp/knowledge/search?q=${encodeURIComponent(searchQ)}`);
    const d = await res.json();
    setSearchResults(d.results || []);
  };

  /* ---- sheets state ---- */
  const [sheetsStatus, setSheetsStatus] = useState<SheetsStatus | null>(null);
  const [syncing, setSyncing] = useState(false);

  const checkSheets = () => {
    fetch(`${WHATSAPP_API_URL}/whatsapp/integrations/sheets/status`)
      .then((r) => r.json())
      .then(setSheetsStatus)
      .catch(() => {});
  };

  const connectSheets = async () => {
    try {
      const res = await fetch(`${WHATSAPP_API_URL}/whatsapp/integrations/sheets/connect`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: "My Google Workspace" }),
      });
      const d = await res.json();
      if (d.authorize_url) {
        const w = window.open(d.authorize_url, "Google Authorization",
          `width=600,height=700,left=${window.innerWidth / 2 - 300},top=${window.innerHeight / 2 - 350}`);
        const timer = setInterval(() => {
          if (w?.closed) {
            clearInterval(timer);
            checkSheets();
            say(true, "Google account connection verified.");
          }
        }, 1000);
      } else say(false, d.detail || "Connect failed");
    } catch {
      say(false, "Cannot reach the backend.");
    }
  };

  const syncSheets = async () => {
    setSyncing(true);
    try {
      const res = await fetch(`${WHATSAPP_API_URL}/whatsapp/integrations/sheets/sync`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: "whatsapp-data" }),
      });
      const d = await res.json();
      if (res.ok) say(true, `Synced ${d.rows_written} rows → spreadsheet ${d.spreadsheet_id}`);
      else say(false, d.detail || "Sync failed");
    } catch {
      say(false, "Cannot reach the backend.");
    } finally {
      setSyncing(false);
    }
  };

  const loadAll = () => {
    loadDocs();
    checkSheets();
  };

  useEffect(() => {
    loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only load
  }, []);

  const tabBtn = (t: "chat" | "knowledge" | "sheets" | "channels", label: string) => (
    <button
      key={t}
      onClick={() => {
        setTab(t);
        if (t === "knowledge") loadDocs();
        if (t === "sheets") checkSheets();
      }}
      className={`rounded-full px-5 py-2.5 text-[13.5px] font-medium transition-colors ${
        tab === t ? "bg-[var(--green)] text-[#06130c]" : "border border-white/10 text-[var(--muted)] hover:text-[var(--ivory)]"
      }`}
    >
      {label}
    </button>
  );

  const inputCls =
    "w-full rounded-xl border border-white/10 bg-[#0a0c10] px-4 py-3 text-[13.5px] text-[#e9edef] placeholder-[#6b727d] focus:outline-none focus:border-[var(--green)]/50";

  return (
    <section id="console" className="relative border-t border-white/5 bg-[var(--ink-2)] py-24 sm:py-32">
      <div className="absolute left-1/2 top-0 h-[400px] w-[700px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[radial-gradient(closest-side,rgba(47,191,113,.10),transparent)]" aria-hidden />
      <Container className="relative">
        <SectionHead
          eyebrow="Agent console"
          title={<>Run your agent from one console.</>}
          body="Chat as the business owner, upload what the agent should know, and connect Google Sheets for exports. Guests talk to the live phone above."
        />

        <div className="mt-10 flex flex-wrap items-center gap-2" data-reveal>
          {tabBtn("chat", "🛠️ Owner chat")}
          {tabBtn("knowledge", "📚 Knowledge base")}
          {tabBtn("sheets", "📊 Google Sheets")}
          {tabBtn("channels", "📞 Numbers / Channels")}
        </div>

        <div className="mt-6 rounded-3xl border border-white/10 bg-[#0a0c10] p-6 sm:p-8" data-reveal data-delay="80">
          {tab === "chat" && (
            <div className="grid items-center gap-10 lg:grid-cols-[0.9fr_1.1fr]">
              <div>
                <h3 className="text-[18px] font-medium">Owner chat — you talk, SARA follows</h3>
                <p className="mt-2 text-[14px] leading-relaxed text-[var(--muted)]">
                  This is the business-owner seat: it never treats you as a customer and answers from the
                  live overview of guest chats, facts and your knowledge base (same memory store as guests).
                </p>
                <ul className="mt-6 space-y-3">
                  {[
                    ["Who has messaged recently?", "summarises stored guest chats"],
                    ["What do you know about Maya?", "answers from extracted guest facts"],
                    ["What is loaded?", "reports business data + knowledge documents"],
                  ].map(([q, a]) => (
                    <li key={q} className="flex items-baseline gap-3 text-[13.5px]">
                      <code className="mono shrink-0 rounded-md border border-white/10 bg-[var(--ink)] px-2 py-1 text-[11px] text-[var(--brass)]">“{q}”</code>
                      <span className="text-[var(--muted)]">→ {a}</span>
                    </li>
                  ))}
                </ul>
              </div>
              <div>
                <OwnerChat />
              </div>
            </div>
          )}

          {tab === "knowledge" && (
            <div className="grid gap-8 lg:grid-cols-2">
              <div>
                <h3 className="text-[16px] font-medium">Add knowledge</h3>
                <p className="mt-1.5 text-[13px] leading-relaxed text-[var(--muted)]">
                  Upload CSV, TXT or JSON — prices, menus, policies. The agent searches it on every message.
                </p>
                <label className="btn-primary mt-4 inline-flex cursor-pointer rounded-full px-5 py-2.5 text-[13.5px] font-semibold">
                  ⬆ Upload file
                  <input type="file" accept=".csv,.txt,.json" onChange={uploadFile} className="hidden" />
                </label>
                <div className="mt-8">
                  <h4 className="mono text-[11px] uppercase tracking-[0.16em] text-[var(--dim)]">Try it</h4>
                  <div className="mt-3 flex gap-2">
                    <input
                      value={searchQ}
                      onChange={(e) => setSearchQ(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && searchKnowledge()}
                      placeholder="Search the knowledge base…"
                      className={inputCls}
                    />
                    <button onClick={searchKnowledge} className="btn-ghost shrink-0 rounded-xl px-4 text-[13px]">Search</button>
                  </div>
                  {searchResults && (
                    <div className="mt-4 space-y-2">
                      {searchResults.length === 0 && <p className="text-[13px] text-[var(--dim)]">No matches.</p>}
                      {searchResults.map((r, i) => (
                        <div key={i} className="rounded-xl border border-white/10 bg-[var(--ink)] p-3">
                          <div className="mono text-[10px] text-[var(--dim)]">{r.source_ref}</div>
                          <div className="mt-1 text-[13px] text-[#d7dbe0]">{r.text}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
              <div>
                <h3 className="text-[16px] font-medium">Stored documents</h3>
                <div className="mt-4 space-y-2">
                  {docs.length === 0 && <p className="text-[13px] text-[var(--dim)]">Nothing uploaded yet.</p>}
                  {docs.map((d) => (
                    <div key={d.id} className="flex items-center justify-between rounded-xl border border-white/10 bg-[var(--ink)] px-4 py-3">
                      <div className="min-w-0">
                        <div className="truncate text-[13.5px] text-[#e9edef]">{d.filename}</div>
                        <div className="mono mt-0.5 text-[10.5px] text-[var(--dim)]">{d.ext} · {d.size} bytes · {d.status}</div>
                      </div>
                      <button
                        onClick={() => deleteDoc(d.id)}
                        className="shrink-0 rounded-lg border border-white/10 px-3 py-1.5 text-[12px] text-[var(--muted)] transition-colors hover:border-red-400/40 hover:text-red-300"
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {tab === "channels" && <ChannelsPanel say={say} />}

          {tab === "sheets" && (
            <div className="max-w-xl">
              <h3 className="text-[16px] font-medium">Google Sheets export</h3>
              <p className="mt-1.5 text-[13px] leading-relaxed text-[var(--muted)]">
                Connect your Google account once — SARA can then export knowledge and guest conversations
                to a spreadsheet (auto-created on first sync).
              </p>
              <div className="mt-6 flex flex-wrap items-center gap-3">
                {!sheetsStatus?.connected ? (
                  <button onClick={connectSheets} className="btn-primary rounded-full px-5 py-2.5 text-[13.5px] font-semibold">
                    🔗 Connect Google account
                  </button>
                ) : (
                  <>
                    <span className="rounded-full border border-[var(--green)]/40 bg-[var(--green)]/10 px-4 py-2 text-[13px] text-[var(--green)]">
                      ✓ Connected{sheetsStatus.label ? ` · ${sheetsStatus.label}` : ""}
                    </span>
                    <button onClick={syncSheets} disabled={syncing} className="btn-ghost rounded-full px-5 py-2.5 text-[13.5px] disabled:opacity-50">
                      {syncing ? "Syncing…" : "⟳ Sync to Sheets"}
                    </button>
                  </>
                )}
              </div>
              <p className="mono mt-4 text-[11px] text-[var(--dim)]">
                {sheetsStatus?.connected
                  ? `Spreadsheet: ${sheetsStatus.default_spreadsheet_id ?? "will be auto-created"}`
                  : "Status: not connected"}
              </p>
            </div>
          )}
        </div>

        {flash && (
          <div
            className={`mt-5 rounded-xl border px-4 py-3 text-[13px] ${
              flash.ok ? "border-[var(--green)]/30 bg-[var(--green)]/10 text-[var(--green)]" : "border-red-500/30 bg-red-500/10 text-red-300"
            }`}
          >
            {flash.msg}
          </div>
        )}
      </Container>
    </section>
  );
}

export default function WhatsAppAgentPage() {
  const { health, reachable } = useEngineHealth();
  useReveal();

  return (
    <div className="sara relative">
      <style dangerouslySetInnerHTML={{ __html: css }} />
      <Nav />
      <main>
        <Hero health={health} reachable={reachable} />
        <ControlPanel />
      </main>
    </div>
  );
}