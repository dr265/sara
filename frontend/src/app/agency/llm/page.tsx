"use client";
import React, { useEffect, useState } from "react";
import { saraFetch } from "@/lib/api";

export default function LlmDashboardPage() {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState("");
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";
  useEffect(()=>{ (async()=>{
    try { const r=await saraFetch(`${backendUrl}/api/agency/llm/health`, { cache:"no-store" as RequestCache}); if(!r.ok) throw new Error(String(r.status)); setData(await r.json()); }
    catch(e:any){ setErr(e.message||"failed"); }
  })(); }, [backendUrl]);
  if (err) return <div className="p-6 text-red-300">LLM health error: {err}</div>;
  if (!data) return <div className="p-6 text-slate-400">Loading LLM health…</div>;
  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-black text-white">LLM Management</h1>
        <p className="text-sm text-slate-400 mt-1">Unified failover chain · every agency agent uses <code className="px-1 py-0.5 rounded bg-white/10 border border-white/10">llm_router.llm_chat</code>. No per-agent keys.</p>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-5">
          <div className="text-xs font-mono tracking-widest text-slate-400">STATUS</div>
          <div className={`mt-2 inline-flex px-3 py-1 rounded-full text-sm font-bold border ${data.any_available ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/30" : "bg-red-500/15 text-red-300 border-red-500/30"}`}>{data.any_available ? "● Available" : "○ Offline — local fallback"}</div>
          <div className="mt-3 text-xs text-slate-400">Active provider: <span className="text-white font-semibold">{data.active ?? "—"}</span></div>
          <div className="text-xs text-slate-400">{data.live_count}/{data.total_count} providers healthy</div>
        </div>
        <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-5 lg:col-span-2">
          <div className="text-xs font-mono tracking-widest text-slate-400">HOW IT WORKS</div>
          <ul className="mt-2 text-xs leading-relaxed text-slate-300 list-disc pl-4 space-y-1">
            <li>All agents share one LLM brain. The orchestrator examines capability + description keywords and routes.</li>
            <li>Every turn sends the agent&apos;s markdown personality as the system prompt — no mock, real execution.</li>
            <li>Provider order: xkiro → hashneuron → anymodel → selora → apinex → cleanapis → codecraft → deepseek/openai/… with automatic 429/5xx retry and 402 quota quarantine.</li>
            <li>Keys live only in <code className="px-1 py-0.5 rounded bg-white/10">backend/.env</code>; the dashboard never exposes secrets — only health + last_error.</li>
          </ul>
        </div>
      </div>
      <div className="rounded-2xl border border-white/10 bg-white/[0.04] overflow-hidden">
        <div className="px-5 py-3 border-b border-white/10 flex items-center justify-between">
          <span className="text-xs font-mono tracking-widest text-slate-400">PROVIDERS</span>
          <span className="text-[11px] font-mono text-slate-500">{data.providers?.length ?? 0} configured</span>
        </div>
        <div className="divide-y divide-white/5">
          {data.providers?.map((p:any)=>(
            <div key={p.name} className="px-5 py-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className={`h-2 w-2 rounded-full ${p.healthy ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.7)]" : "bg-red-400"}`} />
                  <span className="font-bold text-white text-sm">{p.name}</span>
                  <span className={`px-2 py-0.5 rounded-full border text-[10px] font-mono ${p.healthy ? "border-emerald-500/30 text-emerald-300 bg-emerald-500/10" : "border-red-500/30 text-red-300 bg-red-500/10"}`}>{p.healthy ? "healthy" : "unavailable"}</span>
                  {p.out_of_credits && <span className="px-2 py-0.5 rounded-full border border-amber-500/30 text-amber-300 bg-amber-500/10 text-[10px] font-mono">out of credits</span>}
                  {p.retry_in_s ? <span className="text-[11px] font-mono text-slate-500">retry in {p.retry_in_s}s</span> : null}
                </div>
                <div className="text-xs text-slate-400 mt-1 truncate">{p.base_url}</div>
                <div className="text-xs text-slate-500">model: <span className="text-slate-300 font-mono">{p.configured_model ?? "—"}</span>{p.fallback_models?.length ? ` · fallbacks: ${p.fallback_models.slice(0,3).join(", ")}` : ""}</div>
                {p.last_error && <div className="text-[11px] text-red-300/80 mt-1 line-clamp-2">{p.last_error}</div>}
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-5">
        <div className="text-xs font-mono tracking-widest text-slate-400">WHAT TO CONFIGURE</div>
        <p className="text-xs text-slate-300 mt-2">All existing keys in <code className="px-1 py-0.5 rounded bg-white/10">backend/.env</code> are reused — no new credentials needed. Agency agents require <em>no</em> additional provider: they share the same chain. If a division needs a specialty model (e.g., vision), set its <code className="px-1 py-0.5 rounded bg-white/10">*_MODEL</code> env; the run endpoint also accepts a per-call <code className="px-1 py-0.5 rounded bg-white/10">model</code> override.</p>
        <p className="text-xs text-slate-500 mt-2">Test a provider: <code className="px-1 py-0.5 rounded bg-white/10">curl http://127.0.0.1:8000/api/agency/llm/health</code> · Test an agent: <code className="px-1 py-0.5 rounded bg-white/10">POST /api/agency/agents/engineering-backend-architect/run &#123;&quot;message&quot;:&quot;hello&quot;&#125;</code></p>
      </div>
    </div>
  );
}
