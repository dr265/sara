import React from 'react';
import "@/styles/sara.css";

export default function SaraSalesLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-[#0b0f14] text-slate-100 flex flex-col">
      {children}
    </div>
  );
}