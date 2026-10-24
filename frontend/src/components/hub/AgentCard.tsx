"use client";

import React from "react";
import Link from "next/link";
import { HubAgent } from "@/lib/agents.config";
import AgentIcon from "./AgentIcon";

// Deterministic mesh gradient based on string hash
export function getMeshGradient(name: string) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  const c1 = `hsl(${Math.abs(hash) % 360}, 80%, 60%)`;
  const c2 = `hsl(${Math.abs(hash * 2) % 360}, 70%, 40%)`;
  const c3 = `hsl(${Math.abs(hash * 3) % 360}, 90%, 20%)`;
  return `radial-gradient(circle at 0% 0%, ${c1} 0%, transparent 50%), radial-gradient(circle at 100% 100%, ${c2} 0%, transparent 50%), ${c3}`;
}

export default function AgentCard({ agent }: { agent: HubAgent }) {
  return (
    <>
      <style>{`
        .vertex-btn {
          display:grid;place-items:center;color:#fff;position:relative;overflow:hidden;
          background:linear-gradient(to top,
            #9ad9ec 1px, #89dff0 2px, #79e0f1 3px, #61daef 4px, #3ec8e4 5px,
            #14a8c6 6px, #0596b3 7px, #038aa8 8px, #047796 9px, #006180 10px,
            #025066 12px, #0a4f5e 13px, #04465a 14px, #073746 16px,
            #0a2a37 18px, #0d212e 20px, #0f1824 24px, #0a121e 30px,
            #0a111d 34px, #0a111d 100%);
          box-shadow:
            inset 0 3px 3px -2px rgba(180,228,255,.10),
            inset 1px 0 0 rgba(255,255,255,.09),
            inset -1px 0 0 rgba(255,255,255,.09),
            0 1px 0 rgba(152,218,234,.38),
            1px 0 0 rgba(152,218,234,.17), -1px 0 0 rgba(152,218,234,.17),
            0 0 8px rgba(60,190,235,.10),
            0 2px 5px -3px rgba(90,220,255,.45);
          transition:transform .25s,box-shadow .25s,filter .25s;
          border-radius:12px;
          height:42px;
          font-size:14px;
          font-weight:600;
          width:100%;
          text-decoration:none;
        }
        .vertex-btn::after {
          content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;z-index:1;
          background:linear-gradient(90deg,rgba(200,245,255,.70),rgba(200,245,255,0) 13px),
                     linear-gradient(270deg,rgba(200,245,255,.70),rgba(200,245,255,0) 13px);
          -webkit-mask:linear-gradient(to top,#000 0,#000 6px,rgba(0,0,0,.40) 12px,
            rgba(0,0,0,.15) 18px,rgba(0,0,0,.09) 24px,rgba(0,0,0,.02) 30px,rgba(0,0,0,.02) 100%);
                  mask:linear-gradient(to top,#000 0,#000 6px,rgba(0,0,0,.40) 12px,
            rgba(0,0,0,.15) 18px,rgba(0,0,0,.09) 24px,rgba(0,0,0,.02) 30px,rgba(0,0,0,.02) 100%);
        }
        .vertex-btn span {
          position:relative;z-index:2;display:block;line-height:1;
          text-shadow:0 1px 2px rgba(0,20,30,.5);
        }
        .vertex-btn:hover {
          transform:translateY(-1px);filter:brightness(1.12);
          box-shadow:inset 0 1px 0 rgba(200,245,255,.6),inset 1px 0 0 rgba(170,225,255,.35),
                     inset -1px 0 0 rgba(170,225,255,.35),0 6px 22px rgba(20,180,225,.55);
        }
      `}</style>
      
      <div className="flex flex-col gap-4 group rounded-2xl bg-[#111216]/50 p-2.5 border border-white/5 hover:border-white/10 transition-colors shadow-xl">
        {/* The Preview Image Window */}
        <div className="relative w-full aspect-[16/9] rounded-[14px] overflow-hidden shadow-inner bg-black">
           <div className="absolute inset-0 opacity-60 group-hover:opacity-100 transition-opacity duration-300" style={{ background: getMeshGradient(agent.name) }} />
           {/* Noise overlay for premium texture */}
           <div className="absolute inset-0 bg-[url('https://grainy-gradients.vercel.app/noise.svg')] opacity-20 mix-blend-overlay" />
           
           <div className="absolute inset-0 flex items-center justify-center text-white/40 group-hover:scale-110 transition-transform duration-500">
              <div className="scale-125 drop-shadow-2xl">
                 <AgentIcon id={agent.id} category={agent.category} />
              </div>
           </div>
           
           {agent.status === 'online' && (
             <div className="absolute top-2.5 right-2.5 px-2 py-1 bg-black/40 backdrop-blur-md rounded-full border border-white/10 flex items-center gap-1.5 shadow-lg z-10">
               <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]" />
               <span className="text-[9px] font-mono text-emerald-100 uppercase tracking-wider">Online</span>
             </div>
           )}
        </div>
        
        {/* Content Box */}
        <div className="flex flex-col px-1 pb-1 flex-1">
          <div className="flex flex-col gap-1 mb-2.5">
            <h3 className="text-white font-bold text-[16px] leading-tight group-hover:text-cyan-400 transition-colors">{agent.name}</h3>
            <span className="text-cyan-500 text-[11px] font-mono uppercase tracking-widest">{agent.category}</span>
          </div>

          <p className="text-slate-400 text-[13px] leading-relaxed mb-4 line-clamp-2 h-[38px]">
            {agent.shortDescription}
          </p>

          <div className="flex flex-wrap gap-1.5 mb-5 h-[26px] overflow-hidden">
            {agent.capabilities.slice(0, 2).map((cap) => (
              <span key={cap} className="px-2 py-0.5 rounded-md bg-white/5 border border-white/5 text-[10px] text-slate-300 whitespace-nowrap">
                {cap}
              </span>
            ))}
            {agent.capabilities.length > 2 && (
              <span className="px-2 py-0.5 rounded-md bg-white/5 border border-white/5 text-[10px] text-slate-400">
                +{agent.capabilities.length - 2}
              </span>
            )}
          </div>
          
          <div className="mt-auto pt-1">
            <Link href={agent.route} className="vertex-btn">
              <span>Launch Workspace</span>
            </Link>
          </div>
        </div>
      </div>
    </>
  );
}
