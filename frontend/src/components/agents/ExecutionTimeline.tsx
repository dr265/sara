"use client";

import React from "react";
import { PremiumGlassPanel } from "@/components/ui/PremiumGlassPanel";

interface TimelineEvent {
  id: string;
  title: string;
  description: string;
  time: string;
  status: "completed" | "in-progress" | "pending";
}

export const ExecutionTimeline = ({ events }: { events: TimelineEvent[] }) => {
  return (
    <PremiumGlassPanel level={2} className="h-full p-6 flex flex-col">
      <h3 className="text-[10px] font-jetbrains uppercase tracking-widest text-white/50 mb-8">Execution Timeline</h3>
      
      <div className="flex-1 overflow-y-auto sara-scroll relative pr-2">
        <div className="absolute left-2 top-2 bottom-2 w-px bg-gradient-to-b from-white/20 via-white/10 to-transparent" />
        
        <div className="flex flex-col gap-8">
          {events.map((event, i) => (
            <div key={event.id} className="relative pl-8">
              {/* Timeline Node */}
              <div className={`absolute left-[3px] top-1.5 w-3 h-3 rounded-full -translate-x-1/2 border-2 border-[#090e12] ${
                event.status === "completed" ? "bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.5)]" :
                event.status === "in-progress" ? "bg-blue-400 shadow-[0_0_10px_rgba(96,165,250,0.5)] animate-pulse" :
                "bg-white/20"
              }`} />
              
              <div className="flex flex-col">
                <div className="flex items-center justify-between mb-1">
                  <h4 className={`text-sm font-semibold ${event.status === "pending" ? "text-white/40" : "text-white/90"}`}>
                    {event.title}
                  </h4>
                  <span className="text-[9px] font-jetbrains tracking-widest text-white/30">{event.time}</span>
                </div>
                <p className="text-xs text-white/50 leading-relaxed">
                  {event.description}
                </p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </PremiumGlassPanel>
  );
};
