/**
 * Landing page styles — scoped under `.lumora-wrapper` so the editorial page
 * never collides with the product design system. Composes the shared tokens.
 */
export const LANDING_CSS = `
.lumora-wrapper{
  position:relative; isolation:isolate; overflow-x:hidden;
  font-family: var(--sara-font-sans); color: var(--sara-ink);
  background: var(--sara-bg);
}
.lumora-wrapper *{box-sizing:border-box}
.lumora-wrapper ul{list-style:none;margin:0;padding:0}
.lumora-wrapper button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
.lumora-wrapper a{color:inherit;text-decoration:none}

.lumora-shell{max-width:82rem;margin-inline:auto;padding-inline:clamp(1.25rem,3.4vw,3rem)}
.lumora-serif{font-family:var(--sara-font-serif);font-weight:400;letter-spacing:-0.012em}
.lumora-muted{color:var(--sara-muted)}

.lumora-eyebrow{
  display:inline-flex;align-items:center;gap:0.55rem;
  font-family:var(--sara-font-mono);font-size:10.5px;font-weight:600;
  letter-spacing:0.2em;text-transform:uppercase;color:var(--sara-muted);
}
.lumora-eyebrow::before{
  content:"";width:5px;height:5px;border-radius:9999px;flex:none;
  background:var(--sara-accent);box-shadow:0 0 10px var(--sara-accent-glow);
}
.lumora-eyebrow--light{color:rgba(255,255,255,0.6)}

/* reveal coupling: the wrapper owns the staggered settle */
.lumora-wrapper .lumora-reveal{opacity:0;transform:translateY(18px);transition:opacity .7s cubic-bezier(.16,1,.3,1),transform .7s cubic-bezier(.16,1,.3,1)}
.lumora-wrapper .lumora-reveal.sara-visible{opacity:1;transform:none}
.lumora-wrapper.is-ready .sara-line-inner{opacity:1;transform:none}
.lumora-wrapper .sara-line-inner{transition-delay:120ms}
.lumora-h1 .sara-line-wrap:nth-child(2) .sara-line-inner{transition-delay:240ms}
.lumora-h1 .sara-line-wrap:nth-child(3) .sara-line-inner{transition-delay:360ms}

/* ═══ HERO ═══ */
.lumora-hero{position:relative;isolation:isolate;overflow:hidden;min-height:100svh;background:
  radial-gradient(120% 90% at 82% -6%, rgba(94,234,212,0.10) 0%, transparent 44%),
  radial-gradient(90% 78% at 8% 2%, rgba(125,211,252,0.07) 0%, transparent 46%),
  linear-gradient(178deg,var(--sara-bg-1) 0%,var(--sara-bg) 52%,var(--sara-void) 100%);
  border-bottom:1px solid var(--sara-line);
}
.lumora-hero-bg{position:absolute;inset:0;z-index:0;opacity:0.85}
.lumora-hero-vignette{position:absolute;inset:0;z-index:1;pointer-events:none;
  background:radial-gradient(105% 88% at 52% 42%,transparent 30%,rgba(4,5,10,0.6) 100%)}

.lumora-header{position:absolute;inset-inline:0;top:0;z-index:50;opacity:0;transform:translateY(-12px);
  transition:opacity .7s cubic-bezier(.16,1,.3,1),transform .7s cubic-bezier(.16,1,.3,1)}
.lumora-wrapper.is-ready .lumora-header{opacity:1;transform:none}
.lumora-header-row{display:flex;align-items:center;justify-content:space-between;gap:1.25rem;padding-block:1.35rem}
.lumora-brand{display:inline-flex;align-items:center;gap:0.6rem;font-size:1.0625rem;font-weight:600;
  letter-spacing:0.16em;text-transform:uppercase;color:var(--sara-ink)}
.lumora-mark{width:24px;height:24px;border:1.5px solid var(--sara-ink);border-radius:9999px;
  display:flex;align-items:center;justify-content:center;flex:none}
.lumora-mark-dot{width:8px;height:8px;border-radius:9999px;background:var(--sara-accent);
  box-shadow:0 0 10px var(--sara-accent-glow)}

.lumora-nav{display:flex;align-items:center;gap:1.5rem;font-size:0.8125rem;font-weight:500;color:var(--sara-muted)}
.lumora-nav a{transition:color .2s}
.lumora-nav a:hover{color:var(--sara-ink)}
.lumora-nav-hub{display:inline-flex;align-items:center;gap:0.4rem;padding:0.4rem 0.9rem;border-radius:9999px;
  border:1px solid var(--sara-line);background:var(--sara-surface);color:var(--sara-ink-2);
  transition:all .3s cubic-bezier(.2,.8,.2,1)}
.lumora-nav-hub:hover{border-color:var(--sara-accent);color:var(--sara-accent);transform:translateY(-1px)}

.lumora-header-actions{display:flex;align-items:center;gap:0.75rem}
.lumora-link-btn{font-size:13px;font-weight:600;color:var(--sara-muted);transition:color .2s;margin-right:.25rem}
.lumora-link-btn:hover{color:var(--sara-ink)}
.lumora-userbtn{transform:scale(1.08);border-radius:9999px;box-shadow:var(--sara-sh-2)}

.lumora-hero-grid{position:relative;z-index:20;display:grid;min-height:100svh;
  grid-template-columns:repeat(12,1fr);gap:2.5rem;
  padding:9.5rem 0 7rem;align-items:center}
.lumora-hero-left{display:flex;flex-direction:column;gap:1.5rem;grid-column:span 7}
.lumora-hero-right{grid-column:span 5;display:flex;justify-content:center}

.lumora-h1{font-size:clamp(2.5rem,4.8vw,4.75rem);font-weight:600;line-height:0.98;letter-spacing:-0.032em}
.lumora-hero-sub{max-width:34rem;font-size:clamp(0.95rem,1.1vw,1.0625rem);line-height:1.75;color:var(--sara-muted)}

/* omnibar */
.lumora-omnibar{width:100%;max-width:32rem;margin-top:0.5rem;display:flex;align-items:center;
  border-radius:9999px;padding:0.5rem;background:rgba(9,12,19,0.62);
  border:1px solid var(--sara-line);box-shadow:var(--sara-sh-3);
  -webkit-backdrop-filter:blur(20px) saturate(125%);backdrop-filter:blur(20px) saturate(125%);
  transition:box-shadow .3s,border-color .3s,background .3s}
.lumora-omnibar:focus-within{border-color:rgba(94,234,212,0.42);background:rgba(9,12,19,0.86);
  box-shadow:var(--sara-sh-4),0 0 0 3px rgba(94,234,212,0.09)}
.lumora-omnibar-icon{display:flex;align-items:center;justify-content:center;padding:0 0.85rem 0 1.1rem;
  color:var(--sara-dim);cursor:pointer;transition:color .2s;flex:none}
.lumora-omnibar-icon:hover{color:var(--sara-accent)}
.lumora-omnibar-input{flex:1;min-width:0;background:transparent;border:none;outline:none;
  padding:0.85rem 0.25rem;font-size:0.9375rem;font-weight:500;font-family:inherit;color:var(--sara-ink)}
.lumora-omnibar-input::placeholder{color:var(--sara-faint)}
.lumora-omnibar-send{width:2.5rem;height:2.5rem;border-radius:9999px;flex:none;display:flex;
  align-items:center;justify-content:center;background:var(--sara-accent);color:var(--sara-accent-ink);
  transition:transform .3s cubic-bezier(.2,.8,.2,1)}
.lumora-omnibar-send:hover{transform:scale(1.06)}

/* depth scene — a spatial instrument, CSS 3D only */
.lumora-depth-scene{position:relative;width:min(22rem,100%);aspect-ratio:1/1.06;perspective:1400px;
  perspective-origin:52% 44%}
.lumora-depth-plane{position:absolute;inset:12%;border-radius:9999px;border:1px solid var(--sara-line);
  transform-style:preserve-3d}
.lumora-depth-plane--far{transform:rotateX(64deg) rotateZ(-8deg) translateZ(-120px);
  border-color:rgba(125,211,252,0.16)}
.lumora-depth-plane--mid{inset:22%;transform:rotateX(64deg) rotateZ(6deg) translateZ(-40px);
  border-color:rgba(94,234,212,0.2)}
.lumora-depth-orbit{position:absolute;inset:6%;border-radius:9999px;border:1px dashed rgba(255,255,255,0.12);
  transform:rotateX(58deg);animation:lumora-spin 26s linear infinite;transform-style:preserve-3d}
@keyframes lumora-spin{to{transform:rotateX(58deg) rotateZ(360deg)}}
.lumora-depth-node{position:absolute;width:9px;height:9px;border-radius:9999px;background:var(--sara-accent);
  box-shadow:0 0 14px var(--sara-accent-glow)}
.lumora-depth-node--a{top:4%;left:50%;margin-left:-4.5px}
.lumora-depth-node--b{bottom:14%;right:12%;background:var(--sara-azure);box-shadow:0 0 14px rgba(125,211,252,.35)}
.lumora-depth-node--c{bottom:22%;left:16%;background:var(--sara-brass);box-shadow:0 0 14px rgba(201,169,97,.3)}
.lumora-depth-core{position:absolute;inset:34%;display:grid;place-items:center;
  transform:translateZ(70px);animation:lumora-breathe 6s ease-in-out infinite}
.lumora-depth-core-inner{width:56%;aspect-ratio:1;border-radius:9999px;
  background:radial-gradient(circle at 34% 30%,var(--sara-accent-1),var(--sara-accent-2) 55%,var(--sara-accent-deep));
  box-shadow:0 0 0 1px rgba(255,255,255,0.25),0 24px 60px -18px rgba(45,212,191,0.55),inset 0 2px 6px rgba(255,255,255,0.4)}
@keyframes lumora-breathe{0%,100%{transform:translateZ(70px) scale(1)}50%{transform:translateZ(92px) scale(1.05)}}
.lumora-depth-plate{position:absolute;left:0;right:0;bottom:-6%;display:flex;flex-direction:column;gap:0.25rem;
  padding:0.85rem 1.1rem;border-radius:var(--sara-r-lg);border:1px solid var(--sara-line);
  background:var(--sara-glass);-webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);
  box-shadow:var(--sara-sh-3);transform:translateZ(110px)}
.lumora-depth-plate-label{font-family:var(--sara-font-mono);font-size:9.5px;font-weight:600;
  letter-spacing:0.24em;text-transform:uppercase;color:var(--sara-accent)}
.lumora-depth-plate-value{font-size:0.8125rem;font-weight:600;color:var(--sara-ink-2)}

/* ═══ MARQUEE ═══ */
.lumora-band{border-block:1px solid var(--sara-line);background:rgba(255,255,255,0.014);overflow:hidden;padding-block:0.85rem}
.lumora-band-track{display:flex;width:max-content;animation:lumora-marquee 34s linear infinite}
@keyframes lumora-marquee{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.lumora-band-item{display:inline-flex;align-items:center;gap:1.1rem;padding-inline:1.1rem;
  font-family:var(--sara-font-mono);font-size:11px;font-weight:500;letter-spacing:0.2em;
  text-transform:uppercase;color:var(--sara-dim);white-space:nowrap}
.lumora-band-sep{color:var(--sara-accent);font-size:8px}

/* ═══ SECTIONS ═══ */
.lumora-section{padding-block:clamp(4.5rem,8vw,7.5rem)}
.lumora-section--about{padding-block:clamp(3.5rem,6vw,5.5rem)}
.lumora-section-head{margin-bottom:3rem;max-width:22rem}
.lumora-section-head .lumora-h2{margin-top:1rem}

.lumora-about{display:grid;grid-template-columns:1fr 1.35fr;align-items:center;gap:3rem}
.lumora-about-left{position:relative;min-height:12rem;display:flex;flex-direction:column;justify-content:space-between}
.lumora-about-mark{display:flex;align-items:center;gap:0.75rem;position:absolute;bottom:0;left:0;right:0;
  color:var(--sara-accent)}
.lumora-about-mark span{max-width:15rem;font-size:0.875rem;color:var(--sara-muted);line-height:1.6}
.lumora-h2{font-size:clamp(1.625rem,2.9vw,2.5rem);line-height:1.14}

/* works */
.lumora-works-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:1.25rem}
.lumora-work-card{position:relative;display:block;min-height:22rem;overflow:hidden;
  border-radius:var(--sara-r-2xl);border:1px solid var(--sara-line);padding:1.75rem;color:#fff;
  background:linear-gradient(155deg,#0e1420 0%,#080c13 62%,#05070c 100%);
  transition:transform .5s cubic-bezier(.16,1,.3,1),border-color .5s,box-shadow .5s}
.lumora-work-card::after{content:"";position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(80% 60% at 88% 8%,rgba(94,234,212,0.14),transparent 60%);opacity:0;transition:opacity .5s}
.lumora-work-card:hover{transform:translateY(-6px);border-color:var(--sara-line-strong);box-shadow:var(--sara-sh-4)}
.lumora-work-card:hover::after{opacity:1}
.lumora-work-top{display:flex;justify-content:space-between;font-size:0.7rem;
  text-transform:uppercase;letter-spacing:0.14em;color:rgba(255,255,255,0.42);
  font-family:var(--sara-font-mono)}
.lumora-work-bottom{position:absolute;inset-inline:1.75rem;bottom:1.75rem}
.lumora-work-bottom h3{font-size:clamp(1.5rem,2.4vw,1.875rem);font-weight:500}
.lumora-work-bottom p{margin-top:0.5rem;max-width:28rem;font-size:0.8125rem;line-height:1.65;
  color:rgba(255,255,255,0.55)}
.lumora-tags{margin-top:1.1rem;display:flex;flex-wrap:wrap;gap:0.5rem}
.lumora-tag{display:inline-flex;border-radius:9999px;padding:0.3rem 0.65rem;font-size:0.65rem;
  border:1px solid rgba(255,255,255,0.22);color:rgba(255,255,255,0.75);
  font-family:var(--sara-font-mono);letter-spacing:0.08em}

/* services */
.lumora-services{border-top:1px solid var(--sara-line)}
.lumora-service-row{border-bottom:1px solid var(--sara-line)}
.lumora-service-link{display:flex;align-items:center;gap:1.5rem;border-radius:1.25rem;padding-block:1.85rem;
  transition:padding .45s cubic-bezier(.16,1,.3,1),background .45s}
.lumora-service-link:hover{background:rgba(255,255,255,0.028);padding-left:1.5rem;padding-right:1rem}
.lumora-service-idx{width:2.5rem;font-family:var(--sara-font-mono);font-size:0.75rem;
  font-weight:600;color:var(--sara-faint);flex:none}
.lumora-service-title{flex:1;min-width:0;font-size:clamp(1.35rem,2.4vw,1.875rem);font-weight:500;
  letter-spacing:-0.012em}
.lumora-service-desc{max-width:20rem;font-size:0.8125rem;line-height:1.6;color:var(--sara-muted)}

/* stats */
.lumora-stats-panel{position:relative;overflow:hidden;border-radius:var(--sara-r-2xl);
  border:1px solid rgba(255,255,255,0.1);padding:clamp(2rem,4vw,3.5rem);color:#fff;
  background:linear-gradient(155deg,#101820 0%,#080d13 62%,#05080c 100%);
  box-shadow:var(--sara-sh-4)}
.lumora-stats-panel::after{content:"";position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(70% 55% at 12% 6%,rgba(94,234,212,0.12),transparent 60%)}
.lumora-stats-title{margin-top:1rem}
.lumora-stats-grid{position:relative;z-index:2;margin-top:3rem;display:grid;
  grid-template-columns:repeat(4,1fr);column-gap:2rem;row-gap:2.5rem}
.lumora-stat-num{font-size:clamp(2.75rem,5vw,4rem);font-weight:600;letter-spacing:-0.03em;line-height:1}
.lumora-stat-label{margin-top:0.7rem;font-size:0.8125rem;color:rgba(255,255,255,0.55)}

/* footer */
.lumora-footer{position:relative;overflow:hidden;border-top:1px solid var(--sara-line);
  background:linear-gradient(180deg,#080c13,#05070c);color:#fff}
.lumora-footer-inner{position:relative;z-index:10;padding:clamp(3.5rem,6vw,5.5rem) 0 2.5rem}
.lumora-footer-cta{display:flex;align-items:flex-end;justify-content:space-between;gap:2rem;
  border-bottom:1px solid rgba(255,255,255,0.09);padding-bottom:clamp(2.5rem,4vw,3.5rem)}
.lumora-footer-title{max-width:18ch;font-size:clamp(2rem,4vw,3.25rem)}
.lumora-footer-cols{display:grid;grid-template-columns:1.5fr 1fr 1fr;gap:3rem;padding-block:3rem}
.lumora-footer-brand p{max-width:20rem;margin-top:0.85rem;font-size:0.8125rem;line-height:1.65;
  color:rgba(255,255,255,0.5)}
.lumora-footer .lumora-brand{color:#fff}
.lumora-footer .lumora-mark{border-color:rgba(255,255,255,0.7)}
.lumora-col-title{font-family:var(--sara-font-mono);font-size:0.65rem;text-transform:uppercase;
  letter-spacing:0.2em;color:rgba(255,255,255,0.38);margin-bottom:1.1rem}
.lumora-col-links{display:flex;flex-direction:column;gap:0.7rem;font-size:0.8125rem;color:rgba(255,255,255,0.6)}
.lumora-col-links a{transition:color .2s}
.lumora-col-links a:hover{color:#fff}
.lumora-footer-watermark{position:absolute;inset-inline:0;bottom:-1.2rem;z-index:0;text-align:center;
  pointer-events:none;user-select:none;font-weight:700;line-height:1;
  font-size:clamp(5rem,16vw,12rem);color:rgba(255,255,255,0.035)}

/* ═══ RESPONSIVE ═══ */
@media (max-width:1024px){
  .lumora-hero-left{grid-column:span 12}
  .lumora-hero-right{display:none}
  .lumora-about{grid-template-columns:1fr;gap:2rem}
  .lumora-about-left{min-height:auto}
  .lumora-about-mark{position:static}
  .lumora-stats-grid{grid-template-columns:repeat(2,1fr)}
  .lumora-service-link{flex-wrap:wrap;gap:0.6rem 1.5rem}
  .lumora-service-desc{max-width:100%}
}
@media (max-width:820px){
  .lumora-nav{display:none}
  .lumora-works-grid{grid-template-columns:1fr}
  .lumora-footer-cta{flex-direction:column;align-items:flex-start;gap:1.75rem}
  .lumora-footer-cols{grid-template-columns:1fr 1fr;gap:2.25rem}
}
@media (max-width:520px){
  .lumora-hero-grid{padding:7.5rem 0 5.5rem}
  .lumora-h1{font-size:clamp(2.125rem,9.2vw,3rem)}
  .lumora-omnibar{border-radius:var(--sara-r-2xl);flex-wrap:wrap}
  .lumora-omnibar-input{flex-basis:100%;order:3;padding:0.5rem 0.25rem 0.75rem}
  .lumora-omnibar-icon{order:1}
  .lumora-omnibar-send{order:2;margin-left:auto}
  .lumora-stats-grid{grid-template-columns:1fr 1fr;column-gap:1rem}
  .lumora-footer-cols{grid-template-columns:1fr}
  .lumora-work-card{min-height:18rem}
}
`;
