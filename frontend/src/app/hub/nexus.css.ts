/**
 * Nexus Hub styles — the command-center surface. Composed entirely from the
 * shared design tokens; scoped under `.nexus-root` so it never leaks.
 */
export const NEXUS_CSS = `
.nexus-root{position:relative}
.nexus-intro{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;flex-wrap:wrap;padding:4px 0 4px}
.nexus-intro h1{font-size:clamp(30px,3.5vw,48px);font-weight:500;letter-spacing:-.045em;line-height:1.12;margin:16px 0 12px}
.nexus-intro p{font-size:13px;line-height:1.7;color:var(--sara-muted)}
.nexus-routing-error{margin-top:14px;padding:12px 14px;border:1px solid #f8717140;border-radius:8px;background:var(--sara-err-bg);color:#fecaca;font-size:12px;line-height:1.6}

/* ═══════ COMMAND DECK — asymmetric 7/5 ═══════ */
.nexus-deck{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(0,1fr);gap:1.25rem;align-items:stretch}

.nexus-deck-main{
  position:relative;overflow:hidden;
  border-radius:var(--sara-r-2xl);border:1px solid var(--sara-line);
  background:
    radial-gradient(110% 130% at 88% -10%,rgba(94,234,212,0.10) 0%,transparent 46%),
    linear-gradient(158deg,#0b1119 0%,#070b11 60%,#04060a 100%);
  box-shadow:var(--sara-sh-2);
  min-height:310px;display:flex;align-items:flex-end;
  padding:clamp(1.5rem,3vw,2.25rem);
}
.nexus-deck-side{display:flex;flex-direction:column;gap:1.25rem;min-width:0}

.nexus-deck-panel{
  position:relative;flex:1;overflow:hidden;
  border-radius:var(--sara-r-2xl);border:1px solid var(--sara-line);
  background:
    radial-gradient(110% 120% at 10% -12%,rgba(125,211,252,0.085) 0%,transparent 48%),
    linear-gradient(158deg,#0b0e19 0%,#070912 60%,#04060a 100%);
  box-shadow:var(--sara-sh-3);
  padding:clamp(1.5rem,3vw,2.25rem);
  display:flex;flex-direction:column;justify-content:space-between;
}

.nexus-holo{position:absolute;inset:-25% -32% 0 20%;z-index:0;opacity:0.28;pointer-events:none;mask-image:linear-gradient(90deg,transparent,#000 60%)}

.nexus-display-title{
  font-family:var(--sara-font-sans);font-weight:500;
  font-size:clamp(1.5rem,2.9vw,2.25rem);line-height:1.06;letter-spacing:-0.018em;
  color:var(--sara-ink);
}
.nexus-deck-copy{
  margin-top:0.65rem;max-width:34rem;font-size:0.8125rem;line-height:1.7;
  color:var(--sara-muted);
}

.nexus-orchestrator{position:relative;margin-top:1.5rem;display:flex;gap:0.625rem;flex-wrap:wrap}
.nexus-orchestrator-input{
  flex:1 1 16rem;min-width:0;border-radius:var(--sara-r-md);
  background:rgba(4,6,10,0.62);border-color:rgba(94,234,212,0.22);
}
.nexus-orchestrator-input:focus{border-color:rgba(94,234,212,0.55)}

/* ═══════ TELEMETRY STRIP ═══════ */
.nexus-telemetry{
  display:flex;align-items:stretch;min-height:54px;
  border-radius:var(--sara-r-xl);border:1px solid var(--sara-line);
  background:var(--sara-glass);
  -webkit-backdrop-filter:blur(18px) saturate(120%);backdrop-filter:blur(18px) saturate(120%);
  box-shadow:var(--sara-sh-2);overflow:hidden;
}
.nexus-telemetry-divide{width:1px;background:var(--sara-line);margin-block:0.85rem}

/* ═══════ CATEGORY PILLS ═══════ */
.nexus-pill{
  white-space:nowrap;cursor:pointer;font:inherit;
  font-size:0.75rem;font-weight:500;letter-spacing:0.01em;
  padding:0.55rem 0.9rem;border-radius:var(--sara-r-xs);
  border:1px solid var(--sara-line);background:var(--sara-surface);
  color:var(--sara-muted);
  transition:color .2s,border-color .2s,background .2s,transform .2s var(--sara-ease-spring);
}
.nexus-pill:hover{color:var(--sara-ink);border-color:var(--sara-line-strong);transform:translateY(-1px)}
.nexus-pill-on{
  border-color:rgba(94,234,212,0.5);background:var(--sara-accent);
  color:var(--sara-accent-ink);font-weight:600;
  box-shadow:0 8px 20px -10px rgba(45,212,191,0.6);
}

/* ═══════ NODE FIELD ═══════ */
.nexus-grid{
  display:grid;gap:1.125rem;
  grid-template-columns:repeat(1,minmax(0,1fr));
}
@media (min-width:640px){.nexus-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (min-width:1100px){.nexus-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (min-width:1500px){.nexus-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}

.nexus-node{
  background:linear-gradient(165deg,rgba(255,255,255,0.048),rgba(255,255,255,0.014));
  /* Directory surfaces stay solid; blur belongs to navigation and dialogs. */
  min-height:15rem;
}

/* ── per-node accent treatments (instruments, not clones) ── */
.nexus-node-icon{
  width:2.75rem;height:2.75rem;flex:none;display:flex;align-items:center;justify-content:center;
  border-radius:var(--sara-r-md);border:1px solid var(--sara-line);
  background:var(--sara-surface-2);font-size:1.25rem;
  transition:transform .3s var(--sara-ease-spring),border-color .3s,box-shadow .3s;
}
.group:hover .nexus-node-icon{transform:scale(1.05)}

.nexus-icon-cyan   {border-color:rgba(94,234,212,0.34);  box-shadow:0 0 18px -6px rgba(94,234,212,0.4)}
.nexus-icon-emerald{border-color:rgba(52,211,153,0.34); box-shadow:0 0 18px -6px rgba(52,211,153,0.4)}
.nexus-icon-pink   {border-color:rgba(244,114,182,0.34);box-shadow:0 0 18px -6px rgba(244,114,182,0.4)}
.nexus-icon-violet {border-color:rgba(167,139,250,0.34);box-shadow:0 0 18px -6px rgba(167,139,250,0.4)}
.nexus-icon-amber  {border-color:rgba(251,191,36,0.34); box-shadow:0 0 18px -6px rgba(251,191,36,0.4)}
.nexus-icon-blue   {border-color:rgba(125,211,252,0.34);box-shadow:0 0 18px -6px rgba(125,211,252,0.4)}
.nexus-icon-auto{
  border-color:rgba(94,234,212,0.52);background:rgba(94,234,212,0.09);
  box-shadow:0 0 20px -4px rgba(94,234,212,0.55)
}
.nexus-icon-teal   {border-color:rgba(45,212,191,0.34); box-shadow:0 0 18px -6px rgba(45,212,191,0.4)}

/* launch affordance per tone — restrained, weight-based */
.nexus-launch-cyan,
.nexus-launch-emerald,.nexus-launch-pink,.nexus-launch-violet,
.nexus-launch-amber,.nexus-launch-blue,.nexus-launch-teal{
  border-color:var(--sara-line);background:var(--sara-surface);color:var(--sara-ink-2);
}
.nexus-launch-cyan:hover   {border-color:rgba(94,234,212,0.5);  background:rgba(94,234,212,0.10);  color:var(--sara-accent)}
.nexus-launch-emerald:hover{border-color:rgba(52,211,153,0.5);  background:rgba(52,211,153,0.10);  color:var(--sara-ok)}
.nexus-launch-pink:hover   {border-color:rgba(244,114,182,0.5); background:rgba(244,114,182,0.10); color:#f472b6}
.nexus-launch-violet:hover {border-color:rgba(167,139,250,0.5); background:rgba(167,139,250,0.10); color:var(--sara-violet)}
.nexus-launch-amber:hover  {border-color:rgba(251,191,36,0.5);  background:rgba(251,191,36,0.10);  color:var(--sara-warn)}
.nexus-launch-blue:hover   {border-color:rgba(125,211,252,0.5); background:rgba(125,211,252,0.10); color:var(--sara-info)}
.nexus-launch-teal:hover   {border-color:rgba(45,212,191,0.5);  background:rgba(45,212,191,0.10);  color:var(--sara-accent)}
.nexus-launch-auto{
  border-color:rgba(94,234,212,0.42);color:var(--sara-accent);
  background:linear-gradient(100deg,rgba(94,234,212,0.12),rgba(45,212,191,0.06));
  box-shadow:0 10px 24px -14px rgba(45,212,191,0.6)
}
.nexus-launch-auto:hover{border-color:rgba(94,234,212,0.7);filter:brightness(1.12)}

/* edge tint on hover per tone */
.nexus-tone-cyan:hover   {border-color:rgba(94,234,212,0.38)}
.nexus-tone-emerald:hover{border-color:rgba(52,211,153,0.38)}
.nexus-tone-pink:hover   {border-color:rgba(244,114,182,0.38)}
.nexus-tone-violet:hover {border-color:rgba(167,139,250,0.38)}
.nexus-tone-amber:hover  {border-color:rgba(251,191,36,0.38)}
.nexus-tone-blue:hover   {border-color:rgba(125,211,252,0.38)}
.nexus-tone-teal:hover   {border-color:rgba(45,212,191,0.38)}
.nexus-tone-auto:hover   {border-color:rgba(94,234,212,0.55);box-shadow:var(--sara-sh-4),0 0 0 1px rgba(94,234,212,0.18)}

/* ═══════ EMPTY STATE ═══════ */
.nexus-empty{
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:5rem 1.5rem;text-align:center;
  border-radius:var(--sara-r-2xl);border:1px dashed var(--sara-line);
  background:rgba(9,12,19,0.4);
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
}

/* ═══════ RESPONSIVE ═══════ */
@media (max-width:1024px){
  .nexus-deck{grid-template-columns:1fr}
}
@media (max-width:560px){
  .nexus-deck-main{min-height:280px}
  .nexus-orchestrator-input{flex-basis:auto;min-height:46px}
  .nexus-telemetry{flex-direction:column;gap:0.25rem}
  .nexus-telemetry-divide{width:auto;height:1px;margin-block:0}
  .nexus-orchestrator{flex-direction:column}
  .nexus-orchestrator .sara-btn{width:100%}
}
`;
