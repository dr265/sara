"use client";

import React from "react";
import { motion, HTMLMotionProps } from "framer-motion";
import { cn } from "@/lib/utils";

interface PremiumGlassPanelProps extends HTMLMotionProps<"div"> {
  level?: 1 | 2 | 3 | 4;
  interactive?: boolean;
  intensity?: "light" | "medium" | "heavy";
  children?: React.ReactNode;
}

export const PremiumGlassPanel = React.forwardRef<HTMLDivElement, PremiumGlassPanelProps>(
  ({ className, level = 1, interactive = false, intensity = "medium", children, ...props }, ref) => {
    
    const baseClasses = "relative overflow-hidden rounded-[20px] border border-white/[0.04]";
    
    const intensityStyles = {
      light: "backdrop-blur-md bg-white/[0.01]",
      medium: "backdrop-blur-xl bg-white/[0.03]",
      heavy: "backdrop-blur-2xl bg-white/[0.05]",
    };

    const levelStyles = {
      1: "shadow-[0_4px_24px_-4px_rgba(0,0,0,0.5)]",
      2: "shadow-[0_8px_32px_-8px_rgba(0,0,0,0.6)] border-white/[0.06]",
      3: "shadow-[0_16px_48px_-12px_rgba(0,0,0,0.7)] border-white/[0.08]",
      4: "shadow-[0_24px_64px_-16px_rgba(0,0,0,0.8)] border-white/[0.12]",
    };

    const interactiveStyles = interactive 
      ? "hover:border-white/[0.15] hover:bg-white/[0.04] transition-colors duration-500 cursor-pointer" 
      : "";

    return (
      <motion.div
        ref={ref}
        className={cn(
          baseClasses,
          intensityStyles[intensity],
          levelStyles[level],
          interactiveStyles,
          className
        )}
        initial={{ opacity: 0, y: 15 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.8, ease: [0.16, 1, 0.3, 1] }}
        {...props}
      >
        {/* Subtle noise texture overlay */}
        <div className="pointer-events-none absolute inset-0 opacity-[0.15] mix-blend-overlay" 
             style={{ backgroundImage: 'url("data:image/svg+xml,%3Csvg viewBox=%220 0 200 200%22 xmlns=%22http://www.w3.org/2000/svg%22%3E%3Cfilter id=%22noiseFilter%22%3E%3CfeTurbulence type=%22fractalNoise%22 baseFrequency=%220.65%22 numOctaves=%223%22 stitchTiles=%22stitch%22/%3E%3C/filter%3E%3Crect width=%22100%25%22 height=%22100%25%22 filter=%22url(%23noiseFilter)%22/%3E%3C/svg%3E")' }}>
        </div>
        
        {/* Top subtle highlight */}
        <div className="pointer-events-none absolute inset-0 rounded-[20px] ring-1 ring-inset ring-white/[0.05]" />
        <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-white/[0.2] to-transparent" />
        
        <div className="relative z-10 w-full h-full">
          {children}
        </div>
      </motion.div>
    );
  }
);

PremiumGlassPanel.displayName = "PremiumGlassPanel";
