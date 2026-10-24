"use client";

import React, { useEffect, useState, useRef, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { saraFetch } from "@/lib/api";

type AgencyAgentDetail = {
  id: string; division: string; division_label: string; division_color: string;
  division_icon: string; name: string; description: string; emoji: string; vibe: string; color: string;
  body: string; pipeline: string[]; tools: string[];
};
type Health = { providers:{name:string;healthy:boolean}[]; active:string|null; live_count:number; total_count:number };
type RunStage = { stage:string; index:number; total:number; status:"running"|"done" };
type ToolEvent = { tool:string; stage:string; ok?:boolean; summary?:string };

function SceneCanvas({ color, emoji }: { color: string; emoji: string }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const raf = useRef<number | null>(null);
  useEffect(()=>{
    const canvas = ref.current; if(!canvas) return;
    const ctx = canvas.getContext("2d"); if(!ctx) return;
    let dpr=1;
    const resize=()=>{
      const r=canvas.getBoundingClientRect();
      dpr=Math.min(2, window.devicePixelRatio||1);
      canvas.width=Math.round(r.width*dpr); canvas.height=Math.round(r.height*dpr);
      ctx.setTransform(dpr,0,0,dpr,0,0);
    };
    resize(); const ro=new ResizeObserver(resize); ro.observe(canvas);
    const hex=(color||"#3b82f6").trim().replace(/["']/g,"");
    const toRgb=(s:string):[number,number,number]=>{
      if(s.startsWith("#")){ const n=s.slice(1).padEnd(6,"0"); return [parseInt(n.slice(0,2),16),parseInt(n.slice(2,4),16),parseInt(n.slice(4,6),16)]; }
      return [59,130,246];
    };
    const [rv,gv,bv]=toRgb(hex);
    let t=0;
    const draw=()=>{
      t+=0.006; const W=canvas.getBoundingClientRect().width, H=canvas.getBoundingClientRect().height;
      ctx.clearRect(0,0,W,H);
      // soft vignette
      const bg=ctx.createRadialGradient(W*0.5,H*0.55,0, W*0.5,H*0.55, Math.max(W,H)*0.75);
      bg.addColorStop(0, `rgba(${rv},${gv},${bv},0.11)`); bg.addColorStop(0.55, `rgba(0,0,0,0)`); bg.addColorStop(1, `rgba(0,0,0,0.52)`);
      ctx.fillStyle=bg; ctx.fillRect(0,0,W,H);
      // floating orb
      ctx.save(); ctx.translate(W*0.5, H*0.42 + Math.sin(t*0.9)*5);
      const R=Math.min(W,H)*0.14;
      const g=ctx.createRadialGradient(-R*0.22,-R*0.2, R*0.2, 0,0,R);
      g.addColorStop(0, `rgba(255,255,255,0.96)`); g.addColorStop(0.22, `rgba(${rv},${gv},${bv},0.92)`); g.addColorStop(0.55, `rgba(${rv},${gv},${bv},0.42)`); g.addColorStop(1, `rgba(6,10,18,0)`);
      ctx.fillStyle=g; ctx.beginPath(); ctx.arc(0,0,R,0,Math.PI*2); ctx.fill();
      ctx.fillStyle="rgba(255,255,255,0.93)"; ctx.font=`${Math.round(R*0.9)}px ui-emoji, Apple Color Emoji, Segoe UI Emoji, sans-serif`; ctx.textAlign="center"; ctx.textBaseline="middle"; ctx.fillText(emoji||"◍",0,1);
      ctx.strokeStyle=`rgba(${rv},${gv},${bv},0.34)`; ctx.lineWidth=1; ctx.beginPath(); ctx.ellipse(0,0,R*1.55,R*1.22, -0.18+Math.sin(t*0.55)*0.04, 0, Math.PI*2); ctx.stroke();
      ctx.strokeStyle="rgba(255,255,255,0.09)"; ctx.beginPath(); ctx.ellipse(0,0,R*1.95,R*1.55, 0.18+Math.cos(t*0.6)*0.03, 0, Math.PI*2); ctx.stroke();
      ctx.restore();
      // perspective grid
      ctx.save(); ctx.strokeStyle="rgba(255,255,255,0.055)"; ctx.lineWidth=1;
      for(let i=0;i<5;i++){ const y=H*0.66 + i*(H*0.055); const off=i*W*0.015 + Math.sin(t*0.7+i)*1.5; ctx.beginPath(); ctx.moveTo(W*0.08+off, y); ctx.bezierCurveTo(W*0.33, y+3, W*0.66, y-3, W*0.92 - off*0.5, y); ctx.stroke(); }
      ctx.restore();
      raf.current=requestAnimationFrame(draw);
    }; draw(); return()=>{ if(raf.current) cancelAnimationFrame(raf.current); ro.disconnect(); };
  },[color,emoji]);
  return <canvas ref={ref} className="absolute inset-0 w-full h-full" style={{width:"100%",height:"100%"}} aria-hidden />;
}

export default function AgencyWorkspacePage() {
  const params=useParams(); const router=useRouter(); const agentId=params.agentId as string;
  const [agent,setAgent]=useState<AgencyAgentDetail|null>(null);
  const [loading,setLoading]=useState(true); const [error,setError]=useState("");
  const [input,setInput]=useState(""); const [isStreaming,setIsStreaming]=useState(false);
  const [messages,setMessages]=useState<{role:string;content:string;ts?:string}[]>([]);
  const [activeTab,setActiveTab]=useState<"workspace"|"history"|"knowledge"|"settings">("workspace");
  const [llmHealth,setLlmHealth]=useState<Health|null>(null);
  const [copyOk,setCopyOk]=useState(false);
  const [history,setHistory]=useState<any[]>([]);
  const [stages,setStages]=useState<RunStage[]>([]);
  const [toolEvents,setToolEvents]=useState<ToolEvent[]>([]);
  const [runMeta,setRunMeta]=useState<{run_id:string; elapsed_ms?:number}|null>(null);
  const [modelOverride,setModelOverride]=useState("");
  const [maxTokens,setMaxTokens]=useState(2048);
  const endRef=useRef<HTMLDivElement>(null); const inputRef=useRef<HTMLInputElement>(null);
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";

  // load agent + health + history
  useEffect(()=>{
    let c=false;
    (async()=>{
      try{
        const r=await saraFetch(`${backendUrl}/api/agency/agents/${agentId}`); if(!r.ok) throw new Error(await r.text());
        const j=await r.json(); if(!c){ setAgent(j); setMessages([{role:"agent",content:`${j.name} — ${j.vibe || j.description.slice(0,160)}\n\nI'm live. Tell me your goal and I'll run a real staged pipeline.`}]); }
        try{ const lh=await saraFetch(`${backendUrl}/api/agency/llm/health`); if(lh.ok) setLlmHealth(await lh.json()); }catch{}
        try{ const hr=await saraFetch(`${backendUrl}/api/agency/agents/${agentId}/history`); if(hr.ok){ const hj=await hr.json(); if(!c) setHistory(hj.messages||[]); } }catch{}
      }catch(e:any){ if(!c) setError(e.message||"Failed to load agent"); }
      finally{ if(!c) setLoading(false); }
    })(); return()=>{ c=true; };
  },[agentId,backendUrl]);

  useEffect(()=>{ endRef.current?.scrollIntoView({behavior:"smooth"}); },[messages, isStreaming, stages]);

  const refreshHistory=useCallback(async()=>{
    try{ const r=await saraFetch(`${backendUrl}/api/agency/agents/${agentId}/history`); if(r.ok){ const j=await r.json(); setHistory(j.messages||[]); } }catch{}
  },[agentId,backendUrl]);

  const clearHistory=useCallback(async()=>{
    if(!confirm("Clear persisted history for this agent?")) return;
    await saraFetch(`${backendUrl}/api/agency/agents/${agentId}/history`,{method:"DELETE"});
    setHistory([]); setMessages([{role:"agent",content:`${agent?.name||agentId} — history cleared. Ready for a new run.`}]);
  },[agent,agentId,backendUrl]);

  const handleSend=useCallback(async(e?:React.FormEvent)=>{
    e?.preventDefault(); if(!input.trim()||isStreaming||!agent) return;
    const text=input.trim();
    setMessages(m=>[...m,{role:"user",content:text}]);
    setInput(""); setIsStreaming(true); setStages([]); setToolEvents([]); setRunMeta(null);
    // streaming placeholder for assistant
    let assistantIdx=-1;
    setMessages(m=>{ assistantIdx=m.length+1; return [...m, {role:"agent", content:""}]; });

    try{
      const hist=messages.slice(-6).map(m=>({role:m.role==="agent"?"assistant" as const:"user" as const, content:m.content})).filter(h=>h.content && h.content.length>0 && !h.content.startsWith(agent.name+" —"));
      const res=await saraFetch(`${backendUrl}/api/agency/agents/${agentId}/run-stream`,{
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({ message:text, history: hist, model: modelOverride||undefined, max_tokens: maxTokens }),
      });
      if(!res.ok){ const t=await res.text(); throw new Error(t); }
      if(!res.body) throw new Error("No stream body");
      const reader=res.body.getReader(); const decoder=new TextDecoder(); let buf="";
      let full="";
      while(true){
        const { done, value } = await reader.read(); if(done) break;
        buf+=decoder.decode(value,{stream:true});
        const parts=buf.split("\n\n"); buf=parts.pop()||"";
        for(const p of parts){
          const line=p.trim(); if(!line.startsWith("data:")) continue;
          const jsonStr=line.slice(5).trim(); if(!jsonStr) continue;
          try{
            const obj=JSON.parse(jsonStr);
            if(obj.type==="meta"){ setRunMeta({run_id:obj.run_id}); }
            else if(obj.type==="stage"){ setStages(prev=>{ const next=[...prev]; const idx=next.findIndex(s=>s.stage===obj.stage); if(idx>=0) next[idx]=obj; else next.push(obj); return next.sort((a,b)=>a.index-b.index); }); }
            else if(obj.type==="tool_call"){ setToolEvents(prev=>[...prev,{tool:obj.tool, stage:obj.stage}]); }
            else if(obj.type==="tool_result"){ setToolEvents(prev=>[...prev,{tool:obj.tool, stage:obj.stage, ok:obj.ok, summary:obj.summary}]); }
            else if(obj.type==="token"){ full+=obj.text; setMessages(prev=> prev.map((mm,i)=> i===assistantIdx ? {...mm, content: full} : mm)); }
            else if(obj.type==="done"){ full=obj.reply; setMessages(prev=> prev.map((mm,i)=> i===assistantIdx ? {...mm, content: full} : mm)); setRunMeta(r=> r? {...r, elapsed_ms: obj.elapsed_ms} : {run_id:"",elapsed_ms:obj.elapsed_ms}); refreshHistory(); }
            else if(obj.type==="error"){ setMessages(prev=> prev.map((mm,i)=> i===assistantIdx ? {...mm, content:`⚠️ ${obj.error}`} : mm)); }
          }catch{}
        }
      }
    }catch(e:any){
      setMessages(prev=> prev.map((mm,i)=> i===assistantIdx ? {...mm, content: mm.content || `⚠️ ${e.message||"Failed to run"}`} : mm));
    }finally{ setIsStreaming(false); inputRef.current?.focus(); }
  },[input,isStreaming,messages,agent,agentId,backendUrl,modelOverride,maxTokens,refreshHistory]);

  if(loading) return (
    <div className="min-h-[60vh] grid place-items-center">
      <div className="flex flex-col items-center gap-3"><div className="h-10 w-10 rounded-full border-2 border-white/10 border-t-cyan-400 animate-spin"/><span className="text-xs font-mono tracking-widest text-white/50">LOADING NODE · {agentId}</span></div>
    </div>
  );
  if(error||!agent) return (
    <div className="min-h-[60vh] grid place-items-center text-center px-6">
      <div className="rounded-[20px] border border-white/10 bg-white/[0.04] p-8 max-w-md w-full"><div className="text-3xl mb-3">◈</div><h1 className="text-lg font-bold text-white">Agent not found</h1><p className="text-sm text-white/55 mt-2 break-words">{error}</p><button onClick={()=>router.push("/hub")} className="mt-6 px-5 py-2.5 rounded-full bg-white text-black font-bold text-sm">← Back to Hub</button></div>
    </div>
  );

  return (
    <div className="pb-10 relative">
      {/* OPAQUE INK — covers global CinematicBackground completely. z-0 sits above fixed video (-5) */}
      <div aria-hidden className="fixed inset-0" style={{ zIndex:0, background:`radial-gradient(1000px 620px at 16% 0%, ${agent.division_color}16, transparent 58%), radial-gradient(900px 520px at 94% 96%, ${agent.division_color}12, transparent 62%), linear-gradient(180deg, #070a14 0%, #070a14 34%, #05070e 100%)` }} />
      <div aria-hidden className="pointer-events-none fixed inset-0 opacity-[0.03]" style={{ zIndex:0, backgroundImage:`linear-gradient(rgba(255,255,255,.9) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.9) 1px, transparent 1px)`, backgroundSize:"48px 48px", maskImage:"radial-gradient(80% 60% at 50% 0%, black 46%, transparent 82%)" }} />

      <div className="relative" style={{ zIndex:1 }}>
      {/* top rail */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <button onClick={()=>router.push("/hub")} className="inline-flex items-center gap-2 text-xs font-mono tracking-widest text-white/60 hover:text-white transition-colors">
          <span className="grid h-7 w-7 place-items-center rounded-full bg-white/10 border border-white/10">←</span> NEXUS HUB
        </button>
        <div className="flex items-center gap-2">
          <span className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-white/5 border border-white/10 text-xs font-mono text-white/70">
            <span className={`h-1.5 w-1.5 rounded-full ${isStreaming ? "bg-amber-400 animate-pulse" : "bg-emerald-400 animate-pulse"}`} /> {isStreaming ? "RUNNING" : "LIVE"} · /api/agency/agents/{agent.id}/run-stream
          </span>
          <span className="hidden sm:inline-flex px-2.5 py-1 rounded-full text-[10px] font-mono border text-white/60" style={{borderColor:agent.division_color, background:`${agent.division_color}14`}}>{agent.division.toUpperCase()}</span>
        </div>
      </div>

      {/* HERO — real 3D canvas stage */}
      <section className="relative overflow-hidden rounded-[24px] border border-white/10" style={{ background:"linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.015) 38%, rgba(0,0,0,.18))", boxShadow:"0 24px 80px -22px rgba(0,0,0,.75), 0 0 0 1px rgba(255,255,255,.04) inset" }}>
        <div className="grid lg:grid-cols-[1.42fr_0.92fr] gap-5 p-5 sm:p-7">
          <div className="min-w-0">
            <div className="inline-flex items-center gap-2 px-2.5 py-1 rounded-full border bg-white/5 text-[11px] font-mono tracking-widest text-white/70" style={{borderColor:`${agent.division_color}35`}}>
              <span className="h-1.5 w-1.5 rounded-full" style={{background:agent.division_color}}/> {agent.division_label.toUpperCase()} · {agent.division_icon}
            </div>
            <h1 className="mt-3 text-[28px] sm:text-[31px] font-black tracking-tight leading-none text-white">{agent.name}</h1>
            <p className="text-sm text-white/60 mt-2 max-w-[56ch] leading-relaxed">{agent.description}</p>
            {agent.vibe && <p className="text-xs italic text-cyan-200/70 mt-1.5">“{agent.vibe}”</p>}
            {/* 3D canvas */}
            <div className="mt-5 relative rounded-[18px] overflow-hidden border border-white/10 bg-black/35" style={{height:176}}>
              <SceneCanvas color={agent.division_color} emoji={agent.emoji} />
              <div className="pointer-events-none absolute inset-0 p-4 flex items-start justify-between">
                <div className="rounded-xl border border-white/15 bg-white/10 backdrop-blur px-3 py-2.5 shadow-[0_12px_30px_rgba(0,0,0,.42)]">
                  <div className="text-[9px] font-mono tracking-widest text-white/60">PIPELINE</div>
                  <div className="mt-1.5 text-[11px] font-mono text-white/80 leading-tight max-w-[150px]">{agent.pipeline?.slice(0,2).join(" → ")}</div>
                  <div className="text-[9px] font-mono text-emerald-300 mt-2">● STREAMING READY</div>
                </div>
                <div className="hidden sm:flex flex-col items-end gap-1 text-right">
                  <span className="text-[10px] font-mono tracking-widest text-white/40">ACTIVE PROVIDER</span>
                  <span className="text-sm font-bold text-white">{llmHealth?.active ?? "xkiro"}</span>
                  <span className="text-[11px] font-mono text-white/50">{llmHealth ? `${llmHealth.live_count}/${llmHealth.total_count} online` : "checking…"}</span>
                  {runMeta?.run_id && <span className="text-[10px] font-mono text-white/35">run {runMeta.run_id}{runMeta.elapsed_ms ? ` · ${runMeta.elapsed_ms}ms` : ""}</span>}
                </div>
              </div>
              {/* bottom pipeline dots */}
              <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-10 flex items-center justify-center gap-1.5 px-4">
                {(agent.pipeline||[]).map((_,i)=>(
                  <span key={i} className="h-1.5 rounded-full transition-all" style={{ width: stages[i]?.status==="done" ? 18 : stages[i]?.status==="running" ? 14 : 8, background: stages[i]?.status==="done" ? "#10b981" : stages[i]?.status==="running" ? agent.division_color : "rgba(255,255,255,0.18)" }} />
                ))}
              </div>
            </div>
          </div>
          <div className="flex flex-col gap-4">
            <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
              <div className="text-[11px] font-mono tracking-widest text-white/45">QUICK START</div>
              <div className="mt-3 flex flex-wrap gap-2">
                {[
                  `Act as ${agent.name} — give me your best opening framework`,
                  `Show me a checklist you'd use for this domain`,
                  `Critique my idea as ${agent.name}`,
                ].map(p=>(
                  <button key={p} onClick={()=>{ setInput(p); setTimeout(()=>inputRef.current?.focus(),0); }} className="text-left text-xs leading-relaxed px-3 py-2 rounded-full bg-white/5 border border-white/10 text-white/75 hover:bg-white/10 hover:text-white transition-colors">{p}</button>
                ))}
              </div>
            </div>
            <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
              <div className="flex items-center justify-between"><span className="text-[11px] font-mono tracking-widest text-white/45">SYSTEM PROMPT</span><button onClick={()=>{ navigator.clipboard.writeText(agent.body).then(()=>{ setCopyOk(true); setTimeout(()=>setCopyOk(false),1400); }); }} className="text-[11px] font-mono px-2.5 py-1 rounded-full bg-white text-black font-bold hover:bg-white/90">{copyOk?"✓ Copied":"Copy"}</button></div>
              <p className="text-xs text-white/55 mt-2 leading-relaxed">Full <code className="px-1 py-0.5 rounded bg-white/10 border border-white/10 text-white/70">.md</code> streams as system prompt — no mock.</p>
              <details className="mt-3"><summary className="cursor-pointer text-xs font-semibold text-cyan-300 hover:text-cyan-200">Preview first 4k</summary><pre className="mt-2 max-h-[220px] overflow-auto whitespace-pre-wrap text-[11px] leading-relaxed p-3 rounded-xl bg-black/40 border border-white/10 text-white/75">{agent.body.slice(0,4000)}</pre></details>
            </div>
          </div>
        </div>
      </section>

      {/* TABS */}
      <div className="mt-6 flex flex-wrap items-center gap-2">
        {[
          ["workspace","Workspace"],
          ["history","History"],
          ["knowledge","Knowledge"],
          ["settings","Settings"],
        ].map(([k,l])=>(
          <button key={k} onClick={()=>setActiveTab(k as any)} className={`px-4 py-2 rounded-full text-xs font-bold border transition-colors ${activeTab===k ? "bg-white text-black border-white" : "bg-white/5 text-white/70 border-white/10 hover:bg-white/10 hover:text-white"}`}>{l}{k==="history" && history.length ? ` · ${history.length}` : ""}</button>
        ))}
        <Link href="/agency/llm" className="ml-auto text-xs font-mono text-cyan-300 hover:text-cyan-200">LLM Dashboard →</Link>
      </div>

      {/* TAB CONTENT */}
      <div className="mt-4">
        {activeTab==="workspace" && (
          <div className="grid grid-cols-1 lg:grid-cols-[1.68fr_0.92fr] gap-5">
            {/* Chat */}
            <section className="rounded-[20px] border border-white/10 bg-white/[0.04] backdrop-blur-xl overflow-hidden flex flex-col min-h-[540px]" style={{boxShadow:"0 18px 50px rgba(0,0,0,.35)"}}>
              <header className="flex items-center justify-between gap-3 px-4 py-3 border-b border-white/10">
                <div className="flex items-center gap-3 min-w-0">
                  <Link href="/hub" className="h-8 w-8 grid place-items-center rounded-full bg-white/10 border border-white/10 text-white hover:bg-white/15">←</Link>
                  <div className="h-8 w-8 rounded-full grid place-items-center text-[16px] border" style={{background:`${agent.division_color}18`, borderColor:`${agent.division_color}25`}}>{agent.emoji||"◍"}</div>
                  <div className="min-w-0"><div className="text-sm font-bold text-white leading-none truncate">{agent.name}</div><div className="text-[11px] font-mono text-white/50 truncate">{agent.division_label} · <span className="text-emerald-300">{isStreaming?"Streaming…":"LLM Ready"}</span></div></div>
                </div>
                <span className={`hidden sm:inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-[10px] font-mono ${isStreaming?"bg-amber-500/10 border-amber-500/30 text-amber-200":"bg-emerald-500/10 border-emerald-500/20 text-emerald-200"}`}>
                  <span className={`h-1.5 w-1.5 rounded-full ${isStreaming?"bg-amber-400 animate-pulse":"bg-emerald-400 animate-pulse"}`}/> {isStreaming?"STREAMING":"ONLINE"}
                </span>
              </header>
              <div className="flex-1 overflow-auto p-4 space-y-3" style={{background:"linear-gradient(180deg, transparent, rgba(0,0,0,.10))"}}>
                {messages.map((m,i)=>(
                  <div key={i} className={`flex ${m.role==="user"?"justify-end":"justify-start"}`}>
                    <div className={`max-w-[86%] rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap shadow-sm ${m.role==="user"?"text-black":"bg-white/10 text-white border border-white/10"}`} style={m.role==="user"?{background:agent.division_color}:undefined}>
                      {m.content || (isStreaming && i===messages.length-1 ? "▌" : "")}
                    </div>
                  </div>
                ))}
                {isStreaming && stages.length>0 && (
                  <div className="rounded-xl border border-white/10 bg-black/30 p-3 text-xs">
                    <div className="text-[11px] font-mono tracking-widest text-white/40">LIVE PIPELINE</div>
                    <div className="mt-2 space-y-1.5">
                      {stages.map(s=>(
                        <div key={s.stage} className="flex items-center gap-2">
                          <span className="h-1.5 w-1.5 rounded-full" style={{background: s.status==="done"?"#10b981": s.status==="running"?agent.division_color : "rgba(255,255,255,0.18)"}}/>
                          <span className={`text-xs ${s.status==="running"?"text-white font-semibold":"text-white/60"}`}>{s.stage}</span>
                          <span className="ml-auto text-[10px] font-mono text-white/35">{s.status}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                <div ref={endRef}/>
              </div>
              <form onSubmit={handleSend} className="p-3 border-t border-white/10 flex gap-2 bg-black/20">
                <input ref={inputRef} value={input} onChange={e=>setInput(e.target.value)} onKeyDown={e=>{ if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault(); handleSend(); } }} placeholder={`Message ${agent.name}…  (Shift+Enter for new line)`} className="flex-1 rounded-full bg-black/40 border border-white/10 px-4 py-3 text-sm text-white placeholder-white/40 focus:outline-none focus:border-white/20 focus:bg-black/60" />
                <button disabled={!input.trim()||isStreaming} className="h-11 w-11 grid place-items-center rounded-full text-black disabled:opacity-40 hover:brightness-110 transition" style={{background:agent.division_color}}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>
                </button>
              </form>
            </section>

            {/* Live run panel */}
            <section className="rounded-[20px] border border-white/10 bg-white/[0.04] backdrop-blur-xl overflow-hidden flex flex-col">
              <div className="px-4 py-3 border-b border-white/10"><div className="text-[11px] font-mono tracking-widest text-white/40">LIVE RUN</div></div>
              <div className="p-4 space-y-4 text-xs leading-relaxed flex-1 overflow-auto">
                <div>
                  <div className="text-[11px] font-mono tracking-widest text-white/35">PIPELINE</div>
                  <div className="mt-2 space-y-1.5">
                    {(agent.pipeline||[]).map((p,i)=>{
                      const st=stages.find(s=>s.stage===p);
                      const status=st?.status || "pending";
                      return (
                        <div key={p} className={`flex items-center gap-2 px-2.5 py-2 rounded-xl border ${status==="running"?"bg-white/10 border-white/15 text-white": status==="done"?"bg-emerald-500/10 border-emerald-500/20 text-emerald-100":"bg-black/20 border-white/5 text-white/45"}`}>
                          <span className="h-2 w-2 rounded-full" style={{background: status==="done"?"#10b981": status==="running"?agent.division_color:"rgba(255,255,255,0.2)"}}/>
                          <span className="text-xs font-medium">{i+1}. {p}</span>
                          <span className="ml-auto text-[10px] font-mono">{status}</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
                <div className="h-px bg-white/10"/>
                <div>
                  <div className="text-[11px] font-mono tracking-widest text-white/35">TOOL ACTIVITY</div>
                  <div className="mt-2 space-y-1.5 max-h-[180px] overflow-auto">
                    {toolEvents.length===0 ? <span className="text-white/35">No tool calls yet — start a message to see real execution.</span> :
                      toolEvents.map((te,idx)=>(
                        <div key={idx} className="px-2.5 py-1.5 rounded-xl bg-black/30 border border-white/10 text-[11px] font-mono text-white/70">
                          <span className="text-white font-semibold">{te.tool}</span> <span className="text-white/40">· {te.stage}</span>{te.summary ? <span className="text-emerald-300"> · {te.summary}</span>:null}
                        </div>
                      ))}
                  </div>
                </div>
                <div className="h-px bg-white/10"/>
                <div className="rounded-xl bg-black/30 border border-white/10 p-3 font-mono text-[11px] leading-relaxed text-white/70">
                  <div><span className="text-emerald-300">● real microservice</span> — SSE stream.</div>
                  <div className="mt-1.5 break-all">POST <span className="text-white">/api/agency/agents/{agent.id}/run-stream</span></div>
                  <div className="text-white/45">→ llm_router.llm_chat · failover xkiro → anyModel → Selora → …</div>
                  {runMeta?.elapsed_ms ? <div className="mt-1 text-white/60">last run {runMeta.elapsed_ms}ms · id {runMeta.run_id}</div>:null}
                </div>
                <div className="h-px bg-white/10"/>
                <div>
                  <div className="text-[11px] font-mono tracking-widest text-white/35">LLM / PROVIDERS</div>
                  {llmHealth ? (
                    <div className="mt-2 space-y-2">
                      <div className="flex flex-wrap gap-1.5">{llmHealth.providers.slice(0,9).map((p:any)=>(<span key={p.name} className={`px-2 py-1 rounded-full border text-[10px] font-mono ${p.healthy?"border-emerald-500/30 text-emerald-200 bg-emerald-500/10":"border-red-500/30 text-red-200 bg-red-500/10"}`}>{p.name} {p.healthy?"✓":"✕"}</span>))}</div>
                      <div className="text-[11px] font-mono text-white/50">Active: <span className="text-white font-bold">{llmHealth.active??"—"}</span> · {llmHealth.live_count}/{llmHealth.total_count}</div>
                    </div>
                  ): <span className="text-white/40">Loading…</span>}
                </div>
              </div>
            </section>
          </div>
        )}
        {activeTab==="history" && (
          <section className="rounded-[20px] border border-white/10 bg-white/[0.04] backdrop-blur-xl overflow-hidden">
            <div className="px-5 py-4 border-b border-white/10 flex items-center justify-between gap-3">
              <div><div className="text-sm font-bold text-white">Persisted History</div><div className="text-xs text-white/50">Survives refresh — stored at <code className="px-1 py-0.5 rounded bg-white/10 border border-white/10">backend/agency_history.json</code> per agent (capped 100)</div></div>
              <div className="flex items-center gap-2">
                <button onClick={refreshHistory} className="px-3 py-1.5 rounded-full bg-white/10 border border-white/10 text-xs font-semibold text-white hover:bg-white/15">Refresh</button>
                <button onClick={clearHistory} className="px-3 py-1.5 rounded-full bg-red-500/10 border border-red-500/20 text-xs font-semibold text-red-200 hover:bg-red-500/15">Clear</button>
              </div>
            </div>
            <div className="p-4 space-y-2 max-h-[560px] overflow-auto">
              {history.length===0 ? <div className="text-sm text-white/40 py-12 text-center">No history yet — send a message in Workspace to create a real run.</div> :
                history.map((m:any,idx:number)=>(
                  <div key={m.id||idx} className={`flex ${m.role==="user"?"justify-end":"justify-start"}`}>
                    <div className={`max-w-[86%] rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap border ${m.role==="user"?"text-black border-transparent":"bg-white/5 text-white border-white/10"}`} style={m.role==="user"?{background:agent.division_color}:undefined}>
                      <div>{m.content}</div>
                      {m.ts && <div className="text-[10px] font-mono opacity-40 mt-1">{new Date(m.ts).toLocaleString()}</div>}
                    </div>
                  </div>
                ))}
            </div>
          </section>
        )}
        {activeTab==="knowledge" && (
          <section className="rounded-[20px] border border-white/10 bg-white/[0.04] backdrop-blur-xl overflow-hidden">
            <div className="px-5 py-4 border-b border-white/10"><div className="text-sm font-bold text-white">Knowledge — System Prompt</div><div className="text-xs text-white/50">The exact <code className="px-1 py-0.5 rounded bg-white/10 border border-white/10">.md</code> sent every turn. Division: <span className="text-white font-semibold">{agent.division_label}</span> · tools: <span className="text-white">{(agent.tools||[]).join(", ")}</span></div></div>
            <div className="p-5">
              <div className="flex items-center justify-between gap-3 mb-3">
                <span className="text-xs font-mono text-white/50">{agent.body.length.toLocaleString()} chars · {agent.body.slice(0,4000).length} preview</span>
                <button onClick={()=>{ navigator.clipboard.writeText(agent.body).then(()=>{ setCopyOk(true); setTimeout(()=>setCopyOk(false),1400); }); }} className="text-xs font-mono px-3 py-1.5 rounded-full bg-white text-black font-bold hover:bg-white/90">{copyOk?"✓ Copied":"Copy full prompt"}</button>
              </div>
              <pre className="max-h-[560px] overflow-auto whitespace-pre-wrap text-xs leading-relaxed p-4 rounded-xl bg-black/40 border border-white/10 text-white/75">{agent.body}</pre>
            </div>
          </section>
        )}
        {activeTab==="settings" && (
          <section className="rounded-[20px] border border-white/10 bg-white/[0.04] backdrop-blur-xl overflow-hidden">
            <div className="px-5 py-4 border-b border-white/10"><div className="text-sm font-bold text-white">Settings</div><div className="text-xs text-white/50">Per-run overrides — no env keys needed. Backend still uses the 9-provider failover chain.</div></div>
            <div className="p-5 space-y-5 max-w-2xl">
              <label className="block"><div className="text-xs font-mono tracking-widest text-white/40 mb-2">MODEL OVERRIDE (optional)</div><input value={modelOverride} onChange={e=>setModelOverride(e.target.value)} placeholder="leave empty for failover chain (e.g. qwen/qwen3.7-flash:free)" className="w-full rounded-xl bg-black/40 border border-white/10 px-3.5 py-2.5 text-sm text-white placeholder-white/30 focus:outline-none focus:border-white/20"/><div className="text-[11px] text-white/40 mt-1.5">Passed as <code className="px-1 py-0.5 rounded bg-white/10 border border-white/10">model</code> to <code className="px-1 py-0.5 rounded bg-white/10 border border-white/10">llm_router.llm_chat</code>. Empty = auto failover.</div></label>
              <label className="block"><div className="text-xs font-mono tracking-widest text-white/40 mb-2">MAX TOKENS · {maxTokens}</div><input type="range" min={512} max={8192} step={256} value={maxTokens} onChange={e=>setMaxTokens(parseInt(e.target.value))} className="w-full accent-white"/><div className="flex justify-between text-[11px] font-mono text-white/35"><span>512</span><span>8192</span></div></label>
              <div className="rounded-xl bg-black/30 border border-white/10 p-4">
                <div className="text-xs font-mono tracking-widest text-white/35">ABOUT THIS AGENT</div><div className="text-sm text-white/70 mt-2 leading-relaxed">{agent.description}</div>{agent.vibe && <div className="text-xs italic text-cyan-200/60 mt-2">“{agent.vibe}”</div>}
                <div className="text-[11px] font-mono text-white/35 mt-4">PIPELINE</div><div className="text-xs text-white/60 mt-1">{(agent.pipeline||[]).join(" → ")}</div>
                <div className="text-[11px] font-mono text-white/35 mt-3">TOOLS FOR THIS DIVISION</div><div className="text-xs text-white/60 mt-1">{(agent.tools||[]).join(" · ")}</div>
              </div>
              <div className="rounded-xl bg-black/30 border border-white/10 p-4">
                <div className="text-xs font-mono tracking-widest text-white/35">LLM HEALTH</div>
                {llmHealth ? (
                  <div className="mt-3 flex flex-wrap gap-1.5">{llmHealth.providers.map((p:any)=>(<span key={p.name} className={`px-2 py-1 rounded-full border text-[10px] font-mono ${p.healthy?"border-emerald-500/30 text-emerald-200 bg-emerald-500/10":"border-red-500/30 text-red-200 bg-red-500/10"}`}>{p.name} {p.healthy?"✓":"✕"}</span>))}</div>
                ): <span className="text-white/40 text-xs">Loading…</span>}
                <div className="text-xs text-white/50 mt-3">Active: <span className="text-white font-bold">{llmHealth?.active??"—"}</span> · <Link href="/agency/llm" className="text-cyan-300 hover:text-cyan-200 underline decoration-cyan-400/30">Open LLM Dashboard →</Link></div>
              </div>
            </div>
          </section>
        )}
      </div>
      </div>
    </div>
  );
}
