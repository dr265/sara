const fs = require('fs');
const file = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/edulearner-ai/page.tsx';
let content = fs.readFileSync(file, 'utf8');

const headEndIdx = content.indexOf('  if (!dashboard) {');
const tabpanelIdx = content.indexOf('      <section\n        role="tabpanel"');

if (headEndIdx === -1 || tabpanelIdx === -1) {
    console.log('Could not find markers');
    process.exit(1);
}

const head = content.substring(0, headEndIdx);
const tail = content.substring(tabpanelIdx); // This starts with <section role="tabpanel"...

// We need to inject AuraBackground at the top of the file just before export default function
let newHead = head;
if (!newHead.includes('function AuraBackground')) {
    const auraCode = `\nfunction AuraBackground() {
  return (
    <div className="absolute inset-0 overflow-hidden pointer-events-none" style={{ zIndex: 0 }}>
      <div className="absolute -top-[20%] -left-[10%] w-[70%] h-[70%] rounded-full opacity-20 blur-[120px] mix-blend-screen" style={{ backgroundImage: "radial-gradient(circle at 50% 50%, #3D81E3, transparent)" }} />
      <div className="absolute top-[40%] -right-[10%] w-[60%] h-[60%] rounded-full opacity-10 blur-[120px] mix-blend-screen" style={{ backgroundImage: "radial-gradient(circle at 50% 50%, #8A2BE2, transparent)" }} />
      <div className="absolute -bottom-[20%] left-[20%] w-[80%] h-[80%] rounded-full opacity-15 blur-[150px] mix-blend-screen" style={{ backgroundImage: "radial-gradient(circle at 50% 50%, #A4F4FD, transparent)" }} />
      <div className="absolute inset-0 opacity-[0.03]" style={{ backgroundImage: "url('data:image/svg+xml,%3Csvg viewBox=%220 0 200 200%22 xmlns=%22http://www.w3.org/2000/svg%22%3E%3Cfilter id=%22noiseFilter%22%3E%3CfeTurbulence type=%22fractalNoise%22 baseFrequency=%220.65%22 numOctaves=%223%22 stitchTiles=%22stitch%22/%3E%3C/filter%3E%3Crect width=%22100%25%22 height=%22100%25%22 filter=%22url(%23noiseFilter)%22/%3E%3C/svg%3E')" }} />
    </div>
  );
}\n\n`;
    const exportIdx = newHead.indexOf('export default function');
    newHead = newHead.substring(0, exportIdx) + auraCode + newHead.substring(exportIdx);
}

// Modify STYLES to be more premium
newHead = newHead.replace(
  '  background: linear-gradient(160deg, rgba(20,28,46,.66), rgba(10,14,24,.52));',
  '  background: linear-gradient(160deg, rgba(20,28,46,.4), rgba(10,14,24,.3));'
);
newHead = newHead.replace(
  '  -webkit-backdrop-filter: blur(20px) saturate(130%); backdrop-filter: blur(20px) saturate(130%);',
  '  -webkit-backdrop-filter: blur(30px) saturate(150%); backdrop-filter: blur(30px) saturate(150%);'
);

// We need Apple icon imported
if (!newHead.includes('import { Apple ')) {
  newHead = newHead.replace(/import \{([^}]+)\} from "lucide-react";/, 'import { $1, Apple } from "lucide-react";');
}

