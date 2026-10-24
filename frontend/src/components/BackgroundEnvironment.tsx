"use client";

import SaraParticles from "@/components/SaraParticles";

/**
 * BackgroundEnvironment — the single ambient backdrop for the Nexus Hub.
 *
 * Previously pulled two external assets (getlayers.ai gradient SVG + flower.mp4).
 * Now composed entirely from the product design system: the layered `sara-field`
 * gradient, a hairline grid mask, film grain, and a low-cost canvas constellation.
 * Everything is pointer-events:none and behind content (z -1).
 */
export default function BackgroundEnvironment() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none fixed inset-0 z-0 overflow-hidden"
    >
      <div className="sara-field sara-grain absolute inset-0" />
      <div className="absolute inset-0 opacity-[0.55]">
        <SaraParticles />
      </div>
      {/* Vignette so content stays legible at the edges */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(120% 92% at 50% 40%, transparent 34%, rgba(4,5,10,0.55) 100%)",
        }}
      />
    </div>
  );
}
