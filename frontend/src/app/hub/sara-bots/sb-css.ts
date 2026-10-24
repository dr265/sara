/* Shared style language for the Sara Bots screens (Fleet grid + "My Bots").
 * Injected once per route: page.tsx and [id]/page.tsx both render
 * <style>{SB_CSS}</style> so every sb-* class is available everywhere. */

export const SB_CSS = `
.sb-header{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;flex-wrap:wrap;padding:28px 24px 8px;max-width:1500px;margin:0 auto}
.sb-eyebrow{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10px;letter-spacing:.22em;color:#67e8f9}
.sb-title{font-size:clamp(28px,3.2vw,42px);font-weight:800;letter-spacing:-.03em;margin:10px 0 8px}
.sb-sub{max-width:640px;font-size:13px;line-height:1.7;color:#94a3b8;margin:0}
.sb-header-actions{display:flex;gap:10px;flex-wrap:wrap}

.sb-btn{border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.06);color:#e2e8f0;border-radius:12px;padding:9px 16px;font-size:12.5px;font-weight:600;cursor:pointer;transition:all .2s}
.sb-btn:hover{background:rgba(255,255,255,.12);border-color:rgba(103,232,249,.4)}
.sb-btn:disabled{opacity:.45;cursor:not-allowed}
.sb-btn-primary{background:linear-gradient(180deg,#22d3ee,#0891b2);border-color:transparent;color:#06121a;font-weight:800}
.sb-btn-primary:hover{filter:brightness(1.1);background:linear-gradient(180deg,#22d3ee,#0891b2)}
.sb-btn-ghost{background:transparent}
.sb-mt{margin-top:12px}
.sb-link{color:#67e8f9;text-decoration:underline}

.sb-banner{max-width:1500px;margin:14px auto 0;padding:12px 16px;border-radius:12px;font-size:12.5px;line-height:1.6;display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.sb-banner-info{border:1px solid rgba(103,232,249,.3);background:rgba(34,211,238,.08);color:#a5f3fc}
.sb-banner-ok{border:1px solid rgba(52,211,153,.3);background:rgba(52,211,153,.08);color:#a7f3d0}
.sb-banner-error{border:1px solid rgba(248,113,113,.3);background:rgba(248,113,113,.08);color:#fecaca}
.sb-banner code{background:rgba(0,0,0,.35);padding:1px 6px;border-radius:6px}
.sb-banner-x{background:none;border:none;color:inherit;font-size:16px;cursor:pointer;line-height:1}

.sb-toolbar{max-width:1500px;margin:18px auto 0;padding:0 24px;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;align-items:center}
.sb-counts{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.sb-count-total{font-family:ui-monospace,monospace;font-size:11px;color:#64748b;margin-left:6px}
.sb-pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;border:1px solid;font-size:10.5px;font-weight:700;letter-spacing:.02em}
.sb-pill-dot{width:6px;height:6px;border-radius:50%;display:inline-block}
.sb-toolbar-right{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.sb-kind-switch{display:flex;border:1px solid rgba(255,255,255,.12);border-radius:999px;overflow:hidden}
.sb-kind-btn{background:transparent;border:none;color:#94a3b8;font-size:11.5px;font-weight:700;padding:8px 14px;cursor:pointer}
.sb-kind-btn:hover{color:#fff}
.sb-kind-on{background:rgba(34,211,238,.15);color:#67e8f9}
.sb-search{min-width:260px}
.sb-input{background:rgba(2,6,23,.55);border:1px solid rgba(255,255,255,.12);border-radius:12px;color:#e2e8f0;padding:10px 14px;font-size:13px;outline:none}
.sb-input:focus{border-color:rgba(103,232,249,.55)}
.sb-textarea{resize:vertical;font-family:inherit}

/* ── the orb: layered CSS 3D sphere ── */
.sb-orb{position:relative;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;transform-style:preserve-3d}
.sb-orb-core,.sb-orb-atmo,.sb-orb-ring,.sb-orb-spec{position:absolute;inset:0;border-radius:50%}
.sb-orb-core{
  background:
    radial-gradient(circle at 32% 28%, hsla(var(--orb-hue),95%,78%,.95) 0%, hsla(var(--orb-hue),90%,60%,.9) 22%, hsla(var(--orb-hue),85%,42%,.92) 48%, hsla(calc(var(--orb-hue) + 30),80%,22%,.95) 78%, hsla(calc(var(--orb-hue) + 40),75%,12%,.98) 100%);
  box-shadow:
    inset -8px -10px 24px hsla(calc(var(--orb-hue) + 30),90%,8%,.75),
    inset 6px 8px 18px hsla(var(--orb-hue),90%,82%,.35),
    0 0 calc(28px * var(--orb-glow)) hsla(var(--orb-hue),90%,60%,calc(.55 * var(--orb-glow))),
    0 10px 30px rgba(0,0,0,.5);
}
.sb-orb-atmo{
  background:radial-gradient(circle at 68% 74%, hsla(calc(var(--orb-hue) + 60),90%,70%,.28) 0%, transparent 42%);
  filter:blur(2px);mix-blend-mode:screen;
}
.sb-orb-ring{
  inset:-6%;
  border:1px solid hsla(var(--orb-hue),85%,72%,.28);
  transform:rotateX(68deg) rotateZ(-18deg) scale(.92);
  box-shadow:0 0 18px hsla(var(--orb-hue),90%,65%,.2);
}
.sb-orb-spec{
  background:radial-gradient(ellipse 42% 30% at 30% 24%, rgba(255,255,255,.85) 0%, rgba(255,255,255,.18) 45%, transparent 70%);
  filter:blur(1px);
}
@keyframes sb-orb-spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}
@keyframes sb-orb-pulse{
  0%,100%{transform:scale(1);filter:brightness(1)}
  50%{transform:scale(1.055);filter:brightness(1.3)}
}
@keyframes sb-orb-attend{
  0%,100%{box-shadow:0 0 14px hsla(var(--orb-hue),90%,65%,.4)}
  50%{box-shadow:0 0 30px hsla(var(--orb-hue),95%,72%,.85)}
}
.sb-orb-ring{animation:sb-orb-spin 14s linear infinite}
.sb-orb-running .sb-orb-core{animation:sb-orb-pulse 1.15s ease-in-out infinite}
.sb-orb-running .sb-orb-ring{animation-duration:3.5s;border-color:hsla(var(--orb-hue),95%,75%,.55)}
.sb-orb-attention .sb-orb-core{animation:sb-orb-attend 1.8s ease-in-out infinite}
.sb-orb-warn .sb-orb-atmo{background:radial-gradient(circle at 68% 74%, rgba(251,191,36,.4) 0%, transparent 42%)}
.sb-orb-dim{filter:saturate(.25) brightness(.62)}
.sb-orb-dim .sb-orb-ring{animation:none}

/* ── gallery ── */
.sb-grid{max-width:1500px;margin:22px auto 0;padding:0 24px;display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:16px}
@media(min-width:640px){.sb-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(min-width:1100px){.sb-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(min-width:1500px){.sb-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
.sb-card{display:flex;gap:16px;align-items:center;text-align:left;border:1px solid rgba(255,255,255,.08);border-radius:18px;background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.015));padding:18px;cursor:pointer;transition:transform .25s,border-color .25s,box-shadow .25s;color:inherit}
.sb-card:hover{transform:translateY(-3px);border-color:rgba(103,232,249,.35);box-shadow:0 14px 40px rgba(0,0,0,.45),0 0 24px -8px rgba(103,232,249,.25)}
.sb-card:focus-visible{outline:2px solid #67e8f9;outline-offset:3px}
.sb-card-on{border-color:rgba(103,232,249,.55);box-shadow:0 0 0 1px rgba(103,232,249,.35),0 14px 40px rgba(0,0,0,.5)}
.sb-card-orb{flex-shrink:0}
.sb-card-body{min-width:0;flex:1;display:flex;flex-direction:column;gap:7px}
.sb-card-name-row{display:flex;align-items:center;gap:8px}
.sb-card-name{font-weight:800;font-size:14.5px;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sb-kind-tag{font-family:ui-monospace,monospace;font-size:8.5px;letter-spacing:.16em;color:#64748b;border:1px solid rgba(255,255,255,.1);border-radius:6px;padding:2px 6px;flex-shrink:0}
.sb-card-desc{font-size:11.5px;line-height:1.55;color:#94a3b8;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sb-card-foot{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.sb-runnable{font-family:ui-monospace,monospace;font-size:9.5px;letter-spacing:.12em;color:#34d399}
.sb-link-tag{font-family:ui-monospace,monospace;font-size:9.5px;letter-spacing:.12em;color:#64748b}
.sb-empty{max-width:1500px;margin:40px auto;padding:48px 24px;border:1px dashed rgba(255,255,255,.12);border-radius:18px;text-align:center;color:#94a3b8;font-size:13.5px}

/* ── detail ── */
.sb-detail{max-width:1500px;margin:26px auto 0;padding:26px 28px;border:1px solid rgba(103,232,249,.22);border-radius:22px;background:linear-gradient(180deg,rgba(8,15,26,.85),rgba(4,8,16,.9));box-shadow:0 24px 70px rgba(0,0,0,.55),0 0 40px -18px rgba(103,232,249,.3)}
.sb-detail-head{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.sb-detail-title{flex:1;min-width:200px}
.sb-detail-title h2{margin:0 0 6px;font-size:21px;font-weight:800;letter-spacing:-.02em}
.sb-detail-meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sb-detail-cat{font-family:ui-monospace,monospace;font-size:10.5px;color:#64748b}
.sb-detail-actions{display:flex;gap:8px;flex-wrap:wrap}
.sb-detail-desc{margin:16px 0 4px;font-size:13px;line-height:1.75;color:#cbd5e1}
.sb-detail-note{margin:8px 0 0;font-size:12px;line-height:1.65;color:#94a3b8}
.sb-detail-note code{background:rgba(0,0,0,.4);padding:1px 6px;border-radius:6px;font-size:11px}
.sb-detail-warn{color:#fbbf24}

.sb-runform{margin-top:20px;border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:18px 20px;background:rgba(2,6,23,.4)}
.sb-runform-title{margin:0 0 12px;font-size:13px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:#67e8f9}
.sb-field{margin-bottom:12px}
.sb-field-label{display:flex;align-items:baseline;gap:8px;font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:6px}
.sb-field-type{font-family:ui-monospace,monospace;font-size:9.5px;color:#64748b;font-weight:400}
.sb-req{color:#f87171}
.sb-field .sb-input{width:100%}
.sb-field-check{display:flex;gap:8px;align-items:center;font-size:12.5px;color:#cbd5e1}
.sb-confirm-row{display:flex;gap:10px;align-items:center;margin:10px 0;padding:10px 14px;border:1px solid rgba(251,191,36,.4);border-radius:10px;background:rgba(251,191,36,.07);font-size:12.5px;color:#fde68a}
.sb-runform-foot{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:14px}

.sb-runpanel{margin-top:18px;border:1px solid rgba(255,255,255,.1);border-radius:16px;background:rgba(2,6,23,.55);overflow:hidden}
.sb-runpanel-head{display:flex;gap:14px;align-items:center;padding:12px 16px;border-bottom:1px solid rgba(255,255,255,.07);font-family:ui-monospace,monospace;font-size:11px;color:#94a3b8;flex-wrap:wrap}
.sb-run-id{color:#67e8f9}
.sb-run-status{display:inline-flex;align-items:center;gap:6px;font-weight:700}
.sb-run-dur{color:#64748b}
.sb-run-events{display:flex;gap:6px;flex-wrap:wrap;padding:10px 16px 0}
.sb-run-event{font-family:ui-monospace,monospace;font-size:9.5px;letter-spacing:.08em;color:#64748b;border:1px solid rgba(255,255,255,.08);border-radius:6px;padding:2px 8px;text-transform:uppercase}
.sb-output{margin:12px 16px 16px;padding:14px;border-radius:10px;background:rgba(0,0,0,.5);border:1px solid rgba(255,255,255,.07);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;line-height:1.65;color:#a5f3fc;max-height:340px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.sb-output-err{color:#fecaca;border-color:rgba(248,113,113,.25)}

.sb-history{margin-top:18px}
.sb-history-title{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#64748b;margin:0 0 8px}
.sb-history-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.sb-history-row{display:flex;gap:12px;align-items:center;width:100%;text-align:left;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:10px;padding:9px 14px;cursor:pointer;color:inherit;font-size:12px}
.sb-history-row:hover{border-color:rgba(103,232,249,.3)}
.sb-history-preview{flex:1;color:#94a3b8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:ui-monospace,monospace;font-size:11px}
.sb-history-when{color:#64748b;font-family:ui-monospace,monospace;font-size:10.5px;flex-shrink:0}

.sb-scan{max-width:1500px;margin:26px auto 0;padding:18px 24px;border:1px solid rgba(255,255,255,.08);border-radius:16px;background:rgba(2,6,23,.35)}
.sb-scan-title{margin:0 0 8px;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#64748b}
.sb-scan-line{margin:4px 0;font-size:12px;color:#94a3b8;line-height:1.65}
.sb-scan-line code{background:rgba(0,0,0,.4);padding:1px 6px;border-radius:6px;font-size:11px}
.sb-scan-problems{margin-top:8px;font-size:12px;color:#94a3b8}
.sb-scan-problems summary{cursor:pointer;color:#fbbf24}
.sb-scan-problems ul{margin:8px 0 0;padding-left:20px}

@media (prefers-reduced-motion: reduce){
  .sb-orb-ring,.sb-orb-running .sb-orb-core,.sb-orb-attention .sb-orb-core{animation:none}
  .sb-card,.sb-btn{transition:none}
}
`;
