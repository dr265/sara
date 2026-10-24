"use client";

import React, { useState, useMemo, useEffect, useCallback } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AGENT_REGISTRY, AgentCategory, HubAgent } from "@/lib/agents.config";
import AgentCard from "@/components/hub/AgentCard";
import ApogeeBackground from "@/components/hub/ApogeeBackground";
import HologramCanvas from "@/components/hub/HologramCanvas";
import { saraFetch } from "@/lib/api";

// Premium agency card — lightweight, reuses AgentCard visual language but
// wired to /agency/[id] and division metadata.
function AgencyCard({ agent }: { agent: any }) {
  const bg = useMemo(() => {
    let h = 0; for (let i=0;i<agent.name.length;i++) h = agent.name.charCodeAt(i) + ((h<<5)-h);
    const c1 = `hsl(${Math.abs(h)%360},70%,55%)`; const c2=`hsl(${Math.abs(h*2)%360},60%,35%)`;
    return `radial-gradient(circle at 0% 0%, ${c1} 0%, transparent 55%), radial-gradient(circle at 100% 100%, ${c2} 0%, transparent 50%), #0a0e1a`;
  }, [agent.name]);
  return (
    <div className="flex flex-col gap-3 group rounded-2xl bg-[#111216]/50 p-2.5 border border-white/5 hover:border-white/10 transition-all shadow-xl hover:shadow-2xl hover:-translate-y-1">
      <div className="relative w-full aspect-[16/9] rounded-[14px] overflow-hidden shadow-inner bg-black">
        <div className="absolute inset-0 opacity-60 group-hover:opacity-90 transition-opacity" style={{ background: bg }} />
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1">
          <div className="text-3xl drop-shadow-xl">{agent.emoji || "🤖"}</div>
          <span className="text-[10px] font-mono tracking-widest text-white/60">{agent.division_label}</span>
        </div>
        <span className="absolute top-2 right-2 px-2 py-1 rounded-full text-[9px] font-mono bg-black/50 backdrop-blur border border-white/10 text-white/80">{agent.division}</span>
      </div>
      <div className="flex flex-col px-1 pb-1 flex-1">
        <div className="flex items-center gap-2 mb-1">
          <h3 className="text-white font-bold text-[13px] leading-tight truncate flex-1 group-hover:text-cyan-300 transition-colors">{agent.name}</h3>
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)]" />
        </div>
        <p className="text-slate-400 text-[12px] leading-relaxed line-clamp-2 h-[32px]">{agent.description || agent.vibe || ""}</p>
        <div className="mt-3">
          <Link href={`/agency/${agent.id}`} className="grid place-items-center h-9 rounded-xl text-xs font-bold text-white border border-white/15 bg-white/10 hover:bg-white/15 hover:border-cyan-400/30 transition-colors">
            Launch Workspace →
          </Link>
        </div>
      </div>
    </div>
  );
}

const CATEGORIES: ("All" | AgentCategory | string)[] = [
  "All","Engineering","Business","Marketing","Finance","General","Health","Education","Coding","Image Generation","Productivity","Research","Other",
];

// Division filter values — maps to agency divisions + hub categories
const DIVISIONS = ["All","academic","design","engineering","finance","game-development","gis","healthcare","marketing","paid-media","product","project-management","research","sales","security","spatial-computing","specialized","support","testing"] as const;

