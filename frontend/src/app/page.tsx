"use client";

import React, { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { SignInButton, UserButton, useAuth, useClerk } from "@clerk/nextjs";
import Head from "next/head";

export default function NeuralScrollingLandingPage() {
  const { userId } = useAuth();
  const { openSignIn } = useClerk();
  const router = useRouter();
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    // Handle reduced motion for video
    const q = window.matchMedia && window.matchMedia('(prefers-reduced-motion:reduce)');
    const v = videoRef.current;
    if (q && v) {
      const sync = () => {
        if (q.matches) {
          v.pause();
        } else {
          const p = v.play();
          if (p) p.catch(() => {});
        }
      };
      sync();
      if (q.addEventListener) {
        q.addEventListener('change', sync);
      } else {
        q.addListener(sync);
      }
    }

    // Entrance sequence completion
    const foot2 = document.getElementById('foot2');
    let timer: NodeJS.Timeout;
    const done = () => {
      clearTimeout(timer);
      if (foot2) foot2.removeEventListener('animationend', done);
      document.documentElement.classList.add('is-entered');
    };
    if (foot2) {
      foot2.addEventListener('animationend', done);
      timer = setTimeout(done, 4000);
    } else {
      timer = setTimeout(done, 100);
    }

    return () => {
      clearTimeout(timer);
      if (foot2) foot2.removeEventListener('animationend', done);
    };
  }, []);

  const agents = [
    { title: "Frontend Developer", desc: "Builds responsive React/Next.js UIs", link: "/hub/frontend-developer" },
    { title: "Backend Architect", desc: "Designs scalable Python APIs", link: "/hub/backend-architect" },
    { title: "SARA Auto", desc: "Universal agentic execution engine", link: "/hub/sara-auto" },
    { title: "SARA Ultron", desc: "Plans, executes, re-plans and remembers", link: "/hub/sara-ultron" },
    { title: "Voice Agent", desc: "Handles conversational audio tasks", link: "/hub/voice-agent" },
    { title: "Social Media", desc: "Curates and publishes content across platforms", link: "/hub/social-media" },
    { title: "Trading Agent", desc: "Algorithmic market analysis and execution", link: "/hub/trading-agent" },
    { title: "Email Automation", desc: "Manages campaigns and intelligent inbox flows", link: "/hub/email-automation" },
    { title: "WhatsApp Agent", desc: "Intelligent persona-based messaging", link: "/hub/whatsapp-agent" },
    { title: "EduLearner AI", desc: "Personalized learning and intelligent tutoring", link: "/hub/edulearner-ai" },
    { title: "AI Web Scraper", desc: "Extracts and structures web data automatically", link: "/hub/ai-web-scraper" },
  ];

  return (
    <>
      <Head>
        <title>SARA OS — World-Class Digital Products</title>
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
      </Head>

      <style dangerouslySetInnerHTML={{ __html: `
        @import url('https://fonts.googleapis.com/css2?family=Sora:wght@100..900&display=swap');

        :root {
          --ink:#ffffff;  --sub:#a2a9b8;  --nav:#fbfdff;  --foot:#f4f8fd;
          --hair:rgba(196,214,232,.72);   --chev:rgba(214,232,250,.90);
          --navdim:#e8ecf0;               --feat:#e2ebf5;
          --veil:6,10,18;
          --blur:26px; --glow:34px; --hairline:1px;
        }

        html { min-height:100%; background:#02060f; margin:0; padding:0; overflow-x: hidden; scroll-behavior: smooth; }
        body {
          font-family: 'Sora', system-ui, -apple-system, 'Segoe UI', sans-serif;
          color: var(--ink);
          -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
          text-rendering: geometricPrecision;
          display: flex; flex-direction: column;
          min-height: 100vh; margin: 0; padding: 0; background: transparent;
        }
        a { color: inherit; text-decoration: none; cursor: pointer; pointer-events: auto; }
        li { list-style: none; }
        a:focus-visible, button:focus-visible { outline: 2px solid #9fe0ff; outline-offset: 3px; border-radius: 4px; }
        
        .page-content { position: relative; z-index: 10; display: flex; flex-direction: column; width: 100%; max-width: 1536px; margin: 0 auto; padding: 2rem 5%; }

        .bar { display: flex; align-items: center; justify-content: space-between; padding: 2rem 0; width: 100%; z-index: 50; }
        .brand { display:flex; align-items:center; gap: 0.5rem; }
        .brand svg { fill:var(--ink); width: 23px; height: 17px; }
        #word { font-variation-settings: 'wght' 531; font-size: 1.5rem; letter-spacing: 0.2rem; }

        .menu { display: flex; gap: 2.5rem; align-items: center; }
        .menu a { font-variation-settings: 'wght' 506; font-size: 1.15rem; color: var(--nav); transition: opacity 0.2s; }
        .menu a:hover { opacity: 0.7; }

        .actions { display: flex; gap: 1rem; align-items: center; }
        .pill { border:var(--hairline) solid var(--hair); border-radius:999px; padding: 0.6rem 1.5rem; font-variation-settings: 'wght' 581; font-size: 0.9rem; color: var(--ink); transition: background 0.2s; }
        .pill:hover { background: rgba(255,255,255,0.1); }

        .hero { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 80vh; text-align: center; gap: 2rem; padding: 4rem 0; }
        .title { font-variation-settings: 'wght' 424; font-size: clamp(2rem, 5vw, 4rem); line-height: 1.15; margin: 0; }
        .title span { display: block; }
        .sub { font-variation-settings: 'wght' 446; font-size: clamp(1rem, 2vw, 1.25rem); color: var(--sub); line-height: 1.4; max-width: 600px; margin: 0; }
        .sub span { display: block; }

        .cta {
          display: inline-flex; align-items: center; gap: 1rem; padding: 1rem 2rem; font-variation-settings: 'wght' 497; font-size: 1.25rem; color: var(--ink);
          border-radius:999px; position: relative; pointer-events: auto; cursor: pointer;
          background:
            linear-gradient(180deg, rgba(6,12,22,0) 45%, rgba(6,12,22,.12) 78%, rgba(6,12,22,.28) 100%),
            linear-gradient(90deg, rgba(255,226,178,.17) 0%, rgba(255,236,208,.07) 22%, rgba(176,206,238,.03) 55%, rgba(150,196,244,.09) 100%),
            linear-gradient(90deg, rgba(255,255,255,.24) 0%, rgba(255,255,255,.17) 12%, rgba(255,255,255,.11) 26%, rgba(255,255,255,.07) 42%, rgba(255,255,255,.05) 60%, rgba(255,255,255,.04) 80%, rgba(255,255,255,.04) 100%);
          -webkit-backdrop-filter:blur(var(--blur)) saturate(.45);
          backdrop-filter:blur(var(--blur)) saturate(.45);
          box-shadow:0 -2px 20px rgba(255,224,176,.20), 0 0 var(--glow) rgba(168,204,252,.16), inset 0 var(--hairline) 0 0 rgba(255,251,242,.45);
          transition: transform 0.2s, box-shadow 0.2s;
        }
        .cta:hover { transform: translateY(-2px); box-shadow:0 4px 30px rgba(255,224,176,.30), 0 0 calc(var(--glow)*1.5) rgba(168,204,252,.25), inset 0 var(--hairline) 0 0 rgba(255,251,242,.6); }
        .cta-secondary { padding: 0.75rem 1.6rem; font-size: 1.05rem; }
        .cta-secondary::before {
          background:
            linear-gradient(180deg, rgba(103,232,249,.9) 0%, rgba(103,232,249,.18) 45%, rgba(103,232,249,0) 78%),
            linear-gradient(90deg, rgba(34,211,238,.85) 0%, rgba(56,189,248,.55) 12%, rgba(14,116,144,.12) 32%, rgba(8,145,178,.1) 62%, rgba(34,211,238,.55) 90%, rgba(103,232,249,.9) 100%);
        }
        .cta-secondary .arrow { stroke:#a5f3fc; }
        .cta::before {
          content:''; position:absolute; inset:0; border-radius:inherit; padding:var(--hairline); pointer-events:none;
          background:
            linear-gradient(180deg, rgba(255,252,246,.95) 0%, rgba(255,252,246,.20) 45%, rgba(255,252,246,0) 78%),
            linear-gradient(90deg, rgba(255,228,182,.95) 0%, rgba(250,236,210,.70) 12%, rgba(226,230,234,.03) 32%, rgba(214,226,240,.02) 62%, rgba(200,228,250,.70) 90%, rgba(186,224,247,.95) 100%);
          -webkit-mask:linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0); -webkit-mask-composite:xor; mask:linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0); mask-composite:exclude;
        }
        .cta .arrow { fill:none; stroke:var(--ink); stroke-width:1.6; stroke-linecap:square; stroke-linejoin:miter; width: 16px; height: 11px; }

        .rule { background:linear-gradient(180deg, rgba(186,200,214,.70) 0%, rgba(206,220,232,.92) 52%, rgba(182,198,212,.68) 100%); width: 1px; height: 60px; display: block; margin: 2rem auto; opacity: 0.5; }

        /* Multi-Section Additions */
        .section { padding: 6rem 0; width: 100%; display: flex; flex-direction: column; align-items: center; gap: 4rem; }
        .section-header { text-align: center; max-width: 800px; display: flex; flex-direction: column; gap: 1rem; }
        .eyebrow { font-variation-settings: 'wght' 600; font-size: 0.85rem; letter-spacing: 0.1em; text-transform: uppercase; color: #a8ccfc; }
        .section-title { font-variation-settings: 'wght' 424; font-size: clamp(1.75rem, 3vw, 2.5rem); margin: 0; }
        .section-sub { font-variation-settings: 'wght' 446; font-size: 1.1rem; color: var(--sub); line-height: 1.5; margin: 0; }

        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.5rem; width: 100%; }
        .card { 
          background: linear-gradient(180deg, rgba(255,255,255,0.03) 0%, rgba(255,255,255,0.01) 100%);
          border: 1px solid rgba(255,255,255,0.08);
          border-radius: 16px; padding: 2rem;
          -webkit-backdrop-filter: blur(12px) saturate(1.2); backdrop-filter: blur(12px) saturate(1.2);
          transition: transform 0.3s, background 0.3s, border-color 0.3s;
          display: flex; flex-direction: column; gap: 1rem;
        }
        .card:hover { transform: translateY(-4px); background: linear-gradient(180deg, rgba(255,255,255,0.06) 0%, rgba(255,255,255,0.02) 100%); border-color: rgba(255,255,255,0.15); box-shadow: 0 10px 30px rgba(0,0,0,0.2); }
        .card h3 { font-variation-settings: 'wght' 500; font-size: 1.25rem; margin: 0; color: #e2ebf5; }
        .card p { font-variation-settings: 'wght' 400; font-size: 0.95rem; color: #8a96a8; line-height: 1.5; margin: 0; }

        .dashboard-mockup {
          width: 100%; max-width: 1200px; aspect-ratio: 16/9; border-radius: 24px;
          background: linear-gradient(180deg, rgba(10,15,25,0.8) 0%, rgba(6,10,18,0.9) 100%);
          border: 1px solid rgba(255,255,255,0.1);
          -webkit-backdrop-filter: blur(20px); backdrop-filter: blur(20px);
          box-shadow: 0 30px 60px rgba(0,0,0,0.5), 0 0 40px rgba(168,204,252,0.1);
          display: flex; flex-direction: column; overflow: hidden;
          margin-top: 2rem;
        }
        .dash-header { display: flex; align-items: center; padding: 1rem 1.5rem; border-bottom: 1px solid rgba(255,255,255,0.05); gap: 0.5rem; }
        .dash-dot { width: 12px; height: 12px; border-radius: 50%; background: #333; }
        .dash-dot.r { background: #ff5f56; } .dash-dot.y { background: #ffbd2e; } .dash-dot.g { background: #27c93f; }
        .dash-body { flex: 1; padding: 2rem; display: flex; gap: 2rem; }
        .dash-sidebar { width: 200px; display: flex; flex-direction: column; gap: 1rem; }
        .dash-skeleton-line { height: 24px; border-radius: 6px; background: rgba(255,255,255,0.05); width: 100%; }
        .dash-skeleton-line.short { width: 70%; }
        .dash-main { flex: 1; display: grid; grid-template-rows: 1fr 2fr; gap: 1.5rem; }
        .dash-skeleton-box { border-radius: 12px; background: rgba(255,255,255,0.03); border: 1px dashed rgba(255,255,255,0.08); width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: rgba(255,255,255,0.2); font-variation-settings: 'wght' 500; }

        .foot { text-align:center; color:var(--foot); font-variation-settings: 'wght' 521; font-size: 0.9rem; line-height:1.55; margin: 4rem 0 2rem 0; opacity: 0.7; }
        .foot span { display:block; }
        
        @media (max-width: 768px) {
          .menu { display: none; }
          .page-content { padding: 1rem 5%; }
        }

        /* Entrance Animations */
        @media (prefers-reduced-motion: no-preference) {
          :root { --ent-line: cubic-bezier(.16,1,.3,1); --ent-soft: cubic-bezier(.25,.8,.35,1); --rise: 15px; }
          @keyframes en-wipe { from { clip-path: inset(-.78em 0 calc(100% + .78em) 0); transform: translateY(var(--rise)); } to { clip-path: inset(-.78em 0 -.78em 0); transform: translateY(0); } }
          @keyframes en-lift { from { opacity: 0; transform: translateY(var(--rise)); } to { opacity: 1; transform: translateY(0); } }
          
          html:not(.is-entered) .brand { animation: en-lift .60s var(--ent-soft) .12s backwards; }
          html:not(.is-entered) .menu a { animation: en-lift .55s var(--ent-soft) .20s backwards; }
          html:not(.is-entered) .actions { animation: en-lift .60s var(--ent-soft) .40s backwards; }
          html:not(.is-entered) #h1a { animation: en-wipe .95s var(--ent-line) .34s backwards; }
          html:not(.is-entered) #h1b { animation: en-wipe .95s var(--ent-line) .44s backwards; }
          html:not(.is-entered) #sub1 { animation: en-lift .70s var(--ent-soft) .74s backwards; }
          html:not(.is-entered) #sub2 { animation: en-lift .70s var(--ent-soft) .80s backwards; }
          html:not(.is-entered) .cta { animation: en-lift .80s var(--ent-line) .94s backwards; }
          html:not(.is-entered) .rule { animation: en-lift .80s var(--ent-line) 1.2s backwards; }
        }
      `}} />

      {/* Cinematic Background is now globally rendered from layout.tsx */}
      
      <div className="page-content">
        <header className="bar">
          <a className="brand" href="#">
            <svg viewBox="0 0 23 17" aria-hidden="true">
              <path d="M8.15 0.9 L4.55 0.9 L0.5 9.3 L4.1 9.3 Z"/>
              <path d="M17.0 0 L13.4 0 L6.15 16.4 L9.75 16.4 Z"/>
              <path d="M22.9 0 L19.3 0 L15.0 7.6 L18.6 7.6 Z"/>
              <path d="M22.6 6.9 L19.0 6.9 L14.05 16.4 L17.65 16.4 Z"/>
            </svg>
            <span id="word">SARA OS</span>
          </a>
          
          <nav className="menu">
            <a href="#agents">Agents</a>
            <a href="#engine">Engine</a>
            <a href="#network">Network</a>
          </nav>
          
          <div className="actions">
            {userId ? (
              <UserButton />
            ) : (
              <a className="pill" onClick={() => openSignIn()}>Login / Sign up</a>
            )}
          </div>
        </header>

        <main className="hero">
          <h1 className="title">
            <span id="h1a">The Ultimate AI Agent</span>
            <span id="h1b">Operating System.</span>
          </h1>
          <p className="sub">
            <span id="sub1">Deploy autonomous intelligence to build,</span>
            <span id="sub2">scale, and automate your entire workflow.</span>
          </p>
          
          <a className="cta" onClick={() => router.push('/hub')}>
            <span id="cta">Explore Nexus</span>
            <svg className="arrow" viewBox="0 0 16 11" aria-hidden="true">
              <path d="M0 5.5 H14.6 M10.3 1.2 L14.9 5.5 L10.3 9.8"/>
            </svg>
          </a>

          <a className="cta cta-secondary" onClick={() => router.push('/hub/sara-bots')}>
            <span id="cta-bots">Sara Bots</span>
            <svg className="arrow" viewBox="0 0 16 11" aria-hidden="true">
              <path d="M0 5.5 H14.6 M10.3 1.2 L14.9 5.5 L10.3 9.8"/>
            </svg>
          </a>
          
          <span className="rule" aria-hidden="true"></span>
        </main>

        {/* Dynamic Sections */}
        <section className="section" id="agents">
          <div className="section-header">
            <span className="eyebrow">The Autonomous Workforce</span>
            <h2 className="section-title">10 Highly-Specialized Agents</h2>
            <p className="section-sub">Deploy a full engineering and marketing department instantly. Our agents collaborate natively to design, build, test, and ship at superhuman speed.</p>
          </div>
          
          <div className="grid">
            {agents.map((agent, i) => (
              <div 
                className="card" 
                key={i} 
                onClick={() => router.push(agent.link)}
                style={{ cursor: 'pointer' }}
              >
                <h3>{agent.title}</h3>
                <p>{agent.desc}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="section" id="engine">
          <div className="section-header">
            <span className="eyebrow">Command Center</span>
            <h2 className="section-title">One Platform to Rule Them All</h2>
            <p className="section-sub">Stop stitching tools together. Monitor multi-agent collaboration, review autonomous commits, and direct deployment infrastructure from a single pane of glass.</p>
          </div>

          <img 
            src="/dashboard-mockup.jpg" 
            alt="SARA OS Command Center Dashboard" 
            className="dashboard-mockup"
            style={{ padding: 0, objectFit: 'cover' }}
          />
        </section>

        <footer className="foot" id="foot2">
          <span>Trusted by innovative teams around the world.</span>
          <span>© 2026 SARA OS</span>
        </footer>
      </div>
    </>
  );
}
