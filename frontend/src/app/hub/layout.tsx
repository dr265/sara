import React from 'react';
import Link from 'next/link';
import "@/styles/sara.css"; // shared SARA design tokens + utilities (hub opt-in)
import "../globals.css"; // ✅ Yeh bilkul sahi hai

export default function HubLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-[#07090e] text-slate-100 flex flex-col selection:bg-cyan-500/30">
      {/* Top Futuristic Navigation Bar */}
      <header className="sticky top-0 z-50 border-b border-white/10 bg-[#07090e]/70 backdrop-blur-2xl px-4 sm:px-6 py-3.5 flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3 sm:gap-4">
          <Link 
            href="/workspace" 
            className="hub-focus-ring text-xs font-mono uppercase tracking-widest text-cyan-400 hover:text-cyan-300 transition-colors flex items-center gap-1.5"
          >
            ← Workspace
          </Link>
          <span className="text-slate-600">/</span>
          <span className="truncate text-sm font-semibold tracking-wide text-slate-100">
            Nexus Agent Hub
          </span>
          <span className="hidden sm:inline-flex px-2 py-0.5 rounded-full text-[10px] font-mono bg-white/5 text-cyan-300 border border-white/10">
            200+ Nodes Ready
          </span>
        </div>

        <div className="flex w-full items-center justify-end gap-2 sm:w-auto sm:gap-3">
          <Link
            href="/workspace"
            className="hub-focus-ring hidden sm:inline-flex px-3 py-1.5 rounded-lg text-xs font-medium text-slate-400 hover:text-white hover:bg-slate-800/60 border border-transparent hover:border-slate-700 transition-all"
          >
            Desktop Agent (SARA)
          </Link>
          <div className="hidden sm:block h-4 w-px bg-slate-800" />
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-xs font-mono text-cyan-300">
            <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
            Gateway Online
          </div>
        </div>
      </header>

      {/* Main Hub Viewport */}
      <main className="flex-1 w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
        {children}
      </main>
    </div>
  );
}