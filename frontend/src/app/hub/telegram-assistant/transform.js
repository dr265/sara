const fs = require('fs');

const tgPath = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/telegram-assistant/page.tsx';
let tgContent = fs.readFileSync(tgPath, 'utf8');

// 1. Get WhatsApp CSS
const waPath = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/whatsapp-agent/page.tsx';
const waContent = fs.readFileSync(waPath, 'utf8');
const searchStr = 'const css = `';
const cssStart = waContent.indexOf(searchStr);
const cssEnd = waContent.indexOf('`;', cssStart);
const css = waContent.substring(cssStart, cssEnd + 2);

// 2. Modify CSS to use cyan/telegram colors instead of whatsapp green
// Actually, let's keep it mostly similar, but maybe change the neon color to cyan.
// Or just keep it as is, the user said "same ui". Let's stick to the exact same css.
let newCss = css.replace(/10,\s*200,\s*80/g, '0, 200, 255'); // Change green rgb(10,200,80) to cyan
newCss = newCss.replace(/#0ac850/g, '#00c8ff'); // Change hex green to cyan

// 3. Inject CSS into telegram-assistant/page.tsx
// Before export default function TelegramAssistantPage
const exportIdx = tgContent.indexOf('export default function TelegramAssistantPage');
tgContent = tgContent.substring(0, exportIdx) + '\n' + newCss + '\n\n' + tgContent.substring(exportIdx);

// 4. Find the main return block
const returnIdx = tgContent.indexOf('  return (\n    <div className="relative flex');

// 5. Replace with new UI
const newReturn = `  return (
    <div className="sara relative">
      <style dangerouslySetInnerHTML={{ __html: css }} />
      
      {/* Nav Sidebar (mimicking WhatsApp Agent) */}
      <header className="fixed inset-x-0 top-0 z-50 transition-[background,border-color,backdrop-filter] duration-300 border-b border-white/5 bg-[#0b0d10]/80 backdrop-blur-md">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 flex h-16 items-center justify-between">
          <a href="/hub" className="flex items-center gap-2.5" aria-label="Back to hub">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-gradient-to-br from-white/10 to-white/0 border border-white/10 shadow-[0_2px_10px_rgba(0,0,0,0.4)]">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-white opacity-80"><path d="m15 18-6-6 6-6"/></svg>
            </span>
            <span className="text-[13px] font-semibold tracking-wide text-white/90">HUB</span>
          </a>
          <nav className="flex items-center gap-1 bg-white/[0.03] p-1 rounded-lg border border-white/5">
            {['console', 'knowledge', 'data'].map((t) => (
              <button
                key={t}
                onClick={() => setActiveTab(t as any)}
                className={\`px-4 py-1.5 rounded-md text-xs font-medium uppercase tracking-wider transition-all \${activeTab === t ? 'bg-white/10 text-white shadow-sm' : 'text-white/40 hover:text-white/70 hover:bg-white/5'}\`}
              >
                {t === 'console' ? 'Console' : t === 'knowledge' ? 'Knowledge' : 'Data & Sync'}
              </button>
            ))}
          </nav>
        </div>
      </header>

      <main className="pt-24 pb-32">
        {/* Hero Section */}
        <section className="relative mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 mb-12" data-reveal="0">
           <div className="flex flex-col md:flex-row md:items-end justify-between gap-6 border-b border-white/10 pb-8">
              <div>
                 <div className="inline-flex items-center gap-2 px-2.5 py-1 rounded-full bg-white/5 border border-white/10 text-[10px] font-mono tracking-widest text-white/50 mb-6">
                    <span className="w-1.5 h-1.5 rounded-full bg-[#00c8ff] animate-pulse"></span>
                    TELEGRAM_NODE // SARA
                 </div>
                 <h1 className="text-4xl md:text-5xl font-light tracking-tight text-white mb-2">
                    Telegram <span className="font-semibold text-transparent bg-clip-text bg-gradient-to-r from-white to-white/40">Agent</span>
                 </h1>
                 <p className="text-sm font-mono text-white/40 mt-4">TENANT: {tenant || 'Bootstrapping...'}</p>
              </div>
              <div className="flex items-center gap-4">
                <button
                  onClick={toggleAgentPower}
                  disabled={agentBusy || agentRunning === null}
                  className={\`el-btn \${agentRunning === false ? 'err' : 'primary'}\`}
                  style={{ '--btn-hue': agentRunning === false ? '0' : '190' } as any}
                >
                  {agentBusy ? 'WORKING...' : agentRunning ? 'AGENT RUNNING' : 'AGENT PAUSED'}
                </button>
              </div>
           </div>
        </section>

        {/* Main Content Area */}
        <section className="relative mx-auto max-w-7xl px-4 sm:px-6 lg:px-8" data-reveal="100">
          <div className="el-glass min-h-[600px] overflow-hidden flex flex-col">
            
            {activeTab === 'console' && (
              <div className="flex-1 flex flex-col p-6 h-[600px]">
                {/* Bot Token Configuration */}
                <div className="mb-6 flex flex-wrap items-center gap-3 rounded-xl border border-white/10 bg-black/40 px-5 py-4">
                  <span className="text-xs font-bold tracking-widest text-[#00c8ff]">YOUR BOT</span>
                  <span className="text-xs text-white/60 font-mono">
                    {botInfo ? (botInfo.bot_username ? \`@\${botInfo.bot_username}\` : 'Active') : 'Checking...'}
                  </span>
                  <input
                    type="password"
                    value={botTokenInput}
                    onChange={(e) => setBotTokenInput(e.target.value)}
                    placeholder="Paste bot token from @BotFather..."
                    className="flex-1 min-w-[200px] bg-black/50 border border-white/10 rounded-lg px-3 py-1.5 text-xs text-white placeholder:text-white/20 focus:outline-none focus:border-[#00c8ff]/50 transition-colors"
                  />
                  <button
                    onClick={configureBot}
                    disabled={botBusy || !botTokenInput.trim()}
                    className="px-4 py-1.5 rounded-lg bg-white/5 border border-white/10 text-xs font-medium hover:bg-white/10 transition-colors disabled:opacity-50"
                  >
                    AUTHORIZE & START
                  </button>
                  {botNotice && (
                    <span className={\`text-xs \${botNotice.kind === 'ok' ? 'text-green-400' : 'text-red-400'}\`}>
                      {botNotice.text}
                    </span>
                  )}
                </div>

                {/* Messages Window */}
                <div className="flex-1 overflow-y-auto mb-4 space-y-3 pr-2 scrollbar-thin scrollbar-thumb-white/10">
                  {messages.map((m, i) => (
                    <div key={i} className={\`flex \${m.role === 'user' ? 'justify-end' : 'justify-start'}\`}>
                      <div className={\`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm shadow-lg \${
                        m.role === 'user'
                          ? 'bg-[#00c8ff]/20 border border-[#00c8ff]/30 text-white rounded-br-none'
                          : 'bg-white/5 border border-white/10 text-white/80 rounded-bl-none'
                      }\`}>
                        {m.content}
                      </div>
                    </div>
                  ))}
                </div>

                {/* Input Area */}
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && handleCommand()}
                    placeholder="Issue a command..."
                    className="flex-1 bg-black/40 border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:outline-none focus:border-[#00c8ff]/50 transition-colors shadow-inner"
                  />
                  <button
                    onClick={() => {}}
                    className="w-12 rounded-xl bg-white/5 border border-white/10 flex items-center justify-center hover:bg-white/10 transition-colors"
                    title="Voice input (mock)"
                  >
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-white/60"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
                  </button>
                  <button
                    onClick={handleCommand}
                    className="px-6 rounded-xl bg-[#00c8ff]/20 border border-[#00c8ff]/30 text-[#00c8ff] text-xs font-bold tracking-widest hover:bg-[#00c8ff]/30 transition-colors shadow-[0_0_15px_rgba(0,200,255,0.2)]"
                  >
                    EXECUTE
                  </button>
                </div>
              </div>
            )}

            {activeTab === 'knowledge' && (
              <div className="p-8 h-[600px] overflow-y-auto">
                <div className="flex justify-between items-center mb-8 border-b border-white/10 pb-4">
                  <h2 className="text-xl font-light">Knowledge Base</h2>
                  <div className="relative">
                    <input type="file" onChange={uploadDoc} className="absolute inset-0 w-full h-full opacity-0 cursor-pointer" />
                    <button disabled={isSyncing} className="px-4 py-2 rounded-lg bg-white/5 border border-white/10 text-xs font-medium hover:bg-white/10 transition-colors flex items-center gap-2">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/></svg>
                      {isSyncing ? 'UPLOADING...' : 'UPLOAD DOC'}
                    </button>
                  </div>
                </div>
                
                <div className="space-y-3">
                  {docs.length === 0 ? (
                    <div className="text-center py-12 text-white/30 text-sm border border-dashed border-white/10 rounded-xl">No documents indexed yet.</div>
                  ) : (
                    docs.map((d, i) => (
                      <div key={i} className="flex items-center justify-between p-4 rounded-xl border border-white/10 bg-black/20 hover:bg-black/40 transition-colors">
                        <div className="flex items-center gap-4">
                          <div className="w-10 h-10 rounded-lg bg-[#00c8ff]/10 flex items-center justify-center border border-[#00c8ff]/20 text-[#00c8ff]">
                             {d.ext?.replace('.','') || 'DOC'}
                          </div>
                          <div>
                            <div className="font-medium text-sm text-white/90">{d.filename || d.id}</div>
                            <div className="text-xs text-white/40 mt-1 uppercase tracking-wider">{Math.round((d.size || 0)/1024)} KB · {d.status}</div>
                          </div>
                        </div>
                        <button onClick={() => deleteDoc(d.id)} className="p-2 rounded-lg hover:bg-red-500/20 text-white/30 hover:text-red-400 transition-colors">
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
                        </button>
                      </div>
                    ))
                  )}
                </div>
              </div>
            )}

            {activeTab === 'data' && (
              <div className="p-8 h-[600px] overflow-y-auto">
                <div className="flex justify-between items-center mb-8 border-b border-white/10 pb-4">
                  <h2 className="text-xl font-light">Data & Sync</h2>
                  <button onClick={isGoogleConnected ? syncSheets : connectGoogle} disabled={isSyncing} className="px-4 py-2 rounded-lg bg-emerald-500/20 border border-emerald-500/30 text-emerald-400 text-xs font-bold tracking-widest hover:bg-emerald-500/30 transition-colors">
                    {isSyncing ? 'SYNCING...' : (isGoogleConnected ? 'SYNC NOW' : 'CONNECT GOOGLE')}
                  </button>
                </div>
                
                <div className="space-y-4">
                  {bookings.length === 0 ? (
                     <div className="text-center py-12 text-white/30 text-sm border border-dashed border-white/10 rounded-xl">No sync history available.</div>
                  ) : (
                    bookings.map((b, i) => (
                      <div key={i} className="p-4 rounded-xl border border-white/10 bg-black/20 text-sm font-mono text-white/70">
                         {b.id ? \`Booking \${b.id} - \${b.status}\` : JSON.stringify(b)}
                      </div>
                    ))
                  )}
                </div>
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
`;

tgContent = tgContent.substring(0, returnIdx) + newReturn;

fs.writeFileSync(tgPath, tgContent, 'utf8');
console.log('Done transforming Telegram!');
