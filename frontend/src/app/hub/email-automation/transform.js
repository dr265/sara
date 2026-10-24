const fs = require('fs');
const file = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/email-automation/page.tsx';
let content = fs.readFileSync(file, 'utf8');

// 1. Add liquid-glass and shiny styles right after 'use client'
const styleInjection = `
import { Apple } from "lucide-react";

const AURA_STYLES = \`
  .liquid-glass {
    background: rgba(255,255,255,0.01);
    background-blend-mode: luminosity;
    backdrop-filter: blur(4px);
    border: none;
    box-shadow: inset 0 1px 1px rgba(255,255,255,0.1);
    position: relative; overflow: hidden;
  }
  .liquid-glass::before {
    content: ''; position: absolute; inset: 0; border-radius: inherit;
    padding: 1.4px;
    background: linear-gradient(180deg,
      rgba(255,255,255,0.45) 0%, rgba(255,255,255,0.15) 20%,
      rgba(255,255,255,0) 40%, rgba(255,255,255,0) 60%,
      rgba(255,255,255,0.15) 80%, rgba(255,255,255,0.45) 100%);
    -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude;
    pointer-events: none;
  }
  @keyframes shiny {
    0% { background-position: -200% center; }
    100% { background-position: 200% center; }
  }
  .animate-shiny {
    animation: shiny 6s linear infinite;
  }
\`;

function AuraBackground() {
  return (
    <>
      <style>{AURA_STYLES}</style>
      <svg className="hidden">
        <filter id="c3-noise">
          <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" stitchTiles="stitch" />
          <feColorMatrix type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 0.35 0" />
          <feComposite in2="SourceGraphic" operator="in" result="noise" />
          <feBlend in="SourceGraphic" in2="noise" mode="multiply" />
        </filter>
      </svg>
      <div className="fixed inset-0 z-0 pointer-events-none">
        <video autoPlay loop muted playsInline
          className="w-full h-full object-cover pointer-events-none opacity-50"
          src="https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260508_064122_c4750c0e-7476-4b44-94a2-a85a65c63bf2.mp4" />
      </div>
      <div className="hidden md:block pointer-events-none fixed inset-y-0 left-1/2 -translate-x-[calc(50%+36rem)] w-px bg-white/10 z-[5]" />
      <div className="hidden md:block pointer-events-none fixed inset-y-0 left-1/2 translate-x-[calc(-50%+36rem)] w-px bg-white/10 z-[5]" />
    </>
  );
}

function AppleButton({ label, onClick, className }: any) {
  return (
    <button onClick={onClick} className={\`group inline-flex items-center justify-center gap-2 rounded-full bg-white text-black font-medium text-sm px-5 py-3 transition-all hover:bg-white/90 active:scale-[0.98] \${className || ''}\`}>
      <Apple className="w-4 h-4" />
      {label}
      <svg className="w-4 h-4 transition-transform group-hover:translate-x-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" /></svg>
    </button>
  );
}
`;

content = content.replace(/(import React.*?from 'react';)/, "$1\n" + styleInjection);

// 2. Replace ambient background inside main render
const startIdx = content.indexOf('<main className="relative min-h-screen w-full bg-[#060a0d] text-slate-100 overflow-x-hidden">');
const endIdx = content.indexOf('{/* ============ HERO ============ */}');
if (startIdx !== -1 && endIdx !== -1) {
  content = content.substring(0, startIdx) + 
    '<main className="relative min-h-screen w-full bg-[#0c0c0c] text-white overflow-x-hidden" style={{ fontFamily: \\"\'Inter\', system-ui, sans-serif\\" }}>\\n' +
    '      <AuraBackground />\\n' +
    '      <div className="relative z-10 mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 py-8 space-y-8">\\n        ' +
    content.substring(endIdx);
}

// 3. String replacements for colors and styling
content = content.replaceAll('bg-[#0b1014]', 'liquid-glass bg-[#0e1014]/90 backdrop-blur-2xl');
content = content.replaceAll('bg-[#0a1518]', 'liquid-glass bg-[#0e1014]/90 backdrop-blur-2xl');
content = content.replaceAll('bg-teal-500/5', 'bg-white/5');
content = content.replaceAll('bg-cyan-600/5', 'bg-white/5');
content = content.replaceAll('border-teal-500/20', 'border-white/10');
content = content.replaceAll('border-teal-500/30', 'border-white/10');
content = content.replaceAll('border-teal-400/30', 'border-white/10');
content = content.replaceAll('border-teal-500', 'border-white/20');
content = content.replaceAll('text-teal-400', 'text-white/80');
content = content.replaceAll('text-teal-300', 'text-white/70');
content = content.replaceAll('text-slate-100', 'text-white');
content = content.replaceAll('text-slate-200', 'text-white/90');
content = content.replaceAll('text-slate-300', 'text-white/80');
content = content.replaceAll('text-slate-400', 'text-white/60');
content = content.replaceAll('text-slate-500', 'text-white/40');
content = content.replaceAll('text-slate-600', 'text-white/30');
content = content.replaceAll('focus:border-teal-400/70', 'focus:border-white/40');
content = content.replaceAll('focus:ring-teal-400/40', 'focus:ring-white/20');
content = content.replaceAll('bg-teal-500 text-black', 'bg-white text-black font-semibold rounded-full');
content = content.replaceAll('bg-teal-500', 'bg-white text-black font-semibold rounded-full');
content = content.replaceAll('hover:bg-teal-400', 'hover:bg-white/90');
content = content.replaceAll('bg-teal-400/10', 'bg-white/10');
content = content.replaceAll('text-teal-500', 'text-white');
content = content.replaceAll('bg-gradient-to-r from-teal-300 via-cyan-300 to-teal-400', 'animate-shiny');

// Make hero h1 shiny
content = content.replaceAll(
  '<span className="animate-shiny bg-clip-text text-transparent">',
  '<span className="animate-shiny bg-clip-text text-transparent" style={{ backgroundImage: "linear-gradient(to right, #091020 0%, #0B2551 12.5%, #A4F4FD 32.5%, #00d2ff 50%, #0B2551 67.5%, #091020 87.5%, #091020 100%)", backgroundSize: "200% auto", filter: "url(#c3-noise)" }}>'
);

fs.writeFileSync(file, content, 'utf8');
console.log('Successfully transformed email-automation page to Aura aesthetic');
