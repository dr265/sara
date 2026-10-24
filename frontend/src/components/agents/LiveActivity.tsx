"use client";

import React from "react";
import { motion, AnimatePresence } from "framer-motion";

interface LogEntry {
  id: string;
  timestamp: string;
  message: string;
  type: "info" | "success" | "warning" | "error";
}

export const LiveActivity = ({ logs }: { logs: LogEntry[] }) => {
  return (
    <div className="flex flex-col h-full bg-[#05080a] overflow-hidden rounded-[20px] p-6 relative">
      <div className="absolute inset-0 pointer-events-none opacity-[0.03]" style={{ backgroundImage: "url('/noise.png')" }} />
      
      <div className="flex items-center justify-between mb-6 border-b border-white/[0.05] pb-4">
        <h3 className="text-[10px] font-jetbrains uppercase tracking-widest text-white/50">Live Telemetry</h3>
        <div className="flex items-center gap-2">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-2 w-2 bg-blue-500"></span>
          </span>
          <span className="text-[9px] font-jetbrains uppercase tracking-widest text-blue-400">Stream Connected</span>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto sara-scroll flex flex-col justify-end">
        <div className="flex flex-col gap-2 min-h-full justify-end">
          <AnimatePresence initial={false}>
            {logs.map((log) => (
              <motion.div
                key={log.id}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                className="flex items-start gap-4 font-jetbrains text-xs"
              >
                <span className="text-white/30 shrink-0 mt-0.5">{log.timestamp}</span>
                <span className={`shrink-0 w-1.5 h-1.5 rounded-full mt-1.5 ${
                  log.type === "info" ? "bg-blue-400" :
                  log.type === "success" ? "bg-emerald-400" :
                  log.type === "warning" ? "bg-amber-400" : "bg-red-400"
                }`} />
                <span className={`text-white/70 leading-relaxed ${
                  log.type === "error" ? "text-red-400" : ""
                }`}>
                  {log.message}
                </span>
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
};
