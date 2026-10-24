/**
 * sara-platform styles — the operations console shell.
 * Scoped under `.sp-root`, composed from product tokens.
 */
export const PLATFORM_CSS = `
.sp-root{
  position:relative;display:flex;height:100vh;width:100%;overflow:hidden;
  background:var(--sara-bg);color:var(--sara-ink);
  font-family:var(--sara-font-sans);
}

.sp-field{
  position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(90% 80% at 84% -6%,rgba(94,234,212,0.075) 0%,transparent 44%),
    radial-gradient(80% 70% at 6% 2%,rgba(125,211,252,0.055) 0%,transparent 46%),
    linear-gradient(175deg,var(--sara-bg-1) 0%,var(--sara-bg) 50%,var(--sara-void) 100%);
}
.sp-field::after{
  content:"";position:absolute;inset:0;opacity:0.42;
  background-image:
    linear-gradient(var(--sara-line-soft) 1px,transparent 1px),
    linear-gradient(90deg,var(--sara-line-soft) 1px,transparent 1px);
  background-size:72px 72px;
  -webkit-mask-image:radial-gradient(ellipse 76% 60% at 50% 32%,#000 20%,transparent 78%);
  mask-image:radial-gradient(ellipse 76% 60% at 50% 32%,#000 20%,transparent 78%);
}

/* ═══ SIDEBAR ═══ */
.sp-sidebar{
  position:relative;z-index:30;display:flex;flex-direction:column;
  border-right:1px solid var(--sara-line);
  background:rgba(7,9,14,0.78);
  -webkit-backdrop-filter:blur(20px) saturate(120%);backdrop-filter:blur(20px) saturate(120%);
  transition:width .3s var(--sara-ease-slow);flex:none;
}
.sp-sidebar-open{width:16rem}
.sp-sidebar-rail{width:4.5rem}

.sp-sidebar-head{
  display:flex;align-items:center;justify-content:space-between;gap:0.625rem;
  padding:1.125rem 1rem;border-bottom:1px solid var(--sara-line);
}
.sp-brand{display:inline-flex;align-items:center;gap:0.625rem;min-width:0}
.sp-brand-mark{
  width:2rem;height:2rem;flex:none;border-radius:var(--sara-r-sm);
  border:1px solid rgba(94,234,212,0.4);background:rgba(94,234,212,0.09);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 0 16px -4px rgba(94,234,212,0.4);
}
.sp-brand-dot{width:0.5rem;height:0.5rem;border-radius:9999px;background:var(--sara-accent)}
.sp-brand-text{
  font-family:var(--sara-font-mono);font-weight:600;font-size:0.8125rem;
  letter-spacing:0.16em;color:var(--sara-accent);white-space:nowrap;
}
.sp-collapse{
  font-size:0.6875rem;color:var(--sara-dim);border:1px solid var(--sara-line);
  border-radius:var(--sara-r-xs);padding:0.25rem 0.4rem;background:transparent;
  transition:color .2s,border-color .2s;flex:none;
}
.sp-collapse:hover{color:var(--sara-ink);border-color:var(--sara-line-strong)}

/* nav */
.sp-nav{flex:1;overflow-y:auto;padding:0.75rem;display:flex;flex-direction:column;gap:0.125rem}
.sp-nav-item{
  display:flex;align-items:center;gap:0.75rem;
  padding:0.625rem 0.625rem;border-radius:var(--sara-r-md);
  font-size:0.8125rem;font-weight:500;color:var(--sara-muted);
  transition:color .2s,background .2s,border-color .2s;
  border:1px solid transparent;
}
.sp-nav-item:hover{color:var(--sara-ink);background:var(--sara-surface-2)}
.sp-nav-active{
  color:var(--sara-accent);background:rgba(94,234,212,0.08);
  border-color:rgba(94,234,212,0.28);
}
.sp-nav-icon{width:1.125rem;height:1.125rem;flex:none;display:flex;align-items:center;justify-content:center}
.sp-nav-label{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

.sp-sidebar-foot{
  display:flex;align-items:center;gap:0.5rem;
  padding:0.875rem 1rem;border-top:1px solid var(--sara-line);
}
.sp-status-dot{
  width:0.5rem;height:0.5rem;border-radius:9999px;flex:none;
  background:var(--sara-ok);box-shadow:0 0 9px rgba(52,211,153,0.8);
}
.sp-status-text{font-family:var(--sara-font-mono);font-size:0.6875rem;color:var(--sara-dim)}

/* ═══ MAIN ═══ */
.sp-main{position:relative;z-index:10;flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden}
.sp-header{
  position:relative;z-index:20;flex:none;height:3.5rem;
  display:flex;align-items:center;justify-content:space-between;gap:1rem;
  padding:0 1.5rem;border-bottom:1px solid var(--sara-line);
  background:rgba(6,8,13,0.6);
  -webkit-backdrop-filter:blur(16px);backdrop-filter:blur(16px);
}
.sp-header-tag{
  font-family:var(--sara-font-mono);font-size:0.6875rem;font-weight:600;
  letter-spacing:0.16em;text-transform:uppercase;color:var(--sara-accent);
  padding:0.3125rem 0.625rem;border-radius:var(--sara-r-xs);
  border:1px solid rgba(94,234,212,0.26);background:rgba(94,234,212,0.07);
}
.sp-header-meta{display:flex;align-items:center;gap:0.4375rem;font-size:0.75rem;color:var(--sara-dim)}
.sp-header-meta strong{font-family:var(--sara-font-mono);color:var(--sara-ink-2);font-weight:600}

.sp-content{
  position:relative;z-index:10;flex:1;overflow-y:auto;
  padding:clamp(1.125rem,2.4vw,1.75rem);
}

@media (max-width:720px){
  .sp-sidebar-open{width:4.5rem}
  .sp-nav-label{display:none}
  .sp-brand-text{display:none}
  .sp-status-text{display:none}
  .sp-header{padding:0 0.875rem}
  .sp-header-tag{font-size:0.625rem;letter-spacing:0.1em}
  .sp-header-meta{display:none}
}

/* ═══ SHARED CONTENT PRIMITIVES (used by every platform page) ═══ */
.sp-page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-bottom:1.5rem}
.sp-page-title{margin:0;font-size:clamp(1.375rem,2.6vw,1.75rem);font-weight:600;letter-spacing:-0.022em;color:var(--sara-ink)}
.sp-page-sub{margin:0.375rem 0 0;font-size:0.8125rem;line-height:1.6;color:var(--sara-muted)}
.sp-grid{display:grid;gap:1rem}
.sp-cols-4{grid-template-columns:repeat(1,minmax(0,1fr))}
.sp-cols-2{grid-template-columns:repeat(1,minmax(0,1fr))}
@media (min-width:760px){.sp-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (min-width:900px){.sp-cols-4{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (min-width:1280px){.sp-cols-4{grid-template-columns:repeat(4,minmax(0,1fr))}}

.sp-card{
  border-radius:var(--sara-r-xl);border:1px solid var(--sara-line);
  background:linear-gradient(165deg,rgba(255,255,255,0.042),rgba(255,255,255,0.014));
  -webkit-backdrop-filter:blur(16px) saturate(118%);backdrop-filter:blur(16px) saturate(118%);
  box-shadow:var(--sara-sh-2);padding:1.25rem;
}
.sp-card-title{margin:0;font-size:0.9375rem;font-weight:600;color:var(--sara-ink)}
.sp-card-note{margin:0.5rem 0 0;font-size:0.75rem;line-height:1.6;color:var(--sara-dim)}

.sp-metric-label{
  font-family:var(--sara-font-mono);font-size:0.625rem;font-weight:600;
  letter-spacing:0.14em;text-transform:uppercase;color:var(--sara-dim);margin-bottom:0.5rem;
}
.sp-metric-value{font-size:1.875rem;font-weight:600;letter-spacing:-0.03em;line-height:1;color:var(--sara-ink)}
.sp-metric-value.is-ok{color:var(--sara-ok)}
.sp-metric-value.is-info{color:var(--sara-info)}
.sp-metric-value.is-accent{color:var(--sara-accent)}

.sp-row-item{
  display:flex;align-items:center;justify-content:space-between;gap:0.75rem;
  padding:0.75rem 0.875rem;border-radius:var(--sara-r-md);
  border:1px solid var(--sara-line-soft);background:rgba(255,255,255,0.022);
}
.sp-row-item + .sp-row-item{margin-top:0.375rem}
.sp-row-text{font-size:0.8125rem;color:var(--sara-ink-2);min-width:0}
.sp-row-time{font-family:var(--sara-font-mono);font-size:0.6875rem;color:var(--sara-dim);flex:none}

.sp-table{width:100%;border-collapse:collapse;font-size:0.8125rem}
.sp-table th{
  text-align:left;padding:0.625rem 0.75rem;font-family:var(--sara-font-mono);
  font-size:0.625rem;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;
  color:var(--sara-dim);border-bottom:1px solid var(--sara-line);white-space:nowrap;
}
.sp-table td{padding:0.625rem 0.75rem;border-bottom:1px solid var(--sara-line-soft);color:var(--sara-ink-2)}
.sp-table tr:last-child td{border-bottom:0}
.sp-table-wrap{overflow-x:auto}

.sp-field-label{
  display:block;font-family:var(--sara-font-mono);font-size:0.625rem;font-weight:600;
  letter-spacing:0.14em;text-transform:uppercase;color:var(--sara-dim);margin-bottom:0.375rem;
}

/* ═══ DROPZONE ═══ */
.sp-dropzone{
  position:relative;border-style:dashed;
  padding:2.25rem 1.5rem;text-align:center;
  transition:border-color .3s,background .3s;
}
.sp-dropzone:hover{border-color:rgba(94,234,212,0.45);background:rgba(94,234,212,0.03)}
.sp-dropzone-input{
  position:absolute;inset:0;width:100%;height:100%;
  opacity:0;cursor:pointer;z-index:5;
}
.sp-dropzone-inner{pointer-events:none;position:relative;z-index:2;display:flex;flex-direction:column;align-items:center;gap:0.5rem}
.sp-dropzone-mark{
  width:3rem;height:3rem;border-radius:var(--sara-r-lg);
  border:1px solid rgba(94,234,212,0.32);background:rgba(94,234,212,0.08);
  color:var(--sara-accent);display:flex;align-items:center;justify-content:center;
  margin-bottom:0.25rem;
}
.sp-dropzone-busy{
  display:flex;align-items:center;gap:0.625rem;margin-top:0.75rem;
  font-family:var(--sara-font-mono);font-size:0.75rem;color:var(--sara-accent);
}

/* ═══ TABS ═══ */
.sp-tabs{
  display:flex;gap:0.375rem;flex-wrap:wrap;
  padding-bottom:0.875rem;border-bottom:1px solid var(--sara-line);
}
.sp-tab{
  font:inherit;font-size:0.8125rem;font-weight:600;cursor:pointer;
  padding:0.5rem 0.875rem;border-radius:var(--sara-r-sm);
  border:1px solid transparent;color:var(--sara-muted);
  transition:color .2s,background .2s,border-color .2s;
}
.sp-tab:hover{color:var(--sara-ink);background:var(--sara-surface-2)}
.sp-tab-on{
  color:var(--sara-accent);border-color:rgba(94,234,212,0.32);
  background:rgba(94,234,212,0.08);
}

/* ═══ VOICE ═══ */
.sp-voice-card{display:flex;flex-direction:column;align-items:center;gap:1.25rem;padding:2.5rem 1.5rem;text-align:center}
.sp-voice-btn{
  width:6rem;height:6rem;border-radius:9999px;flex:none;
  display:flex;align-items:center;justify-content:center;
  border:1px solid rgba(94,234,212,0.34);color:var(--sara-accent);
  background:linear-gradient(165deg,rgba(94,234,212,0.14),rgba(94,234,212,0.04));
  box-shadow:0 0 0 1px rgba(94,234,212,0.14),0 18px 40px -16px rgba(45,212,191,0.55);
  transition:transform .3s var(--sara-ease-spring),box-shadow .3s,background .3s,color .3s;
}
.sp-voice-btn:hover{transform:scale(1.05)}
.sp-voice-rec{
  color:#fff;border-color:rgba(248,113,113,0.6);
  background:linear-gradient(165deg,rgba(248,113,113,0.28),rgba(248,113,113,0.08));
  box-shadow:0 0 0 1px rgba(248,113,113,0.3),0 0 34px -6px rgba(248,113,113,0.6);
  transform:scale(1.1);
}
.sp-voice-state{
  margin:0;font-family:var(--sara-font-mono);font-size:0.6875rem;font-weight:600;
  letter-spacing:0.2em;text-transform:uppercase;color:var(--sara-dim);
}
.sp-transcript{
  padding:1rem 1.125rem;border-radius:var(--sara-r-md);
  border:1px solid var(--sara-line-soft);background:rgba(255,255,255,0.022);
  display:flex;flex-direction:column;gap:0.5rem;
}
.sp-transcript-head{display:flex;align-items:center;justify-content:space-between;gap:0.75rem;flex-wrap:wrap}
.sp-transcript-id{font-size:0.6875rem;color:var(--sara-dim)}
.sp-transcript-text{margin:0;font-size:0.875rem;color:var(--sara-ink-2);line-height:1.6}
.sp-transcript-meta{
  margin:0;font-family:var(--sara-font-mono);font-size:0.6875rem;color:var(--sara-accent);
}

/* ═══ LOG ROWS ═══ */
.sp-log-row{
  display:flex;align-items:center;gap:0.875rem;flex-wrap:wrap;
  padding:0.75rem 0.875rem;border-radius:var(--sara-r-md);
  border:1px solid var(--sara-line-soft);background:rgba(255,255,255,0.022);
  font-family:var(--sara-font-mono);font-size:0.75rem;
}
.sp-log-time{color:var(--sara-dim);width:5rem;flex:none}
.sp-log-source{
  color:var(--sara-muted);width:9.5rem;flex:none;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.sp-log-msg{color:var(--sara-ink-2);flex:1;min-width:12rem}

/* ═══ KEY/VALUE + ACTIONS ═══ */
.sp-kv{
  padding:0.875rem 1rem;border-radius:var(--sara-r-md);
  border:1px solid var(--sara-line-soft);background:rgba(255,255,255,0.022);
}
.sp-kv-value{
  margin:0;font-family:var(--sara-font-mono);font-size:0.8125rem;font-weight:600;
}
.sp-kv-value.is-accent{color:var(--sara-accent)}
.sp-kv-value.is-ok{color:var(--sara-ok)}
.sp-actions{display:flex;gap:0.625rem;flex-wrap:wrap;margin-top:1.25rem;padding-top:1.25rem;border-top:1px solid var(--sara-line)}
.sp-test-result{
  margin:1rem 0 0;font-size:0.8125rem;line-height:1.6;padding:0.75rem 0.875rem;
  border-radius:var(--sara-r-md);border:1px solid;
}
.sp-test-result.is-ok{color:var(--sara-ok);border-color:rgba(52,211,153,0.3);background:var(--sara-ok-bg)}
.sp-test-result.is-err{color:var(--sara-err);border-color:rgba(248,113,113,0.3);background:var(--sara-err-bg)}
`;
