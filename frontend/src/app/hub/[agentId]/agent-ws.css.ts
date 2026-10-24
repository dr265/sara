/**
 * Generic agent workspace styles — the shared chassis every registry agent
 * lands in. Scoped under `.aws-root`, composed from product tokens.
 */
export const AGENT_WS_CSS = `
.aws-root{
  position:relative;height:100%;width:100%;
  display:flex;flex-direction:column;overflow:hidden;
  background:var(--sara-bg);color:var(--sara-ink);
  font-family:var(--sara-font-sans);
}

/* ═══ HEADER ═══ */
.aws-header{
  position:relative;z-index:10;flex:none;display:flex;align-items:center;
  justify-content:space-between;gap:1rem;
  height:4rem;padding:0 1.25rem;
  border-bottom:1px solid var(--sara-line);
  background:rgba(6,8,13,0.55);
  -webkit-backdrop-filter:blur(18px) saturate(120%);backdrop-filter:blur(18px) saturate(120%);
}
.aws-header-left{display:flex;align-items:center;gap:1rem;min-width:0}
.aws-back{
  display:flex;align-items:center;justify-content:center;
  width:2.25rem;height:2.25rem;border-radius:var(--sara-r-sm);
  border:1px solid var(--sara-line);color:var(--sara-muted);
  transition:color .2s,border-color .2s,background .2s;
}
.aws-back:hover{color:var(--sara-accent);border-color:rgba(94,234,212,0.4);background:var(--sara-surface-2)}
.aws-header-divide{width:1px;height:1.5rem;background:var(--sara-line)}
.aws-header-id{min-width:0}
.aws-header-name{display:flex;align-items:center;gap:0.625rem}
.aws-header-name h1{
  margin:0;font-size:0.9375rem;font-weight:600;letter-spacing:-0.01em;color:var(--sara-ink);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.aws-header-cat{
  margin:2px 0 0;font-family:var(--sara-font-mono);font-size:9.5px;font-weight:600;
  letter-spacing:0.18em;text-transform:uppercase;color:var(--sara-faint);
}
.aws-caps{display:flex;align-items:center;gap:0.375rem;flex-wrap:wrap;justify-content:flex-end}

/* ═══ BODY ═══ */
.aws-body{flex:1;display:flex;flex-direction:column;overflow:hidden}
@media (min-width:1024px){.aws-body{flex-direction:row}}

/* ═══ CHAT ═══ */
.aws-chat{
  flex:1;display:flex;flex-direction:column;min-width:0;position:relative;
  border-right:1px solid var(--sara-line);
  background:rgba(9,12,19,0.32);
}
.aws-log{
  flex:1;overflow-y:auto;padding:1.5rem 1.5rem 2rem;
  display:flex;flex-direction:column;gap:1.25rem;
}
.aws-msg{display:flex;flex-direction:column;max-width:86%}
.aws-msg-user{align-self:flex-end;align-items:flex-end}
.aws-msg-agent{align-self:flex-start;align-items:flex-start}
.aws-msg-author{
  font-family:var(--sara-font-mono);font-size:9.5px;font-weight:600;
  letter-spacing:0.14em;text-transform:uppercase;color:var(--sara-faint);
  margin-bottom:0.375rem;padding-left:0.25rem;
}
.aws-bubble{
  padding:0.875rem 1.125rem;border-radius:var(--sara-r-lg);
  border:1px solid var(--sara-line);
  font-size:0.875rem;line-height:1.65;
}
.aws-bubble-user{
  background:rgba(94,234,212,0.09);border-color:rgba(94,234,212,0.26);
  color:var(--sara-ink);border-bottom-right-radius:var(--sara-r-xs);
}
.aws-bubble-agent{
  background:rgba(255,255,255,0.035);color:var(--sara-ink-2);
  border-bottom-left-radius:var(--sara-r-xs);
}

/* markdown roles */
.aws-md-p{margin:0 0 0.75rem}
.aws-md-p:last-child{margin-bottom:0}
.aws-md-strong{font-weight:600;color:var(--sara-ink)}
.aws-md-ul{margin:0 0 0.75rem;padding-left:1.15rem;list-style:disc;display:flex;flex-direction:column;gap:0.25rem}
.aws-md-ol{margin:0 0 0.75rem;padding-left:1.15rem;list-style:decimal;display:flex;flex-direction:column;gap:0.25rem}
.aws-md-li{padding-left:0.25rem}
.aws-md-h1{font-size:1.125rem;font-weight:600;margin:0 0 0.5rem;color:var(--sara-ink)}
.aws-md-h2{font-size:1rem;font-weight:600;margin:1.25rem 0 0.5rem;color:var(--sara-ink)}
.aws-md-h3{font-size:0.875rem;font-weight:600;margin:1rem 0 0.5rem;color:var(--sara-ink)}
.aws-md-code{
  font-family:var(--sara-font-mono);font-size:0.75rem;
  background:rgba(94,234,212,0.1);color:var(--sara-accent);
  padding:0.125rem 0.375rem;border-radius:var(--sara-r-xs);
}

/* input bar */
.aws-input-bar{
  flex:none;padding:0.875rem 1.25rem 1.125rem;
  border-top:1px solid var(--sara-line);
  background:rgba(6,8,13,0.72);
  -webkit-backdrop-filter:blur(16px);backdrop-filter:blur(16px);
}
.aws-input-form{position:relative;display:flex;align-items:center;gap:0.5rem}
.aws-input-field{padding-right:3rem}
.aws-send{
  position:absolute;right:0.375rem;top:50%;transform:translateY(-50%);
  width:2.25rem;height:2.25rem;border-radius:var(--sara-r-sm);
  border:1px solid rgba(94,234,212,0.32);background:rgba(94,234,212,0.1);
  color:var(--sara-accent);display:flex;align-items:center;justify-content:center;
  transition:background .2s,border-color .2s,transform .2s var(--sara-ease-spring);
}
.aws-send:hover:not(:disabled){background:rgba(94,234,212,0.2);transform:translateY(-50%) scale(1.05)}
.aws-send:disabled{opacity:.35;cursor:not-allowed}

/* ═══ EXECUTION CANVAS ═══ */
.aws-canvas{
  display:none;flex:1;position:relative;overflow:hidden;
  background:rgba(4,6,10,0.42);
}
@media (min-width:1024px){.aws-canvas{display:flex}}
.aws-canvas-grid{
  position:absolute;inset:0;
  background-image:
    linear-gradient(var(--sara-line-soft) 1px,transparent 1px),
    linear-gradient(90deg,var(--sara-line-soft) 1px,transparent 1px);
  background-size:56px 56px;
  -webkit-mask-image:radial-gradient(ellipse 70% 60% at 50% 40%,#000 26%,transparent 76%);
  mask-image:radial-gradient(ellipse 70% 60% at 50% 40%,#000 26%,transparent 76%);
}
.aws-canvas-fade{
  position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(150deg,rgba(4,6,10,0.9) 0%,rgba(4,6,10,0.5) 45%,transparent 100%);
}
.aws-canvas-inner{
  position:relative;z-index:2;flex:1;display:flex;flex-direction:column;
  align-items:center;justify-content:center;text-align:center;padding:2rem;gap:0.875rem;
}
.aws-canvas-mark{
  width:4rem;height:4rem;border-radius:var(--sara-r-xl);
  border:1px solid var(--sara-line);background:var(--sara-surface-2);
  display:flex;align-items:center;justify-content:center;color:var(--sara-dim);
  box-shadow:var(--sara-sh-3);
}
.aws-canvas-title{margin:0;font-size:1.125rem;font-weight:600;color:var(--sara-ink)}
.aws-canvas-copy{margin:0;max-width:24rem;font-size:0.8125rem;line-height:1.7;color:var(--sara-dim)}
.aws-canvas-copy strong{color:var(--sara-ink-2);font-weight:600}

/* ═══ NOT FOUND ═══ */
.aws-notfound{
  min-height:100vh;align-items:center;justify-content:center;text-align:center;
  padding:0 1.5rem;gap:0.75rem;
}
.aws-notfound-mark{
  font-size:2.5rem;color:var(--sara-accent);text-shadow:0 0 24px var(--sara-accent-glow);
}
.aws-notfound-title{
  margin:0;font-family:var(--sara-font-mono);font-size:1.25rem;font-weight:600;
  letter-spacing:0.08em;color:var(--sara-err);
}
.aws-notfound-copy{margin:0;max-width:28rem;font-size:0.875rem;color:var(--sara-muted)}

@media (max-width:520px){
  .aws-log{padding:1.125rem 1rem 1.5rem}
  .aws-input-bar{padding:0.75rem 1rem 1rem}
  .aws-header{padding:0 0.875rem;gap:0.5rem}
  .aws-caps{display:none}
}
`;
