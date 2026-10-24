"use client";

import React, { useRef, useState } from "react";
import { motion, HTMLMotionProps } from "framer-motion";
import { cn } from "@/lib/utils";

interface MagneticButtonProps extends HTMLMotionProps<"button"> {
  children: React.ReactNode;
  className?: string;
  intensity?: number;
}

export const MagneticButton = React.forwardRef<HTMLButtonElement, MagneticButtonProps>(
  ({ children, className, intensity = 0.5, ...props }, ref) => {
    const buttonRef = useRef<HTMLButtonElement | null>(null);
    const [position, setPosition] = useState({ x: 0, y: 0 });

    const handleMouse = (e: React.MouseEvent<HTMLButtonElement>) => {
      if (!buttonRef.current) return;
      const { clientX, clientY } = e;
      const { height, width, left, top } = buttonRef.current.getBoundingClientRect();
      const middleX = clientX - (left + width / 2);
      const middleY = clientY - (top + height / 2);
      
      setPosition({ x: middleX * intensity, y: middleY * intensity });
    };

    const reset = () => {
      setPosition({ x: 0, y: 0 });
    };

    return (
      <motion.button
        ref={(node) => {
          buttonRef.current = node;
          if (typeof ref === "function") {
            ref(node);
          } else if (ref) {
            ref.current = node;
          }
        }}
        className={cn(
          "relative overflow-hidden rounded-full font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/20",
          className
        )}
        onMouseMove={handleMouse}
        onMouseLeave={reset}
        animate={{ x: position.x, y: position.y }}
        transition={{ type: "spring", stiffness: 150, damping: 15, mass: 0.1 }}
        {...props}
      >
        <div className="pointer-events-none relative z-10">{children}</div>
      </motion.button>
    );
  }
);

MagneticButton.displayName = "MagneticButton";
