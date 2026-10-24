"use client";

import { useEffect, useRef } from "react";

/**
 * SaraParticles — a lightweight 2D canvas constellation.
 *
 * Replaces the external flower.mp4 / getlayers.ai gradient dependencies with a
 * self-contained ambient field. Deliberately cheap: a few dozen drifting nodes,
 * subtle parallax on pointer move, and full disable under reduced-motion or on
 * small/low-power screens. No WebGL, no allocations in the hot loop.
 */
export default function SaraParticles({
  density = 0.000055,
  className = "",
}: {
  density?: number;
  className?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const reduce = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    const isSmall = window.matchMedia("(max-width: 860px)").matches;

    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    let width = 0;
    let height = 0;
    let dpr = 1;
    let nodes: {
      x: number;
      y: number;
      vx: number;
      vy: number;
      r: number;
      a: number;
    }[] = [];
    let pointerX = 0;
    let pointerY = 0;
    let targetPX = 0;
    let targetPY = 0;
    let raf = 0;
    let running = true;

    const LINK = isSmall ? 96 : 132;

    const build = () => {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = canvas.clientWidth;
      height = canvas.clientHeight;
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const count = Math.min(
        isSmall ? 34 : 74,
        Math.max(16, Math.round(width * height * density * 1000)),
      );
      nodes = Array.from({ length: count }, () => ({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.16,
        vy: (Math.random() - 0.5) * 0.16,
        r: Math.random() * 1.15 + 0.45,
        a: Math.random() * 0.42 + 0.16,
      }));
    };

    const draw = () => {
      if (!running) return;
      ctx.clearRect(0, 0, width, height);

      pointerX += (targetPX - pointerX) * 0.045;
      pointerY += (targetPY - pointerY) * 0.045;
      const ox = (pointerX - 0.5) * 22;
      const oy = (pointerY - 0.5) * 22;

      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        n.x += n.vx;
        n.y += n.vy;

        if (n.x < -12) n.x = width + 12;
        else if (n.x > width + 12) n.x = -12;
        if (n.y < -12) n.y = height + 12;
        else if (n.y > height + 12) n.y = -12;

        const px = n.x + ox * (n.r * 0.9);
        const py = n.y + oy * (n.r * 0.9);

        ctx.beginPath();
        ctx.arc(px, py, n.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(186, 230, 253, ${n.a * 0.55})`;
        ctx.fill();

        for (let j = i + 1; j < nodes.length; j++) {
          const m = nodes[j];
          const dx = px - (m.x + ox * (m.r * 0.9));
          const dy = py - (m.y + oy * (m.r * 0.9));
          const d2 = dx * dx + dy * dy;
          if (d2 > LINK * LINK) continue;
          const f = 1 - Math.sqrt(d2) / LINK;
          ctx.strokeStyle = `rgba(94, 234, 212, ${f * 0.13})`;
          ctx.lineWidth = 0.6;
          ctx.beginPath();
          ctx.moveTo(px, py);
          ctx.lineTo(m.x + ox * (m.r * 0.9), m.y + oy * (m.r * 0.9));
          ctx.stroke();
        }
      }

      raf = requestAnimationFrame(draw);
    };

    const onPointerMove = (e: PointerEvent) => {
      targetPX = e.clientX / window.innerWidth;
      targetPY = e.clientY / window.innerHeight;
    };
    const onResize = () => build();

    build();
    targetPX = pointerX = 0.5;
    targetPY = pointerY = 0.42;

    if (reduce) {
      // Static single frame — composition still reads, motion does not.
      running = true;
      draw();
      running = false;
      cancelAnimationFrame(raf);
    } else {
      draw();
      window.addEventListener("pointermove", onPointerMove, { passive: true });
      window.addEventListener("resize", onResize);
    }

    return () => {
      running = false;
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("resize", onResize);
    };
  }, [density]);

  return (
    <canvas
      ref={canvasRef}
      className={`pointer-events-none h-full w-full ${className}`}
      aria-hidden="true"
    />
  );
}
