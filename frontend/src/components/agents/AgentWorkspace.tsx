"use client";

import React from "react";
import { PremiumGlassPanel } from "@/components/ui/PremiumGlassPanel";
import { PageTransition } from "@/components/ui/PageTransition";
import { MagneticButton } from "@/components/ui/MagneticButton";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

interface AgentWorkspaceProps {
  agentName: string;
  category: string;
  status: "Online" | "Executing" | "Offline" | "Requires Input";
  children: React.ReactNode;
  sidebar?: React.ReactNode;
}

export const AgentWorkspace = ({ agentName, category, status, children, sidebar }: AgentWorkspaceProps) => {
  return (
    <PageTransition>
      <div className="flex h-[calc(100vh-64px)] w-full gap-6 p-6">
        
        {/* Main Workspace Area */}
        <div className="flex-1 flex flex-col min-w-0 h-full gap-6">
          
          <header className="flex-none flex items-center justify-between">
            <div className="flex items-center gap-4">
              <Link href="/hub" className="text-white/40 hover:text-white transition-colors">
                <ArrowLeft size={16} />
              </Link>
              <div>
                <h1 className="text-2xl text-editorial text-white leading-none mb-1">{agentName}</h1>
                <span className="font-jetbrains text-[9px] uppercase tracking-widest text-white/40">
                  {category} Workspace
                </span>
              </div>
            </div>

            <div className="flex items-center gap-4">
              <div className="flex items-center gap-2 px-3 py-1.5 rounded-full border border-white/10 bg-white/5">
                <div className={`w-1.5 h-1.5 rounded-full shadow-[0_0_8px_currentColor] ${
                  status === "Online" ? "bg-emerald-400 text-emerald-400" :
                  status === "Executing" ? "bg-blue-400 text-blue-400 animate-pulse" :
                  status === "Requires Input" ? "bg-amber-400 text-amber-400" :
                  "bg-red-400 text-red-400"
                }`} />
                <span className="font-jetbrains text-[10px] uppercase tracking-widest text-white/60">
                  {status}
                </span>
              </div>
              <MagneticButton intensity={0.1} className="px-4 py-2 bg-white text-black text-xs font-semibold rounded-full hover:bg-white/90">
                Execute
              </MagneticButton>
            </div>
          </header>

          <PremiumGlassPanel level={1} className="flex-1 overflow-hidden flex flex-col p-0">
            {children}
          </PremiumGlassPanel>
        </div>

        {/* Sidebar Area (Optional) */}
        {sidebar && (
          <div className="w-[320px] flex-none h-full flex flex-col gap-6">
            {sidebar}
          </div>
        )}
        
      </div>
    </PageTransition>
  );
};
