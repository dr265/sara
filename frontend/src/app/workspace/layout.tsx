"use client";

import Link from "next/link";
import { useSearchParams, useRouter } from "next/navigation";
import { useEffect, useState, useRef, Suspense } from "react";
import "@/styles/sara.css"; // shared SARA design tokens + utilities

export interface LocalChat {
  id: string;
  title: string;
  updatedAt: number;
}

/* ── Icon set ────────────────────────────────────────────────────────────── */
const I = {
  plus: (s = 16) => (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14" /></svg>
  ),
  dots: (s = 16) => (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.4} strokeLinecap="round"><circle cx="12" cy="12" r="1" /><circle cx="12" cy="5" r="1" /><circle cx="12" cy="19" r="1" /></svg>
  ),
  mail: (s = 16) => (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="4" width="20" height="16" rx="2" /><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7" /></svg>
  ),
  close: (s = 14) => (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
  ),
  copy: (s = 14) => (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9} strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
  ),
  export: (s = 14) => (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9} strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
  ),
  trash: (s = 14) => (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9} strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>
  ),
  pdf: (s = 16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>,
  voice: (s = 16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg>,
  universal: (s = 16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/></svg>,
  hologram: (s = 16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><polygon points="12 2 22 8.5 22 15.5 12 22 2 15.5 2 8.5 12 2"/><line x1="12" y1="22" x2="12" y2="15.5"/><polyline points="22 8.5 12 15.5 2 8.5"/><polyline points="2 15.5 12 8.5 22 15.5"/><line x1="12" y1="2" x2="12" y2="8.5"/></svg>,
  desktop: (s = 16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>,
  chat: (s = 16) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>,
};

function NavItem({
  href,
  label,
  glyph,
  active = false,
  accent = false,
  onNav,
}: {
  href: string;
  label: string;
  glyph: React.ReactNode;
  active?: boolean;
  accent?: boolean;
  onNav?: () => void;
}) {
  return (
    <Link
      href={href}
      onClick={onNav}
      className={`group relative flex items-center gap-3 rounded-[12px] px-3 py-2.5 text-[13.5px] font-medium transition-all duration-300 ${
        active
          ? "bg-[rgba(255,255,255,0.06)] text-white shadow-[inset_0_0_0_1px_rgba(255,255,255,0.08)]"
          : accent
            ? "text-[var(--sara-accent)] hover:bg-[rgba(255,255,255,0.03)]"
            : "text-[#98999C] hover:bg-[rgba(255,255,255,0.03)] hover:text-white"
      }`}
    >
      {active && (
        <div className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-[60%] bg-white rounded-r-full shadow-[0_0_10px_rgba(255,255,255,0.5)]" />
      )}
      <span
        aria-hidden="true"
        className={`flex items-center justify-center transition-colors duration-300 ${
          active 
            ? "text-white" 
            : accent 
              ? "text-[var(--sara-accent)]" 
              : "text-[#52525b] group-hover:text-[#98999C]"
        }`}
      >
        {glyph}
      </span>
      <span className="truncate">{label}</span>
    </Link>
  );
}

function SidebarContent({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const currentChatId = searchParams.get("chatId");

  const [recentChats, setRecentChats] = useState<LocalChat[]>([]);
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const loadChats = () => {
    try {
      const stored = localStorage.getItem("sara_chats");
      if (stored) {
        const parsed = JSON.parse(stored);
        setRecentChats(parsed.sort((a: LocalChat, b: LocalChat) => b.updatedAt - a.updatedAt));
      }
    } catch (e) { console.error(e); }
  };

  useEffect(() => {
    loadChats();
    window.addEventListener("chatsUpdated", loadChats);
    return () => window.removeEventListener("chatsUpdated", loadChats);
  }, []);

  // Close the dropdown menu if the user clicks anywhere outside of it
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setOpenMenuId(null);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [menuRef]);

  const startNewChat = () => {
    router.push("/workspace");
    setMobileNavOpen(false);
  };

  // --- GMAIL CONNECTION HANDLER (unchanged behaviour) ---
  const handleConnectGmail = () => {
    const clientId = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID;
    // Google only accepts a redirect URI registered for THIS origin, so a
    // hardcoded localhost works on a laptop and fails the moment the app is
    // deployed. Derive it from the request the browser actually made.
    const redirectUri =
      process.env.NEXT_PUBLIC_GMAIL_REDIRECT_URI ||
      (typeof window !== "undefined"
        ? `${window.location.origin}/auth/callback`
        : `${process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:3000"}/auth/callback`);
    const scope = encodeURIComponent("https://www.googleapis.com/auth/gmail.send");

    // Redirects user to Google's secure login screen
    window.location.href = `https://accounts.google.com/o/oauth2/v2/auth?client_id=${clientId}&redirect_uri=${redirectUri}&response_type=code&scope=${scope}&access_type=offline&prompt=consent`;
  };

  // --- THREE DOTS MENU ACTIONS (unchanged behaviour) ---
  const handleCopyLink = (id: string, e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const link = `${window.location.origin}/workspace?chatId=${id}`;
    navigator.clipboard.writeText(link);
    alert("Link copied to clipboard!");
    setOpenMenuId(null);
  };

  const handleExport = (chat: LocalChat, e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const content = `SARA AI Chat Export\nTitle: ${chat.title}\nDate: ${new Date(chat.updatedAt).toLocaleString()}\n\n(Note: Full message history export will be available once connected to the FastAPI message fetching endpoint.)`;
    const blob = new Blob([content], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `SARA_AI_${chat.title.replace(/[^a-z0-9]/gi, "_").toLowerCase()}.txt`;
    a.click();
    URL.revokeObjectURL(url);
    setOpenMenuId(null);
  };

  const handleDelete = (id: string, e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      const stored = localStorage.getItem("sara_chats");
      if (stored) {
        let parsed = JSON.parse(stored);
        parsed = parsed.filter((c: LocalChat) => c.id !== id);
        localStorage.setItem("sara_chats", JSON.stringify(parsed));
        window.dispatchEvent(new Event("chatsUpdated"));

        if (currentChatId === id) {
          router.push("/workspace");
        }
      }
    } catch (err) { console.error(err); }
    setOpenMenuId(null);
  };

  const isWorkspaceActive =
    !currentChatId && typeof window !== "undefined" && window.location.pathname === "/workspace";

  return (
    <div className="sara-grain relative flex h-[100dvh] w-full overflow-hidden bg-transparent text-[var(--sara-ink)]">
      {/* 1. GUARANTEED FULL SCREEN AMBIENT BACKGROUND (fluid engine preserved) */}
      <div className="fixed inset-0 z-[-1] pointer-events-none bg-[var(--sara-void)]">
        <FlowstateFluid />
        <div
          className="absolute inset-0"
          style={{
            background:
              "radial-gradient(115% 95% at 50% 46%, rgba(4,5,12,0.62) 0%, rgba(4,5,12,0.42) 40%, rgba(4,5,12,0.12) 100%)",
          }}
        />
      </div>
      {/* 2. SIDEBAR — static panel on desktop, slide-over drawer below md */}
      <aside
        className={`absolute inset-y-0 left-0 z-[60] flex w-[17.5rem] shrink-0 flex-col border-r border-[rgba(255,255,255,0.05)] bg-[rgba(16,16,18,0.6)] backdrop-blur-[30px] backdrop-saturate-[120%] transition-transform duration-[450ms] ease-[cubic-bezier(0.16,1,0.3,1)] md:static md:z-40 md:translate-x-0 ${
          mobileNavOpen ? "translate-x-0 shadow-[20px_0_40px_rgba(0,0,0,0.5)]" : "-translate-x-full md:translate-x-0"
        }`}
      >
        <div className="border-b border-[rgba(255,255,255,0.05)] px-5 py-6">
          <div className="flex items-center justify-between">
            <Link
              href="/"
              className="flex items-center gap-3 text-[14.5px] font-semibold uppercase tracking-[0.18em] text-white"
            >
              <span className="flex h-6 w-6 items-center justify-center rounded-full border border-white/80 shadow-[0_0_10px_rgba(255,255,255,0.2)]">
                <span className="h-2 w-2 rounded-full bg-white" />
              </span>
              SARA AI
            </Link>
            <button
              onClick={() => setMobileNavOpen(false)}
              className="flex h-8 w-8 items-center justify-center rounded-md text-[#52525B] hover:bg-[rgba(255,255,255,0.05)] hover:text-white md:hidden transition-colors"
              aria-label="Close navigation"
            >
              {I.close()}
            </button>
          </div>

          <button
            onClick={startNewChat}
            className="mt-7 flex w-full items-center justify-center gap-2 rounded-[14px] bg-[linear-gradient(180deg,rgba(40,40,42,1)_0%,rgba(25,25,27,1)_100%)] px-4 py-3 text-[13.5px] font-medium text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_4px_12px_rgba(0,0,0,0.4)] transition-all hover:brightness-110 active:scale-[0.98]"
          >
            {I.plus(15)}
            New Chat
          </button>
        </div>

        <div className="sara-scroll flex-1 overflow-y-auto px-2 py-6">
          <div className="px-3 pb-3 font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-[#52525B]">
            AI Agents
          </div>

          <nav className="mb-8 flex flex-col gap-1.5 px-1">
            <NavItem href="/workspace" label="PDF Analyst" glyph={I.pdf(15)} active={isWorkspaceActive} onNav={() => setMobileNavOpen(false)} />
            <NavItem href="/workspace/voice-agent" label="Voice Interface" glyph={I.voice(15)} onNav={() => setMobileNavOpen(false)} />
            <NavItem href="/workspace/agent-creator" label="Universal Voice Agent" glyph={I.universal(15)} accent onNav={() => setMobileNavOpen(false)} />
            <NavItem href="/workspace/sara-orb" label="Hologram Interface" glyph={I.hologram(15)} onNav={() => setMobileNavOpen(false)} />
            <NavItem href="/workspace/desktop-agent" label="Live Desktop Agent" glyph={I.desktop(15)} onNav={() => setMobileNavOpen(false)} />
          </nav>

          <div className="flex items-center justify-between px-3 pb-3">
            <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-[#52525B]">
              Recent Chats
            </span>
            {recentChats.length > 0 && (
              <span className="font-mono text-[10px] text-[#52525B]">
                {recentChats.length}
              </span>
            )}
          </div>
          <div className="flex flex-col gap-1 px-1">
            {recentChats.length === 0 ? (
              <div className="px-3 py-2 text-[12.5px] italic text-[#52525B]">
                No recent chats
              </div>
            ) : (
              recentChats.map((chat) => (
                <div key={chat.id} className="relative flex items-center group">
                  <Link
                    href={`/workspace?chatId=${chat.id}`}
                    className={`flex-1 truncate rounded-[12px] px-3 py-2.5 pr-9 text-[13.5px] font-medium transition-all duration-300 flex items-center gap-3 ${
                      currentChatId === chat.id
                        ? "bg-[rgba(255,255,255,0.06)] text-white shadow-[inset_0_0_0_1px_rgba(255,255,255,0.08)]"
                        : "text-[#98999C] hover:bg-[rgba(255,255,255,0.03)] hover:text-white"
                    }`}
                  >
                    <span className={currentChatId === chat.id ? "text-white" : "text-[#52525b] group-hover:text-[#98999C] transition-colors"}>{I.chat(14)}</span>
                    <span className="truncate">{chat.title}</span>
                  </Link>

                  <button
                    onClick={(e) => { e.preventDefault(); e.stopPropagation(); setOpenMenuId(openMenuId === chat.id ? null : chat.id); }}
                    className={`absolute right-2 flex h-7 w-7 items-center justify-center rounded-[8px] border-none bg-transparent transition-colors hover:bg-[rgba(255,255,255,0.1)] hover:text-white ${currentChatId === chat.id ? 'text-white' : 'text-[#52525B] opacity-0 group-hover:opacity-100'}`}
                    title="Options"
                    aria-label={`Options for ${chat.title}`}
                  >
                    {I.dots(15)}
                  </button>

                  {openMenuId === chat.id && (
                    <div
                      ref={menuRef}
                      className="absolute right-2 top-full z-[100] mt-1.5 flex min-w-[150px] flex-col overflow-hidden rounded-[12px] bg-[rgba(25,25,28,0.95)] border border-[rgba(255,255,255,0.1)] shadow-[0_10px_30px_rgba(0,0,0,0.5)] backdrop-blur-xl py-1"
                    >
                      <MenuButton onClick={(e) => handleCopyLink(chat.id, e)}>{I.copy()} Copy Link</MenuButton>
                      <MenuButton onClick={(e) => handleExport(chat, e)}>{I.export()} Export to Text</MenuButton>
                      <div className="my-1 h-px bg-[rgba(255,255,255,0.05)]" />
                      <MenuButton danger onClick={(e) => handleDelete(chat.id, e)}>{I.trash()} Delete Chat</MenuButton>
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </div>

        {/* --- GMAIL CONNECTION BUTTON (pinned to bottom) --- */}
        <div className="border-t border-[rgba(255,255,255,0.05)] p-4 bg-[rgba(0,0,0,0.1)]">
          <button
            onClick={handleConnectGmail}
            className="flex w-full items-center justify-center gap-2.5 rounded-[12px] border border-[rgba(255,255,255,0.06)] bg-[rgba(255,255,255,0.02)] px-4 py-3 text-[13px] font-medium text-[#98999C] transition-all hover:bg-[rgba(255,255,255,0.05)] hover:text-white"
          >
            {I.mail(15)}
            Connect Gmail
          </button>
        </div>
      </aside>

      {/* 3. MOBILE TOP BAR â€” reveals the drawer */}
      <div className="absolute inset-x-0 top-0 z-40 flex h-12 items-center gap-3 border-b border-[var(--sara-line)] bg-[rgba(6,8,13,0.78)] px-3 backdrop-blur-xl md:hidden">
        <button
          onClick={() => setMobileNavOpen(true)}
          className="sara-focus-ring flex h-8 w-8 items-center justify-center rounded-[var(--sara-r-sm)] border border-[var(--sara-line)] text-[var(--sara-muted)]"
          aria-label="Open navigation"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
        </button>
        <Link href="/" className="flex items-center gap-2 text-[12px] font-semibold uppercase tracking-[0.18em] text-[var(--sara-ink)]">
          <span className="h-2 w-2 rounded-full bg-[var(--sara-accent)]" />
          SARA AI
        </Link>
      </div>

      <main className="relative z-10 flex min-w-0 flex-1 flex-col overflow-hidden pt-12 md:h-[100dvh] md:pt-0">
        {children}
      </main>
    </div>
  );
}

function MenuButton({
  children,
  onClick,
  danger = false,
}: {
  children: React.ReactNode;
  onClick: (e: React.MouseEvent) => void;
  danger?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-2.5 px-3.5 py-2 text-left text-[13px] font-medium transition-colors duration-150 ${
        danger
          ? "text-[var(--sara-err)] hover:bg-[var(--sara-err-bg)]"
          : "text-[var(--sara-ink-2)] hover:bg-[var(--sara-surface-3)]"
      }`}
    >
      {children}
    </button>
  );
}

export default function WorkspaceLayout({ children }: { children: React.ReactNode }) {
  return (
    <Suspense
      fallback={
        <div style={{ height: "100vh", width: "100vw", backgroundColor: "var(--sara-void)" }} />
      }
    >
      <SidebarContent>{children}</SidebarContent>
    </Suspense>
  );
}


// --- PURE FLOWSTATE WEBGL ENGINE (PRESERVED EXACTLY AS IS) ---
function FlowstateFluid() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current; if (!canvas) return;
    let destroyed = false; let rafHandle = 0;

    // Ensure canvas strictly fills the entire window
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;

    const config = { SIM_RESOLUTION: 128, DYE_RESOLUTION: 512, DENSITY_DISSIPATION: 0.958, VELOCITY_DISSIPATION: 0.96, PRESSURE_DISSIPATION: 0.8, PRESSURE_ITERATIONS: 20, CURL: 42, SPLAT_RADIUS: 0.22, SHADING: true, COLORFUL: true, PAUSED: false, BACK_COLOR: { r: 4, g: 5, b: 12 }, TRANSPARENT: false, BLOOM: false };
    function pointerPrototype() { return { id: -1, x: 0, y: 0, dx: 0, dy: 0, down: false, moved: false, color: [30, 0, 300] }; }
    let pointers: any[] = [pointerPrototype()]; let splatStack: number[] = [];
    const getWebGLContext = (canvas: HTMLCanvasElement) => {
      const params = { alpha: true, depth: false, stencil: false, antialias: false, preserveDrawingBuffer: false };
      let gl: any = canvas.getContext('webgl2', params);
      const isWebGL2 = !!gl; if (!isWebGL2) gl = canvas.getContext('webgl', params) || canvas.getContext('experimental-webgl', params);
      if (!gl) return { gl: null, ext: null };
      let halfFloat, supportLinearFiltering;
      if (isWebGL2) { gl.getExtension('EXT_color_buffer_float'); supportLinearFiltering = gl.getExtension('OES_texture_float_linear'); } else { halfFloat = gl.getExtension('OES_texture_half_float'); supportLinearFiltering = gl.getExtension('OES_texture_half_float_linear'); }
      gl.clearColor(0.0, 0.0, 0.0, 1.0); const halfFloatTexType = isWebGL2 ? gl.HALF_FLOAT : halfFloat.HALF_FLOAT_OES;
      let formatRGBA = getSupportedFormat(gl, isWebGL2 ? gl.RGBA16F : gl.RGBA, gl.RGBA, halfFloatTexType); let formatRG = getSupportedFormat(gl, isWebGL2 ? gl.RG16F : gl.RGBA, isWebGL2 ? gl.RG : gl.RGBA, halfFloatTexType); let formatR = getSupportedFormat(gl, isWebGL2 ? gl.R16F : gl.RGBA, isWebGL2 ? gl.RED : gl.RGBA, halfFloatTexType);
      return { gl, ext: { formatRGBA, formatRG, formatR, halfFloatTexType, supportLinearFiltering } };
    };
    function supportRenderTextureFormat(gl: any, internalFormat: any, format: any, type: any) {
      let texture = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, texture); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE); gl.texImage2D(gl.TEXTURE_2D, 0, internalFormat, 4, 4, 0, format, type, null); let fbo = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, fbo); gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0); return gl.checkFramebufferStatus(gl.FRAMEBUFFER) == gl.FRAMEBUFFER_COMPLETE;
    }
    function getSupportedFormat(gl: any, internalFormat: any, format: any, type: any): any {
      if (!supportRenderTextureFormat(gl, internalFormat, format, type)) { switch (internalFormat) { case gl.R16F: return getSupportedFormat(gl, gl.RG16F, gl.RG, type); case gl.RG16F: return getSupportedFormat(gl, gl.RGBA16F, gl.RGBA, type); default: return null; } } return { internalFormat, format };
    }
    const { gl, ext } = getWebGLContext(canvas) as any;
    if (!gl || !ext) return;
    if (/Mobi|Android/i.test(navigator.userAgent) || !ext.supportLinearFiltering) { config.SHADING = false; }
    class GLProgram {
      program: any; uniforms: any = {};
      constructor(vertexShader: any, fragmentShader: any) {
        this.program = gl.createProgram(); gl.attachShader(this.program, vertexShader); gl.attachShader(this.program, fragmentShader); gl.linkProgram(this.program);
        if (!gl.getProgramParameter(this.program, gl.LINK_STATUS)) throw gl.getProgramInfoLog(this.program);
        const uniformCount = gl.getProgramParameter(this.program, gl.ACTIVE_UNIFORMS);
        for (let i = 0; i < uniformCount; i++) { const uniformName = gl.getActiveUniform(this.program, i).name; this.uniforms[uniformName] = gl.getUniformLocation(this.program, uniformName); }
      }
      bind() { gl.useProgram(this.program); }
    }
    function compileShader(type: any, source: string) { const shader = gl.createShader(type); gl.shaderSource(shader, source); gl.compileShader(shader); if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw gl.getShaderInfoLog(shader); return shader; }
    const baseVertexShader = compileShader(gl.VERTEX_SHADER, `precision highp float; attribute vec2 aPosition; varying vec2 vUv; varying vec2 vL; varying vec2 vR; varying vec2 vT; varying vec2 vB; uniform vec2 texelSize; void main () { vUv = aPosition * 0.5 + 0.5; vL = vUv - vec2(texelSize.x, 0.0); vR = vUv + vec2(texelSize.x, 0.0); vT = vUv + vec2(0.0, texelSize.y); vB = vUv - vec2(0.0, texelSize.y); gl_Position = vec4(aPosition, 0.0, 1.0); }`);
    const clearShader = compileShader(gl.FRAGMENT_SHADER, `precision mediump float; precision mediump sampler2D; varying highp vec2 vUv; uniform sampler2D uTexture; uniform float value; void main () { gl_FragColor = value * texture2D(uTexture, vUv); }`);
    const colorShader = compileShader(gl.FRAGMENT_SHADER, `precision mediump float; uniform vec4 color; void main () { gl_FragColor = color; }`);
    const displayShadingShader = compileShader(gl.FRAGMENT_SHADER, `precision highp float; precision highp sampler2D; varying vec2 vUv; varying vec2 vL; varying vec2 vR; varying vec2 vT; varying vec2 vB; uniform sampler2D uTexture; uniform vec2 texelSize; void main () { vec3 L = texture2D(uTexture, vL).rgb; vec3 R = texture2D(uTexture, vR).rgb; vec3 T = texture2D(uTexture, vT).rgb; vec3 B = texture2D(uTexture, vB).rgb; vec3 C = texture2D(uTexture, vUv).rgb; float dx = length(R) - length(L); float dy = length(T) - length(B); vec3 n = normalize(vec3(dx, dy, length(texelSize))); vec3 l = vec3(0.0, 0.0, 1.0); float diffuse = clamp(dot(n, l) + 0.7, 0.7, 1.0); C.rgb *= diffuse; float a = max(C.r, max(C.g, C.b)); gl_FragColor = vec4(C, a); }`);
    const splatShader = compileShader(gl.FRAGMENT_SHADER, `precision highp float; precision highp sampler2D; varying vec2 vUv; uniform sampler2D uTarget; uniform float aspectRatio; uniform vec3 color; uniform vec2 point; uniform float radius; void main () { vec2 p = vUv - point.xy; p.x *= aspectRatio; vec3 splat = exp(-dot(p, p) / radius) * color; vec3 base = texture2D(uTarget, vUv).xyz; gl_FragColor = vec4(base + splat, 1.0); }`);
    const advectionShader = compileShader(gl.FRAGMENT_SHADER, `precision highp float; precision highp sampler2D; varying vec2 vUv; uniform sampler2D uVelocity; uniform sampler2D uSource; uniform vec2 texelSize; uniform float dt; uniform float dissipation; void main () { vec2 coord = vUv - dt * texture2D(uVelocity, vUv).xy * texelSize; gl_FragColor = dissipation * texture2D(uSource, coord); gl_FragColor.a = 1.0; }`);
    const divergenceShader = compileShader(gl.FRAGMENT_SHADER, `precision mediump float; precision mediump sampler2D; varying highp vec2 vUv; varying highp vec2 vL; varying highp vec2 vR; varying highp vec2 vT; varying highp vec2 vB; uniform sampler2D uVelocity; void main () { float L = texture2D(uVelocity, vL).x; float R = texture2D(uVelocity, vR).x; float T = texture2D(uVelocity, vT).y; float B = texture2D(uVelocity, vB).y; vec2 C = texture2D(uVelocity, vUv).xy; if (vL.x < 0.0) { L = -C.x; } if (vR.x > 1.0) { R = -C.x; } if (vT.y > 1.0) { T = -C.y; } if (vB.y < 0.0) { B = -C.y; } float div = 0.5 * (R - L + T - B); gl_FragColor = vec4(div, 0.0, 0.0, 1.0); }`);
    const curlShader = compileShader(gl.FRAGMENT_SHADER, `precision mediump float; precision mediump sampler2D; varying highp vec2 vUv; varying highp vec2 vL; varying highp vec2 vR; varying highp vec2 vT; varying highp vec2 vB; uniform sampler2D uVelocity; void main () { float L = texture2D(uVelocity, vL).y; float R = texture2D(uVelocity, vR).y; float T = texture2D(uVelocity, vT).x; float B = texture2D(uVelocity, vB).x; float vorticity = R - L - T + B; gl_FragColor = vec4(0.5 * vorticity, 0.0, 0.0, 1.0); }`);
    const vorticityShader = compileShader(gl.FRAGMENT_SHADER, `precision highp float; precision highp sampler2D; varying vec2 vUv; varying vec2 vL; varying vec2 vR; varying vec2 vT; varying vec2 vB; uniform sampler2D uVelocity; uniform sampler2D uCurl; uniform float curl; uniform float dt; void main () { float L = texture2D(uCurl, vL).x; float R = texture2D(uCurl, vR).x; float T = texture2D(uCurl, vT).x; float B = texture2D(uCurl, vB).x; float C = texture2D(uCurl, vUv).x; vec2 force = 0.5 * vec2(abs(T) - abs(B), abs(R) - abs(L)); force /= length(force) + 0.0001; force *= curl * C; force.y *= -1.0; vec2 vel = texture2D(uVelocity, vUv).xy; gl_FragColor = vec4(vel + force * dt, 0.0, 1.0); }`);
    const pressureShader = compileShader(gl.FRAGMENT_SHADER, `precision mediump float; precision mediump sampler2D; varying highp vec2 vUv; varying highp vec2 vL; varying highp vec2 vR; varying highp vec2 vT; varying highp vec2 vB; uniform sampler2D uPressure; uniform sampler2D uDivergence; void main () { float L = texture2D(uPressure, vL).x; float R = texture2D(uPressure, vR).x; float T = texture2D(uPressure, vT).x; float B = texture2D(uPressure, vB).x; float C = texture2D(uPressure, vUv).x; float divergence = texture2D(uDivergence, vUv).x; float pressure = (L + R + B + T - divergence) * 0.25; gl_FragColor = vec4(pressure, 0.0, 0.0, 1.0); }`);
    const gradientSubtractShader = compileShader(gl.FRAGMENT_SHADER, `precision mediump float; precision mediump sampler2D; varying highp vec2 vUv; varying highp vec2 vL; varying highp vec2 vR; varying highp vec2 vT; varying highp vec2 vB; uniform sampler2D uPressure; uniform sampler2D uVelocity; void main () { float L = texture2D(uPressure, vL).x; float R = texture2D(uPressure, vR).x; float T = texture2D(uPressure, vT).x; float B = texture2D(uPressure, vB).x; vec2 velocity = texture2D(uVelocity, vUv).xy; velocity.xy -= vec2(R - L, T - B); gl_FragColor = vec4(velocity, 0.0, 1.0); }`);
    const blit = (() => { gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer()); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, -1, 1, 1, 1, 1, -1]), gl.STATIC_DRAW); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer()); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array([0, 1, 2, 0, 2, 3]), gl.STATIC_DRAW); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0); gl.enableVertexAttribArray(0); return (destination: any) => { gl.bindFramebuffer(gl.FRAMEBUFFER, destination); gl.drawElements(gl.TRIANGLES, 6, gl.UNSIGNED_SHORT, 0); } })();

    let simWidth: number, simHeight: number, dyeWidth: number, dyeHeight: number;
    let density: any, velocity: any, divergence: any, curl: any, pressure: any;

    const clearProgram = new GLProgram(baseVertexShader, clearShader); const colorProgram = new GLProgram(baseVertexShader, colorShader); const displayShadingProgram = new GLProgram(baseVertexShader, displayShadingShader); const splatProgram = new GLProgram(baseVertexShader, splatShader); const advectionProgram = new GLProgram(baseVertexShader, advectionShader); const divergenceProgram = new GLProgram(baseVertexShader, divergenceShader); const curlProgram = new GLProgram(baseVertexShader, curlShader); const vorticityProgram = new GLProgram(baseVertexShader, vorticityShader); const pressureProgram = new GLProgram(baseVertexShader, pressureShader); const gradienSubtractProgram = new GLProgram(baseVertexShader, gradientSubtractShader);

    function createFBO(w: number, h: number, internalFormat: any, format: any, type: any, param: any) { gl.activeTexture(gl.TEXTURE0); let texture = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, texture); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, param); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, param); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE); gl.texImage2D(gl.TEXTURE_2D, 0, internalFormat, w, h, 0, format, type, null); let fbo = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, fbo); gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0); gl.viewport(0, 0, w, h); gl.clear(gl.COLOR_BUFFER_BIT); return { texture, fbo, width: w, height: h, attach(id: number) { gl.activeTexture(gl.TEXTURE0 + id); gl.bindTexture(gl.TEXTURE_2D, texture); return id; } }; }
    function createDoubleFBO(w: number, h: number, internalFormat: any, format: any, type: any, param: any) { let fbo1 = createFBO(w, h, internalFormat, format, type, param); let fbo2 = createFBO(w, h, internalFormat, format, type, param); return { get read() { return fbo1; }, set read(value) { fbo1 = value; }, get write() { return fbo2; }, set write(value) { fbo2 = value; }, swap() { let temp = fbo1; fbo1 = fbo2; fbo2 = temp; } } }

    function initFramebuffers() {
      let aspectRatio = gl.drawingBufferWidth / gl.drawingBufferHeight; if (aspectRatio < 1) aspectRatio = 1.0 / aspectRatio;
      simWidth = Math.round(config.SIM_RESOLUTION * aspectRatio); simHeight = Math.round(config.SIM_RESOLUTION); dyeWidth = Math.round(config.DYE_RESOLUTION * aspectRatio); dyeHeight = Math.round(config.DYE_RESOLUTION);
      const texType = ext.halfFloatTexType; const rgba = ext.formatRGBA; const rg = ext.formatRG; const r = ext.formatR; const filtering = ext.supportLinearFiltering ? gl.LINEAR : gl.NEAREST;
      density = createDoubleFBO(dyeWidth, dyeHeight, rgba.internalFormat, rgba.format, texType, filtering); velocity = createDoubleFBO(simWidth, simHeight, rg.internalFormat, rg.format, texType, filtering); divergence = createFBO(simWidth, simHeight, r.internalFormat, r.format, texType, gl.NEAREST); curl = createFBO(simWidth, simHeight, r.internalFormat, r.format, texType, gl.NEAREST); pressure = createDoubleFBO(simWidth, simHeight, r.internalFormat, r.format, texType, gl.NEAREST);
    }

    initFramebuffers();

    // Slight delay ensures React has fully painted the canvas DOM node before the burst calculates positions.
    setTimeout(() => {
      multipleSplats(34);
      for (let i = 0; i < 8; i++) splatStack.push(10 + parseInt((Math.random() * 10).toString()));
    }, 50);

    let lastColorChangeTime = Date.now(); let virtualSeeded = false; let orbitAngle = 0; let vPrevX = 0, vPrevY = 0; let virtualColor: any = null; let lastVColorTime = 0; const engineStart = Date.now();
    function update() {
      if (destroyed) return;
      if (canvas!.width != window.innerWidth || canvas!.height != window.innerHeight) { canvas!.width = window.innerWidth; canvas!.height = window.innerHeight; initFramebuffers(); }
      if (Date.now() - engineStart > 700) {
        const cx = canvas!.width / 2; const cy = canvas!.height / 2; const r = Math.min(300, canvas!.width * 0.35, canvas!.height * 0.35) * (0.72 + 0.28 * Math.sin(orbitAngle * 0.37)); orbitAngle += 0.026;
        const x = cx + Math.cos(orbitAngle) * r; const y = cy + Math.sin(orbitAngle) * r;
        if (!virtualSeeded) { virtualSeeded = true; vPrevX = x; vPrevY = y; } else {
          if (!virtualColor || Date.now() - lastVColorTime > 120) { virtualColor = generateColor(); virtualColor.r *= 3.2; virtualColor.g *= 3.2; virtualColor.b *= 3.2; lastVColorTime = Date.now(); }
          splat(x, y, (x - vPrevX) * 9.0, (y - vPrevY) * 9.0, virtualColor); vPrevX = x; vPrevY = y;
        }
      }
      if (splatStack.length > 0) multipleSplats(splatStack.pop() as number);
      for (let i = 0; i < pointers.length; i++) { if (pointers[i].moved) { splat(pointers[i].x, pointers[i].y, pointers[i].dx, pointers[i].dy, pointers[i].color); pointers[i].moved = false; } }
      if (lastColorChangeTime + 100 < Date.now()) { lastColorChangeTime = Date.now(); for (let i = 0; i < pointers.length; i++) pointers[i].color = generateColor(); }
      if (!config.PAUSED) step(0.016);
      render(null); rafHandle = requestAnimationFrame(update);
    }
    update();

    function step(dt: number) {
      gl.disable(gl.BLEND); gl.viewport(0, 0, simWidth, simHeight);
      curlProgram.bind(); gl.uniform2f(curlProgram.uniforms.texelSize, 1.0 / simWidth, 1.0 / simHeight); gl.uniform1i(curlProgram.uniforms.uVelocity, velocity.read.attach(0)); blit(curl.fbo);
      vorticityProgram.bind(); gl.uniform2f(vorticityProgram.uniforms.texelSize, 1.0 / simWidth, 1.0 / simHeight); gl.uniform1i(vorticityProgram.uniforms.uVelocity, velocity.read.attach(0)); gl.uniform1i(vorticityProgram.uniforms.uCurl, curl.attach(1)); gl.uniform1f(vorticityProgram.uniforms.curl, config.CURL); gl.uniform1f(vorticityProgram.uniforms.dt, dt); blit(velocity.write.fbo); velocity.swap();
      divergenceProgram.bind(); gl.uniform2f(divergenceProgram.uniforms.texelSize, 1.0 / simWidth, 1.0 / simHeight); gl.uniform1i(divergenceProgram.uniforms.uVelocity, velocity.read.attach(0)); blit(divergence.fbo);
      clearProgram.bind(); gl.uniform1i(clearProgram.uniforms.uTexture, pressure.read.attach(0)); gl.uniform1f(clearProgram.uniforms.value, config.PRESSURE_DISSIPATION); blit(pressure.write.fbo); pressure.swap();
      pressureProgram.bind(); gl.uniform2f(pressureProgram.uniforms.texelSize, 1.0 / simWidth, 1.0 / simHeight); gl.uniform1i(pressureProgram.uniforms.uDivergence, divergence.attach(0)); for (let i = 0; i < config.PRESSURE_ITERATIONS; i++) { gl.uniform1i(pressureProgram.uniforms.uPressure, pressure.read.attach(1)); blit(pressure.write.fbo); pressure.swap(); }
      gradienSubtractProgram.bind(); gl.uniform2f(gradienSubtractProgram.uniforms.texelSize, 1.0 / simWidth, 1.0 / simHeight); gl.uniform1i(gradienSubtractProgram.uniforms.uPressure, pressure.read.attach(0)); gl.uniform1i(gradienSubtractProgram.uniforms.uVelocity, velocity.read.attach(1)); blit(velocity.write.fbo); velocity.swap();
      advectionProgram.bind(); gl.uniform2f(advectionProgram.uniforms.texelSize, 1.0 / simWidth, 1.0 / simHeight); let velocityId = velocity.read.attach(0); gl.uniform1i(advectionProgram.uniforms.uVelocity, velocityId); gl.uniform1i(advectionProgram.uniforms.uSource, velocityId); gl.uniform1f(advectionProgram.uniforms.dt, dt); gl.uniform1f(advectionProgram.uniforms.dissipation, config.VELOCITY_DISSIPATION); blit(velocity.write.fbo); velocity.swap();
      gl.viewport(0, 0, dyeWidth, dyeHeight); gl.uniform1i(advectionProgram.uniforms.uVelocity, velocity.read.attach(0)); gl.uniform1i(advectionProgram.uniforms.uSource, density.read.attach(1)); gl.uniform1f(advectionProgram.uniforms.dissipation, config.DENSITY_DISSIPATION); blit(density.write.fbo); density.swap();
    }
    function render(target: any) {
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA); gl.enable(gl.BLEND); let width = target == null ? gl.drawingBufferWidth : dyeWidth; let height = target == null ? gl.drawingBufferHeight : dyeHeight; gl.viewport(0, 0, width, height); colorProgram.bind();
      gl.uniform4f(colorProgram.uniforms.color, config.BACK_COLOR.r / 255, config.BACK_COLOR.g / 255, config.BACK_COLOR.b / 255, 1); blit(target);
      displayShadingProgram.bind(); gl.uniform2f(displayShadingProgram.uniforms.texelSize, 1.0 / width, 1.0 / height); gl.uniform1i(displayShadingProgram.uniforms.uTexture, density.read.attach(0)); blit(target);
    }
    function splat(x: number, y: number, dx: number, dy: number, color: any) {
      gl.viewport(0, 0, simWidth, simHeight); splatProgram.bind(); gl.uniform1i(splatProgram.uniforms.uTarget, velocity.read.attach(0)); gl.uniform1f(splatProgram.uniforms.aspectRatio, canvas!.width / canvas!.height); gl.uniform2f(splatProgram.uniforms.point, x / canvas!.width, 1.0 - y / canvas!.height); gl.uniform3f(splatProgram.uniforms.color, dx, -dy, 1.0); gl.uniform1f(splatProgram.uniforms.radius, config.SPLAT_RADIUS / 100.0); blit(velocity.write.fbo); velocity.swap();
      gl.viewport(0, 0, dyeWidth, dyeHeight); gl.uniform1i(splatProgram.uniforms.uTarget, density.read.attach(0)); gl.uniform3f(splatProgram.uniforms.color, color.r, color.g, color.b); blit(density.write.fbo); density.swap();
    }
    function multipleSplats(amount: number) { for (let i = 0; i < amount; i++) { const color = generateColor(); color.r *= 10.0; color.g *= 10.0; color.b *= 10.0; splat(canvas!.width * Math.random(), canvas!.height * Math.random(), 1000 * (Math.random() - 0.5), 1000 * (Math.random() - 0.5), color); } }

    // IMPORTANT: Maps mouse coordinates accurately across the whole fixed screen
    const onPointerMove = (e: any) => { const p = pointers[0]; if (!p.everMoved) { p.everMoved = true; p.x = e.clientX; p.y = e.clientY; p.down = true; return; } p.down = true; p.moved = true; p.dx = (e.clientX - p.x) * 5.0; p.dy = (e.clientY - p.y) * 5.0; p.x = e.clientX; p.y = e.clientY; p.color = generateColor(); };
    window.addEventListener('mousemove', onPointerMove);

    function generateColor() {
      const h = 0.5 + Math.random() * 0.42; let i = Math.floor(h * 6), f = h * 6 - i, p = 1.0 * (1 - 0.95), q = 1.0 * (1 - f * 0.95), t = 1.0 * (1 - (1 - f) * 0.95); let r = 0, g = 0, b = 0;
      switch (i % 6) { case 0: r=1.0; g=t; b=p; break; case 1: r=q; g=1.0; b=p; break; case 2: r=p; g=1.0; b=t; break; case 3: r=p; g=q; b=1.0; break; case 4: r=t; g=p; b=1.0; break; case 5: r=1.0; g=p; b=q; break; }
      return { r: r * 0.92, g: g * 0.92, b: b * 0.92 };
    }
    return () => { destroyed = true; if (rafHandle) cancelAnimationFrame(rafHandle); window.removeEventListener('mousemove', onPointerMove); };
  }, []);
  return <canvas ref={canvasRef} style={{ width: '100vw', height: '100vh', display: 'block' }} aria-hidden="true" />;
}