const newReturn = `
  return (
    <main className="relative min-h-screen w-full bg-[#0c0c0c] text-white" style={{ fontFamily: "'Inter', system-ui, sans-serif" }}>
      <style>{STYLES}</style>
      <AuraBackground />
      
      {/* MAC-OS STYLE MENU BAR */}
      <div className="relative z-20 w-full h-10 bg-black/40 backdrop-blur-md border-b border-white/10 flex items-center justify-between px-6 text-xs text-white/70">
        <div className="flex items-center gap-4 font-medium">
          <Apple className="w-3.5 h-3.5 text-white" />
          <span className="font-bold text-white">Aura Core</span>
        </div>
        <div className="flex items-center gap-4">
           <span>{new Date().toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</span>
        </div>
      </div>

      <div className="relative z-10 mx-auto max-w-[1400px] px-4 sm:px-6 lg:px-8 py-10">
        
        {/* HERO SECTION */}
        <div className="text-center mb-16 flex flex-col items-center">
           <h1 className="text-4xl md:text-6xl font-semibold tracking-tight leading-[0.9]">
             EduLearner.<br/>
             <span className="animate-shiny bg-clip-text text-transparent inline-block mt-2 pb-2" style={{ backgroundImage: "linear-gradient(to right, #091020 0%, #0B2551 12.5%, #A4F4FD 32.5%, #00d2ff 50%, #0B2551 67.5%, #091020 87.5%, #091020 100%)", backgroundSize: "200% auto", filter: "url(#c3-noise)" }}>
               Intelligence
             </span>
           </h1>
           <p className="mt-6 text-white/60 max-w-xl text-base leading-[1.5]">
             An agentic adaptive-learning engine: it reads evidenced skill gaps, re-plans your roadmap, marks your practice, and records exactly why every change was made — 13 specialised agents in one auditable loop.
           </p>
        </div>

        {/* MOCKUP INTERFACE (FULLY EXPANDABLE) */}
        <section className="relative rounded-2xl border border-white/10 bg-[#0e1014]/90 backdrop-blur-2xl shadow-2xl flex flex-col mb-32 overflow-hidden">
          {/* Mac window title bar */}
          <div className="h-10 bg-black/50 border-b border-white/10 flex items-center px-4 shrink-0 relative">
             <div className="flex gap-2">
                <div className="w-3 h-3 rounded-full bg-[#ff5f57]" />
                <div className="w-3 h-3 rounded-full bg-[#febc2e]" />
                <div className="w-3 h-3 rounded-full bg-[#28c840]" />
             </div>
             <div className="absolute inset-0 flex items-center justify-center text-xs text-white/50 font-medium pointer-events-none">
                Aura — EduLearner AI
             </div>
          </div>

          <div className="flex-1 grid grid-cols-12 min-h-[800px]">
             
             {/* LEFT SIDEBAR: Navigation & Status */}
             <div className="col-span-12 md:col-span-3 border-b md:border-b-0 md:border-r border-white/10 bg-black/30 flex flex-col">
                <div className="p-4 border-b border-white/10 shrink-0 bg-black/40">
                   <div className="text-[10px] font-mono uppercase tracking-widest text-white/40 mb-3">Engine Status</div>
                   <div className="flex flex-col gap-2">
                     <div className="flex items-center gap-2 text-xs text-white/80"><div className={\`w-2 h-2 rounded-full \${registry ? 'bg-emerald-400' : 'bg-amber-400 animate-pulse'}\`} /> {registry ? "Online" : "Connecting..."}</div>
                     <div className="text-xs text-white/50 font-mono">db: {registry?.database ?? "—"}</div>
                     <div className="text-xs text-white/50 font-mono">llm: {registry?.llm ?? "—"}</div>
                     {dashboard?.memory?.current_bottleneck && <div className="text-xs text-white/50 font-mono truncate">top gap: {dashboard.memory.current_bottleneck}</div>}
                   </div>
                </div>
                
                <div className="flex-1 p-3 space-y-1">
                   <div className="text-[10px] font-mono text-white/30 uppercase tracking-widest px-3 mb-2 mt-2">Workspace Modules</div>
                   {TABS.map(({ id, label, icon: Icon }) => (
                     <button
                       key={id}
                       disabled={!dashboard}
                       onClick={() => setTab(id)}
                       className={\`w-full flex items-center gap-3 text-left px-3 py-2.5 rounded-lg text-sm transition-all \${tab === id ? 'bg-white/10 text-white shadow-sm font-medium' : 'text-white/50 hover:bg-white/5 hover:text-white/90'} \${!dashboard ? 'opacity-30 cursor-not-allowed' : ''}\`}
                     >
                       <Icon size={16} /> <span>{label}</span>
                     </button>
                   ))}
                </div>

                <div className="p-4 border-t border-white/10 shrink-0 bg-black/40 text-xs text-white/50 space-y-2">
                   <div>Learner: <span className="text-white/90">{userId}</span></div>
                   {dashboard && <div>Profile: <span className="text-emerald-400">{dashboard.profile_completion}%</span></div>}
                </div>
             </div>

             {/* RIGHT COLUMN: Toolbar & Panel */}
             <div className="col-span-12 md:col-span-9 flex flex-col bg-[#0e1014]/20">
                {!dashboard ? (
                   <div className="flex-1 flex flex-col items-center justify-center p-8 text-center">
                      <div className="w-16 h-16 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center mb-6">
                         <User size={32} className="text-white/30" />
                      </div>
                      <h2 className="text-xl font-semibold mb-2">Connect a Learner</h2>
                      <p className="text-white/50 max-w-sm mb-8 text-sm">Enter a Learner ID to pull evidence, analyze gaps, and adapt the curriculum.</p>
                      <div className="flex gap-2">
                        <input
                          className="el-input" style={{ width: 200 }} value={userId}
                          onChange={(event) => setUserId(event.target.value)}
                          placeholder="learner id" aria-label="Learner id"
                        />
                        <button className="el-btn primary" onClick={() => onboard(true)} disabled={busy}>
                          {loading === "onboard" ? <Loader2 size={15} className="el-spin" /> : <Sparkles size={15} />}
                          Run analysis
                        </button>
                      </div>
                      {(error || notice) && (
                        <div className="mt-8">
                          {error && <div className="el-banner err" role="alert"><XCircle size={15} /><span>{error}</span></div>}
                          {notice && !error && <div className="el-banner ok" role="status"><CheckCircle2 size={15} /><span>{notice}</span></div>}
                        </div>
                      )}
                   </div>
                ) : (
                   <>
                      {/* Top Action Toolbar */}
                      <div className="h-14 border-b border-white/10 flex items-center justify-between px-6 shrink-0 bg-black/20">
                         <div className="flex items-center gap-2">
                           <button className="el-btn sm" onClick={() => onboard(false)} disabled={busy}>
                             <RefreshCw size={14} /> Re-analyse
                           </button>
                           <button className="el-btn sm" onClick={planToday} disabled={busy}>
                             {loading === "plan" ? <Loader2 size={14} className="el-spin" /> : <CalendarCheck size={14} />} Plan today
                           </button>
                         </div>
                         <button className="el-btn sm" onClick={generateReport} disabled={busy || !dashboard}>
                           {loading === "report" ? <Loader2 size={14} className="el-spin" /> : <Activity size={14} />} Report
                         </button>
                      </div>
                      
                      {/* Notices */}
                      {(error || notice) && (
                        <div className="p-6 pb-0">
                          {error && <div className="el-banner err" role="alert"><XCircle size={15} /><span>{error}</span></div>}
                          {notice && !error && <div className="el-banner ok" role="status"><CheckCircle2 size={15} /><span>{notice}</span></div>}
                        </div>
                      )}

                      {/* Main Tab Panel */}
                      <div className="flex-1 p-6">
`;

// Now we need to modify the tail so that it closes the right column and section and main properly.
// The tail starts with <section role="tabpanel"... and ends with `</section>\n    </div>\n  );\n}`
let newTail = tail;
newTail = newTail.replace(
  '      </section>\n    </div>\n  );\n}',
  '                      </div>\n                   </>\n                )}\n             </div>\n          </div>\n        </section>\n      </div>\n    </main>\n  );\n}'
);
// Also modify the tabpanel <section> to have no padding or margin since we handle it in our wrapper
newTail = newTail.replace('className="el-grid"\n        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(330px, 1fr))" }}', 'className="el-grid"\n        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(330px, 1fr))" }}');


const finalContent = newHead + newReturn + newTail;
fs.writeFileSync(file, finalContent, 'utf8');
console.log('Successfully transformed EduLearner AI!');
