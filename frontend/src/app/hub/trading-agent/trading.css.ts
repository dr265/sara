/**
 * Trading Signal Agent styles — dense command-center surface.
 * Shared tokens, scoped under `.tg-root`.
 */
export const TRADING_CSS = `
.tg-root{
  font-family:var(--sara-font-sans);color:var(--sara-ink);
  display:flex;flex-direction:column;gap:0.75rem;
}
.tg-root *{box-sizing:border-box}
.tg-root button,.tg-root input,.tg-root textarea,.tg-root select{font:inherit}
.tg-root button{cursor:pointer}
.tg-root label{cursor:default}

.tg-card{
  border-radius:var(--sara-r-lg);border:1px solid var(--sara-line);
  background:linear-gradient(165deg,rgba(255,255,255,0.042),rgba(255,255,255,0.014));
  -webkit-backdrop-filter:blur(16px) saturate(118%);backdrop-filter:blur(16px) saturate(118%);
  box-shadow:var(--sara-sh-2);
  padding:1rem;display:flex;flex-direction:column;gap:0.625rem;
}
.tg-title{
  font-family:var(--sara-font-mono);font-size:0.6875rem;font-weight:600;
  letter-spacing:0.14em;text-transform:uppercase;color:var(--sara-dim);
}
.tg-row{display:flex;align-items:center;gap:0.875rem;flex-wrap:wrap}
.tg-grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0.75rem}

.tg-mono{font-family:var(--sara-font-mono);font-variant-numeric:tabular-nums}

.tg-input{
  background:rgba(4,6,10,0.6);color:var(--sara-ink);
  border:1px solid var(--sara-line);border-radius:var(--sara-r-sm);
  padding:0.375rem 0.625rem;font-weight:600;outline:none;
  transition:border-color .2s,box-shadow .2s;
}
.tg-input:focus{border-color:rgba(94,234,212,0.5);box-shadow:0 0 0 3px rgba(94,234,212,0.1)}

.tg-lbl{display:flex;align-items:center;gap:0.375rem;font-size:0.6875rem;color:var(--sara-muted)}
.tg-lbl input[type="range"]{accent-color:var(--sara-accent)}
.tg-lbl input[type="checkbox"]{accent-color:var(--sara-accent)}
.tg-lbl input[type="file"]{font-size:0.6875rem;color:var(--sara-dim);max-width:9rem}

.tg-btn{
  border:0;border-radius:var(--sara-r-sm);padding:0.5rem 0.875rem;font-weight:700;font-size:0.8125rem;
  background:var(--sara-accent);color:var(--sara-accent-ink);
  transition:filter .2s,transform .2s var(--sara-ease-spring);
}
.tg-btn:hover:not(:disabled){filter:brightness(1.08);transform:translateY(-1px)}
.tg-btn:disabled{opacity:.45;cursor:not-allowed}
.tg-btn-warn{background:var(--sara-warn);color:#231a06}
.tg-btn-ghost{
  background:transparent;color:var(--sara-ink-2);
  border:1px solid var(--sara-line);
}
.tg-btn-ghost:hover:not(:disabled){border-color:var(--sara-line-strong);background:var(--sara-surface-2)}

/* signal banner */
.tg-banner{
  padding:1.1rem;border-radius:var(--sara-r-lg);text-align:center;
  font-weight:800;font-size:clamp(1rem,2.4vw,1.5rem);letter-spacing:0.02em;color:#fff;
  border:1px solid var(--sara-line);background:#141a23;
  transition:background .4s,border-color .4s;
}
.tg-banner.call {background:linear-gradient(100deg,#15803d,#16a34a);border-color:rgba(255,255,255,0.18)}
.tg-banner.put  {background:linear-gradient(100deg,#b91c1c,#dc2626);border-color:rgba(255,255,255,0.18)}

/* tables */
.tg-table{width:100%;border-collapse:collapse}
.tg-th{text-align:left;padding:0.375rem 0.5rem;font-size:0.6875rem;color:var(--sara-dim);
  border-bottom:1px solid var(--sara-line);font-family:var(--sara-font-mono);letter-spacing:0.06em}
.tg-td{padding:0.375rem 0.5rem;border-bottom:1px solid var(--sara-line-soft);font-size:0.8125rem}

/* region picker */
.tg-region{border:2px solid var(--sara-accent);background:rgba(94,234,212,0.16)}

.tg-debug-img{
  border:1px solid var(--sara-line);border-radius:var(--sara-r-sm);
  image-rendering:pixelated;max-width:21rem;max-height:8rem;background:#fff;
}

@media (max-width:720px){
  .tg-grid2{grid-template-columns:1fr}
  .tg-row{gap:0.625rem}
  .tg-lbl input[type="file"]{max-width:100%}
}
`;
