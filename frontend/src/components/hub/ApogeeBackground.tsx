"use client";

import React from "react";

const BAR_HEIGHTS = [
  23, 40, 53, 40, 33, 14, 7, 17, 75, 65,
  88, 75, 65, 47, 33, 88, 4, 7, 9, 14,
  95, 65, 79, 37, 7, 40, 17, 20, 62, 47,
  92, 72,
];

export default function ApogeeBackground() {
  const maxHeight = Math.max(...BAR_HEIGHTS);

  return (
    <div className="fixed inset-0 z-0 pointer-events-none overflow-hidden bg-[#080A19]">
      <style>{`
        @keyframes bar-grow {
          from { transform: scaleY(0); opacity: 0; }
          to   { transform: scaleY(1); opacity: 1; }
        }
        .animate-bar-grow {
          animation: bar-grow 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
          opacity: 0;
        }
      `}</style>

      {/* Background Video */}
      <video
        className="absolute inset-0 w-full h-full object-cover opacity-60"
        src="https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260813_092641_de52eb87-daf2-41db-92cb-7a56eae012a5.mp4"
        autoPlay
        loop
        muted
        playsInline
      />

      {/* Floating Abstract Chart (from Apogee RevenueCard) */}
      <div className="absolute top-[20%] right-[10%] w-full max-w-[405px] opacity-40">
        <div className="w-full rounded-[24px] sm:rounded-[33px] bg-[rgba(17,16,15,0.35)] backdrop-blur-[20px] p-5 sm:p-8 pb-5 sm:pb-6 border border-white/5 shadow-2xl">
          
          <div className="relative mt-8">
            {/* Bars */}
            <div className="flex items-end gap-[1.5px] h-[80px] sm:h-[100px]">
              {BAR_HEIGHTS.map((h, i) => {
                const isProjected = i >= 28;
                const heightPercent = (h / maxHeight) * 100;
                return (
                  <div
                    key={i}
                    className="flex-1 rounded-[0.5px] animate-bar-grow origin-bottom"
                    style={{
                      height: `${heightPercent}%`,
                      backgroundColor: isProjected ? 'rgba(255,255,255,0.1)' : 'white',
                      animationDelay: `${1100 + i * 30}ms`,
                    }}
                  />
                );
              })}
            </div>

            {/* Vertical gridlines */}
            <div className="absolute inset-0 pointer-events-none">
              {[0, 1, 2, 3, 4].map((i) => (
                <div
                  key={i}
                  className="absolute top-0 bottom-0 w-px bg-white/10"
                  style={{ left: `${((i + 1) / 5) * 100}%` }}
                />
              ))}
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}