export default function AgentHub() {
  const router = useRouter();
  const [searchQuery, setSearchQuery] = useState("");
  const [activeCategory, setActiveCategory] = useState<string>("All");
  const [activeDivision, setActiveDivision] = useState<string>("All");
  const [activeTab, setActiveTab] = useState<"hub"|"agency"|"all">("all");
  const [agencyAgents, setAgencyAgents] = useState<any[]>([]);
  const [agencyLoaded, setAgencyLoaded] = useState(false);
  const [isRouting, setIsRouting] = useState(false);
  const [orchestratorPrompt, setOrchestratorPrompt] = useState("");
  const [isListening, setIsListening] = useState(false);
  const [routeError, setRouteError] = useState("");
  const [llmHealth, setLlmHealth] = useState<any>(null);

  // Load agency agents once
  useEffect(() => {
    const base = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";
    (async () => {
      try {
        const r = await saraFetch(`${base}/api/agency/agents?limit=400`, { cache: "no-store" as RequestCache });
        if (r.ok) { const j = await r.json(); setAgencyAgents(j.agents ?? []); }
      } catch { /* agents stay empty; the tab shows the count */ }
      setAgencyLoaded(true);
    })();
    (async () => {
      try {
        const r = await saraFetch(`${base}/api/agency/llm/health`, { cache: "no-store" as RequestCache });
        if (r.ok) setLlmHealth(await r.json());
      } catch { /* chip stays hidden */ }
    })();
  }, []);

  // Unified search across hub + agency
  const filteredHub = useMemo(() => {
    return AGENT_REGISTRY.filter(a => {
      const q = searchQuery.toLowerCase();
      const matchesSearch = !q || a.name.toLowerCase().includes(q) || a.shortDescription.toLowerCase().includes(q) || a.capabilities.some(c=>c.toLowerCase().includes(q)) || a.id.toLowerCase().includes(q);
      const matchesCat = activeCategory==="All" || a.category===activeCategory;
      return matchesSearch && matchesCat;
    });
  }, [searchQuery, activeCategory]);

  const filteredAgency = useMemo(() => {
    const q = searchQuery.toLowerCase();
    return agencyAgents.filter((a:any) => {
      const matchesSearch = !q || a.name.toLowerCase().includes(q) || a.description.toLowerCase().includes(q) || a.division.toLowerCase().includes(q) || a.id.toLowerCase().includes(q) || (a.vibe||"").toLowerCase().includes(q);
      const matchesDivision = activeDivision==="All" || a.division===activeDivision;
      const matchesCat = true; // agency handles its own division filter
      return matchesSearch && matchesDivision;
    });
  }, [searchQuery, activeDivision, agencyAgents]);

  const handleOrchestrate = useCallback(async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!orchestratorPrompt.trim()) return;
    setIsRouting(true); setRouteError("");
    const q = orchestratorPrompt.toLowerCase();
    // 1) Try local hub match first (instant)
    let target = AGENT_REGISTRY.find(a => a.name.toLowerCase().includes(q) || a.category.toLowerCase().includes(q) || a.capabilities.some(c=>c.toLowerCase().includes(q)) || a.id.toLowerCase().includes(q));
    if (target) { router.push(target.route); return; }
    // 2) Try agency match
    let atarget = agencyAgents.find((a:any)=> a.name.toLowerCase().includes(q) || a.division.toLowerCase().includes(q) || a.description.toLowerCase().includes(q) || a.id.toLowerCase().includes(q));
    if (atarget) { router.push(`/agency/${atarget.id}`); return; }
    // 3) Fallback: ask backend LLM router (real LLM call)
    try {
      const base = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";
      const r = await saraFetch(`${base}/api/v1/orchestrate`, { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify({ prompt: orchestratorPrompt }) });
      if (r.ok) { const j = await r.json(); if (j.route) { router.push(j.route); return; } }
    } catch {}
    setRouteError("No matching agent. Try keywords like 'backend', 'marketing', 'security', 'finance', or describe your goal.");
    setIsRouting(false);
  }, [orchestratorPrompt, agencyAgents, router]);

  const toggleVoice = () => {
    // Use Web Speech API when available; else mock
    const SR: any = (typeof window !== "undefined" && ((window as any).SpeechRecognition || (window as any).webkitSpeechRecognition));
    if (SR && !isListening) {
      try {
        const rec = new SR(); rec.lang="en-US"; rec.interimResults=false; rec.maxAlternatives=1;
        setIsListening(true); setRouteError("");
        rec.onresult = (e:any) => { const t=e.results[0][0].transcript; setOrchestratorPrompt(t); setIsListening(false); };
        rec.onerror = () => setIsListening(false); rec.onend = () => setIsListening(false);
        rec.start(); return;
      } catch {}
    }
    setIsListening(!isListening);
    if (!isListening) {
       setOrchestratorPrompt(""); setRouteError("");
       setTimeout(()=>{ setOrchestratorPrompt("I need a backend architect to design my database"); setIsListening(false); }, 1600);
    } else { setOrchestratorPrompt(""); }
  };

  const hubCount = filteredHub.length;
  const agencyCount = filteredAgency.length;
  const totalVisible = activeTab==="hub" ? hubCount : activeTab==="agency" ? agencyCount : hubCount + agencyCount;

  return (
    <main className="relative z-10 min-h-screen w-full bg-transparent text-white pb-20">
      <style>{`
        .vertex-badge-mobile { height:36px; border-radius:18px; position:relative; border:1px solid rgba(255,255,255,.115); background:linear-gradient(to top, rgba(190,225,255,.175) 0px, rgba(190,225,255,.128) 2px, rgba(190,225,255,.075) 4px, rgba(190,225,255,.026) 6px, rgba(255,255,255,.012) 9px, rgba(255,255,255,.012) 100%); backdrop-filter:blur(12px); box-shadow:inset 0 1px 0 rgba(255,255,255,.035); flex:0 0 auto; cursor:pointer; transition:filter 0.2s, border-color 0.2s; }
        .vertex-badge-mobile:hover { filter:brightness(1.2); }
        .vertex-badge-mobile.active { border-color: rgba(60,190,230,.5); background:rgba(60,190,230,.12); }
        .vertex-badge-mobile b { position:relative; height:36px; padding:0 15px 0 42px; font-size:12.5px; display:flex; align-items:center; color:rgba(255,255,255,.94); }
        .vertex-badge-mobile i { position:absolute; top:3px; left:3px; width:28px; height:28px; border-radius:14px; display:grid; place-items:center; background:linear-gradient(to top, #46afc8 0px, #35abc7 2px, #0b859d 4px, #026c84 6px, #004e66 8px, #053f58 10px, #012c3d 12px, #031a2a 14px, #061125 16px, #090f25 19px, #090c1c 23px, #060d16 29px); box-shadow:inset 1px 0 0 rgba(150,220,250,.40), inset -1px 0 0 rgba(150,220,250,.52), 0 0 6px rgba(60,190,230,.20), 0 3px 8px -5px rgba(90,220,255,.6); }
        .vertex-badge-mobile i svg { width:13px; height:19px; position:relative; top:-1px; }
      `}</style>

      <ApogeeBackground />

      <div className="relative z-20 space-y-7 pt-6 max-w-[1600px] mx-auto">
        {/* HERO — Cinematic title + mode switcher */}
        <section className="px-6">
          <div className="relative overflow-hidden rounded-[24px] border border-white/10 bg-gradient-to-br from-[#0a0e17] via-[#0e1220] to-[#07090e] p-6 sm:p-8">
            <div className="absolute inset-0 opacity-40" style={{ background:"radial-gradient(600px 300px at 70% 0%, rgba(56,189,248,0.15), transparent 60%), radial-gradient(500px 400px at 0% 100%, rgba(99,102,241,0.12), transparent 60%)"}} />
            <div className="relative z-10 flex flex-col lg:flex-row lg:items-center justify-between gap-4">
              <div>
                <div className="inline-flex items-center gap-2 px-2.5 py-1 rounded-full border border-cyan-500/30 bg-cyan-500/10 text-[11px] font-mono tracking-widest text-cyan-300">NEXUS AGENT HUB · 282 NODES</div>
                <h1 className="mt-3 text-2xl sm:text-3xl font-black tracking-tight text-white">Every specialist. One command center.</h1>
                <p className="mt-2 text-sm text-slate-400 max-w-2xl">15 core SARA agents + 267 agency specialists. Voice or text — the orchestrator routes you to the right workspace and the LLM failover chain keeps every agent online.</p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                { (["all","hub","agency"] as const).map(t=>(
                  <button key={t} onClick={()=>setActiveTab(t)} className={`px-4 py-2 rounded-full text-xs font-bold border transition-colors ${activeTab===t ? "bg-white text-black border-white" : "bg-white/10 text-white border-white/10 hover:bg-white/15"}`}>
                    {t==="all" ? `All (${AGENT_REGISTRY.length + agencyAgents.length})` : t==="hub" ? `Core Hub (${AGENT_REGISTRY.length})` : `Agency (${agencyAgents.length})`}
                  </button>
                ))}
                <Link href="/hub/sara-bots" className="px-4 py-2 rounded-full text-xs font-bold border border-cyan-400/40 bg-cyan-500/15 text-cyan-200 hover:bg-cyan-500/25 transition-colors inline-flex items-center gap-1.5">
                  <span className="h-1.5 w-1.5 rounded-full bg-cyan-300 shadow-[0_0_6px_rgba(103,232,249,0.9)]" />
                  Sara Bots →
                </Link>
              </div>
            </div>
            {llmHealth && (
              <div className="relative z-10 mt-4 flex flex-wrap items-center gap-2 text-[11px]">
                <span className="text-slate-500 font-mono">LLM</span>
                <span className={`px-2 py-1 rounded-full border font-mono ${llmHealth.any_available ? "border-emerald-500/30 text-emerald-300 bg-emerald-500/10" : "border-red-500/30 text-red-300 bg-red-500/10"}`}>
                  {llmHealth.any_available ? `● ${llmHealth.active ?? "online"} · ${llmHealth.live_count}/${llmHealth.total_count} providers` : "○ offline — local fallback active"}
                </span>
                <Link href="/agency/llm" className="px-2 py-1 rounded-full border border-white/10 text-slate-300 hover:text-white hover:border-white/20">LLM Dashboard →</Link>
                {llmHealth.providers?.slice(0,4).map((p:any)=>(
                  <span key={p.name} className={`px-1.5 py-0.5 rounded-full border text-[10px] ${p.healthy ? "border-white/10 text-slate-300" : "border-amber-500/30 text-amber-300"}`}>{p.name}</span>
                ))}
              </div>
            )}
          </div>
        </section>

        {/* TWO SYSTEM AGENTS */}
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-6 px-6">
          <div className="relative overflow-hidden rounded-[24px] border border-cyan-500/20 bg-[#0a0e17]/80 backdrop-blur-xl p-6 sm:p-8 shadow-2xl group flex flex-col justify-between min-h-[280px]">
            <div className="absolute inset-0 z-0 opacity-40 group-hover:opacity-70 transition-opacity duration-1000"><HologramCanvas /></div>
            <div className="relative z-10">
              <div className="flex items-center gap-3 mb-3"><span className="flex h-2.5 w-2.5 relative"><span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-cyan-400 opacity-75" /><span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-cyan-500" /></span><span className="text-xs font-mono uppercase tracking-widest text-cyan-400">Core Router · Voice + Text</span></div>
              <h2 className="text-2xl font-bold text-white mb-2">Hologram Orchestrator</h2>
              <p className="text-sm text-slate-400 max-w-md">Speak or type your goal. Routes across all {AGENT_REGISTRY.length + agencyAgents.length} agents — hub + agency.</p>
            </div>
            <div className="relative z-10 mt-6 space-y-2">
               <form onSubmit={handleOrchestrate} className="relative flex items-center">
                 <button type="button" onClick={toggleVoice} className={`absolute left-2.5 p-2 rounded-full transition-colors z-20 ${isListening ? 'bg-red-500 text-white animate-pulse' : 'text-slate-400 hover:text-cyan-300 hover:bg-white/5'}`} title="Voice input">
                    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"></path></svg>
                 </button>
                 <input type="text" value={orchestratorPrompt} onChange={e=>setOrchestratorPrompt(e.target.value)} placeholder={isListening ? "Listening… speak now" : "e.g., 'design a secure payment API' or 'write a TikTok growth plan'…"} className="w-full rounded-2xl bg-black/50 border border-cyan-800/50 pl-12 pr-28 py-3.5 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-cyan-400 transition-all" />
                 <button type="submit" disabled={isRouting} className="absolute right-1.5 px-5 py-2.5 rounded-xl bg-cyan-500 text-black text-xs font-black hover:bg-cyan-400 disabled:opacity-50">{isRouting ? 'Routing…' : 'Route'}</button>
               </form>
               {routeError && <p className="text-amber-300 text-xs font-mono ml-1">{routeError}</p>}
            </div>
          </div>

          <div className="relative overflow-hidden rounded-[24px] border border-indigo-500/20 bg-[#0c0d14]/80 backdrop-blur-xl p-6 sm:p-8 shadow-2xl flex flex-col justify-between min-h-[280px] group">
            <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_bottom_right,_var(--tw-gradient-stops))] from-indigo-900/40 via-transparent to-transparent opacity-50 group-hover:opacity-100 transition-opacity duration-700 pointer-events-none" />
            <div className="relative z-10">
              <div className="flex items-center gap-3 mb-3"><span className="text-xs font-mono uppercase tracking-widest text-indigo-400">Master Workspace · Desktop + Voice</span></div>
              <h2 className="text-2xl font-bold text-white mb-2">SARA Desktop Agent</h2>
              <p className="text-sm text-slate-400 max-w-md">Full PC control. Jarvis standalone · window management · voice sessions.</p>
              <div className="mt-3 flex flex-wrap gap-2 text-[11px] font-mono">
                <span className="px-2 py-1 rounded-full bg-white/5 border border-white/10 text-slate-300">Normal System</span>
                <span className="px-2 py-1 rounded-full bg-white/5 border border-white/10 text-slate-300">Agent System</span>
                <span className="px-2 py-1 rounded-full bg-indigo-500/20 border border-indigo-500/30 text-indigo-200">Voice-controlled</span>
              </div>
            </div>
            <div className="relative z-10 mt-6 flex flex-wrap gap-2">
               <Link href="/workspace/desktop-agent" className="inline-flex items-center justify-center gap-2 px-6 py-3 rounded-2xl bg-indigo-600 text-white text-sm font-bold hover:bg-indigo-500 shadow-[0_0_20px_rgba(79,70,229,0.3)]">Launch Desktop Agent</Link>
               <Link href="/agency/llm" className="inline-flex items-center justify-center gap-2 px-5 py-3 rounded-2xl bg-white/10 text-white text-sm font-semibold border border-white/10 hover:bg-white/15">LLM Dashboard</Link>
            </div>
          </div>
        </section>

        {/* Search + Filters */}
        <section className="space-y-3 px-6">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <h3 className="text-[13px] font-bold text-white tracking-wide">Browse Agents</h3>
              <span className="text-[11px] font-mono text-slate-500 uppercase bg-white/5 px-3 py-1 rounded-full border border-white/10">{totalVisible} shown</span>
              {!agencyLoaded && <span className="text-[11px] font-mono text-amber-300">loading agency…</span>}
            </div>
            <div className="relative w-full sm:w-[360px]">
              <input type="text" value={searchQuery} onChange={e=>setSearchQuery(e.target.value)} placeholder="Search 282 agents — try 'payment', 'TikTok', 'security'…"
                className="w-full rounded-full bg-black/40 backdrop-blur-md border border-white/10 px-4 py-2.5 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500/50" />
              <svg className="absolute right-4 top-3 w-4 h-4 text-slate-500 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
            </div>
          </div>

          {(activeTab==="all" || activeTab==="hub") && (
            <div className="flex items-center gap-2 overflow-x-auto pb-2 scrollbar-none pt-1">
              {CATEGORIES.map((cat) => (
                <button key={cat} className={`vertex-badge-mobile ${activeCategory===cat ? 'active' : ''}`} onClick={() => setActiveCategory(cat)}>
                  <i><svg viewBox="5 1 14 22" preserveAspectRatio="none" fill="rgba(16,112,152,.72)" stroke="rgba(190,236,255,.6)" strokeWidth="1.6" strokeLinejoin="round"><path d="M13.9 1.6 5.5 13.6a.7.7 0 0 0 .6 1.1h4.2l-1 7.7a.7.7 0 0 0 1.25.55l8.3-12.1a.7.7 0 0 0-.6-1.1h-4.2l1-7.7a.7.7 0 0 0-1.25-.55Z"/></svg></i>
                  <b>{cat}</b>
                </button>
              ))}
            </div>
          )}
          {(activeTab==="all" || activeTab==="agency") && (
            <div className="flex items-center gap-2 overflow-x-auto pb-2 scrollbar-none">
              <span className="text-[11px] font-mono text-slate-500 shrink-0">Divisions</span>
              {DIVISIONS.map(d=>(
                <button key={d} onClick={()=>setActiveDivision(d)} className={`shrink-0 px-3 py-1.5 rounded-full text-xs font-semibold border ${activeDivision===d ? "bg-white text-black border-white" : "bg-white/5 text-slate-300 border-white/10 hover:bg-white/10"}`}>{d==="All"?"All divisions":d}</button>
              ))}
            </div>
          )}
        </section>

        {/* Grids */}
        {(activeTab==="all" || activeTab==="hub") && (
          <section className="relative z-30 px-6">
            <div className="flex items-center gap-2 mb-3">
              <h4 className="text-xs font-mono tracking-widest text-slate-400">CORE HUB</h4>
              <span className="text-[11px] font-mono px-2 py-0.5 rounded-full bg-white/5 border border-white/10 text-slate-400">{hubCount} agents</span>
            </div>
            {hubCount>0 ? (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-x-6 gap-y-6">
                {filteredHub.map(a=> <AgentCard key={a.id} agent={a} />)}
              </div>
            ) : <div className="text-center py-10 rounded-2xl border border-white/5 bg-white/[0.03] text-sm text-slate-400">No hub agents match your filter.</div>}
          </section>
        )}

        {(activeTab==="all" || activeTab==="agency") && (
          <section className="relative z-30 px-6">
            <div className="flex items-center gap-2 mb-3">
              <h4 className="text-xs font-mono tracking-widest text-slate-400">AGENCY SPECIALISTS</h4>
              <span className="text-[11px] font-mono px-2 py-0.5 rounded-full bg-white/5 border border-white/10 text-slate-400">{agencyCount} / {agencyAgents.length}</span>
              <span className="hidden sm:inline text-[11px] text-slate-500">· each is a full system-prompt agent via <code className="px-1 py-0.5 rounded bg-white/10">llm_router</code></span>
            </div>
            {agencyCount>0 ? (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-x-6 gap-y-6">
                {filteredAgency.slice(0, 80).map((a:any)=> <AgencyCard key={a.id} agent={a} />)}
              </div>
            ) : <div className="text-center py-10 rounded-2xl border border-white/5 bg-white/[0.03] text-sm text-slate-400">No agency agents match your filter.</div>}
            {agencyCount>80 && <div className="text-center mt-4 text-xs text-slate-500">Showing 80 of {agencyCount} — refine search to narrow.</div>}
          </section>
        )}

        {totalVisible===0 && (
          <section className="px-6">
            <div className="text-center py-16 rounded-[24px] border border-white/5 bg-white/[0.03] backdrop-blur">
              <p className="text-sm font-mono text-slate-400 mb-4">NO MATCHING NODES</p>
              <button onClick={()=>{ setSearchQuery(""); setActiveCategory("All"); setActiveDivision("All"); }} className="text-xs text-cyan-400 hover:text-white border border-cyan-500/30 px-6 py-2 rounded-full font-bold">Reset filters</button>
            </div>
          </section>
        )}
      </div>
    </main>
  );
}
