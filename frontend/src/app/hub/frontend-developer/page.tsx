"use client";

import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";

type JungleMindProps = {
  apiBaseUrl?: string;
};

type Panel = "news" | "help" | "credits" | null;
type ViewMode = "landing" | "workspace";
type Message = { role: "user" | "assistant"; content: string };
type Tab = "preview" | "code";
type SessionHistory = {
  id: string;
  prompt: string;
  messages: Message[];
  code: string;
  timestamp: number;
};

const VIDEO =
  "https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260831_232706_43757be4-2250-4f09-8cd7-23aebbf147ad.mp4";

const POSTER =
  "https://images.higgs.ai/?default=1&output=webp&url=https%3A%2F%2Fd8j0ntlcm91z4.cloudfront.net%2Fuser_38xzZboKViGWJOttwIXH07lWA1P%2Fhf_20260831_223518_f11bfa03-4e65-47e1-a4a7-30e42a7a8c2f.png&w=1920&q=85";

const styles = `
:root {
  --ink:#111111;
  --muted:#5c5c5c;
  --card:rgba(248,248,246,0.97);
  --shell:1120px;
  --ease:cubic-bezier(0.16,1,0.3,1);
}
*, *::before, *::after { box-sizing:border-box }

/* The main wrapper filling the flex area below Nexus Nav */
.frontend-agent-wrapper {
  margin:0; padding:0; 
  background:#f8f9fa;
  font-family:"Poppins",-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 64px;
  left: 0;
  right: 0;
  bottom: 0;
  width: 100vw;
  height: calc(100vh - 64px);
  z-index: 10;
  overflow: hidden;
}

.frontend-agent-wrapper button, .frontend-agent-wrapper input, .frontend-agent-wrapper textarea { font-family:inherit }
.frontend-agent-wrapper button, .frontend-agent-wrapper a, .frontend-agent-wrapper textarea, .frontend-agent-wrapper input { -webkit-tap-highlight-color:transparent }
.frontend-agent-wrapper button:disabled { cursor:wait; opacity:.6 }
.frontend-agent-wrapper a { color:inherit; text-decoration:none; }
.frontend-agent-wrapper :focus-visible { outline:2px solid #166047; outline-offset:2px }

/* Landing Page Styles */
.hero {
  position:relative; 
  height: 100%;
  width: 100%; 
  overflow: hidden; 
  display: flex; 
  flex-direction: column;
  color: var(--ink);
}
.hero__bg { position:absolute; inset:0; z-index:0; background-size:100% 100%; background-position:center; background-color:#eef2ee; }
.hero__bg video { position:absolute; inset:0; width:100%; height:100%; object-fit:fill; }
.hero__inner { position:relative; z-index:2; display:flex; flex-direction:column; flex:1 1 auto; min-height:0; }

.nav {
  position:relative; z-index:70; width:100%; max-width:var(--shell);
  margin:0 auto; padding:26px 24px 0; display:grid;
  grid-template-columns:1fr auto 1fr; align-items:center; gap:16px;
}
.brand {
  display:inline-flex; align-items:center; gap:10px; font-size:25px;
  font-weight:600; letter-spacing:-.02em; color:var(--ink);
  white-space:nowrap; width:fit-content; cursor:pointer;
}
.brand svg { width:30px; height:30px; display:block; flex-shrink:0 }
.nav__links { display:flex; align-items:center; gap:42px; list-style:none; margin:0; padding:0; }
.nav__links a { font-size:15px; font-weight:400; color:#1a1a1a; opacity:.9; transition:opacity .18s ease; cursor:pointer; }
.nav__links a:hover { opacity:.55 }

.stage {
  flex:1 1 auto; min-height:0; display:flex; flex-direction:column;
  align-items:center; justify-content:center; text-align:center;
  padding:24px; gap:clamp(18px,2.4vh,26px);
}
.badge {
  display:inline-flex; align-items:center; gap:14px;
  background:rgba(255,255,255,.94); border:1px solid rgba(0,0,0,.07);
  border-radius:999px; padding:7px 16px 7px 8px; font-size:13.5px; color:#4a4a4a;
}
.badge__tag {
  background:rgba(255,255,255,.95); border:1px solid rgba(0,0,0,.07);
  border-radius:999px; padding:3px 12px; font-size:13.5px; font-weight:500; color:#111;
}
.headline { margin:0; font-size:clamp(32px,5.7vw,72px); line-height:1.03; font-weight:500; letter-spacing:-.035em; max-width:20ch; }
.sub { margin:0; font-size:clamp(14px,1.35vw,17.5px); line-height:1.65; font-weight:400; color:var(--muted); max-width:66ch; }

.prompt {
  position:relative; width:min(790px,100%); background:rgba(255,255,255,.42);
  -webkit-backdrop-filter:blur(18px) saturate(1.15); backdrop-filter:blur(18px) saturate(1.15);
  border:1px solid rgba(0,0,0,.06); border-radius:20px;
  padding:22px 20px 16px; text-align:left;
  box-shadow:0 18px 44px -26px rgba(0,0,0,.28);
  margin-top:clamp(6px,1.6vh,16px);
}
.prompt__input {
  display:block; width:100%; border:0; outline:0; resize:none;
  background:transparent; font-family:inherit; font-size:16.5px;
  line-height:1.5; color:var(--ink); min-height:clamp(56px,11vh,108px);
  padding:2px 4px;
}
.prompt__input::placeholder { color:#3d3d3d; opacity:.72 }
.prompt__bar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:10px; }
.prompt__right { display:flex; align-items:center; gap:12px }
.icon-btn {
  display:inline-flex; align-items:center; justify-content:center;
  width:42px; height:42px; border-radius:50%; border:1px solid rgba(0,0,0,.1);
  background:#fff; color:#1a1a1a; cursor:pointer; padding:0;
  transition:background .18s ease; flex-shrink:0;
}
.icon-btn:hover { background:#f1f1ef }
.icon-btn--bare { border:0; background:transparent; width:34px }
.icon-btn--bare:hover { background:rgba(0,0,0,.05) }
.icon-btn--send {
  background:#141414; border-color:#141414; color:#fff;
  width:46px; height:46px; border-radius:12px;
}
.icon-btn--send:hover { background:#000 }
.icon-btn svg { width:19px; height:19px }
.icon-btn--send svg { width:21px; height:21px }

/* Workspace Layout */
.workspace {
  display: flex;
  flex: 1;
  width: 100%;
  background: #fdfdfc;
  overflow: hidden;
  height: 100%;
}

.workspace__sidebar {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-right: 1px solid #e5e7eb;
  background: #f9fafb;
  height: 100%;
  overflow: hidden;
  transition: transform 0.3s ease;
}
.workspace__sidebar.closed {
  transform: translateX(-100%);
  border-right: none;
}
.resizer {
  width: 6px;
  cursor: col-resize;
  background: transparent;
  flex-shrink: 0;
  z-index: 10;
  position: relative;
}
.resizer:hover {
  background: rgba(0, 0, 0, 0.03);
}
.resizer:active {
  background: rgba(59, 130, 246, 0.3);
}
body.resizing * {
  pointer-events: none !important;
  user-select: none !important;
}

.sidebar__header {
  padding: 16px 20px;
  border-bottom: 1px solid #e5e7eb;
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}

.btn-new-chat {
  background: #111; color: #fff; border: none;
  padding: 8px 14px; border-radius: 8px; font-size: 13px; font-weight: 500;
  cursor: pointer; display: flex; align-items: center; gap: 6px;
  transition: opacity 0.2s;
}
.btn-new-chat:hover { opacity: 0.8; }

.sidebar__chat {
  flex: 1;
  overflow-y: auto;
  padding: 24px 20px;
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.history-item {
  padding: 14px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  margin-bottom: 10px;
  cursor: pointer;
  transition: all 0.2s;
}
.history-item:hover {
  border-color: #d1d5db;
  box-shadow: 0 4px 12px rgba(0,0,0,0.04);
  transform: translateY(-1px);
}

.msg { display: flex; flex-direction: column; gap: 6px; }
.msg--user { align-items: flex-end; }
.msg--assistant { align-items: flex-start; }

.msg__bubble {
  max-width: 95%;
  padding: 14px 18px;
  border-radius: 14px;
  font-size: 14px;
  line-height: 1.6;
  word-wrap: break-word;
}
.msg--user .msg__bubble {
  background: #111; color: #fff;
  border-bottom-right-radius: 4px;
}
.msg--assistant .msg__bubble {
  background: #fff; color: #111;
  border: 1px solid #e5e7eb;
  border-bottom-left-radius: 4px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.03);
}

.artifact-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: #f3f4f6;
  border: 1px solid #e5e7eb;
  padding: 8px 14px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  color: #374151;
  margin-top: 8px;
}
.artifact-pill svg { width: 16px; height: 16px; color: #059669; }

.sidebar__input-area {
  padding: 20px;
  background: #fff;
  border-top: 1px solid #e5e7eb;
  flex-shrink: 0;
}

/* Main Workspace Pane */
.workspace__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #1e1e1e; /* Dark theme for the editor side */
  overflow: hidden;
}

.main__header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 24px;
  background: #181818;
  border-bottom: 1px solid #2d2d2d;
  flex-shrink: 0;
}

.tab-btn {
  background: transparent; border: none; font-family: inherit;
  font-size: 13px; font-weight: 500; color: #888;
  padding: 8px 16px; cursor: pointer; border-radius: 6px;
  transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px;
}
.tab-btn:hover { color: #d4d4d4; }
.tab-btn.active { background: #2d2d2d; color: #fff; }

.main__content {
  flex: 1;
  overflow: hidden; /* Contains the scrollable children */
  position: relative;
  display: flex;
}

.iframe-preview {
  flex: 1;
  width: 100%;
  height: 100%;
  border: none;
  background: #fff;
}

.code-preview {
  flex: 1;
  width: 100%;
  height: 100%;
  overflow-y: auto;
  overflow-x: auto;
  padding: 24px;
  margin: 0;
  background: #1e1e1e;
  color: #d4d4d4;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  font-size: 14px;
  line-height: 1.6;
}

/* Custom Scrollbars for code block */
.code-preview::-webkit-scrollbar, .sidebar__chat::-webkit-scrollbar, .dialog::-webkit-scrollbar {
  width: 8px; height: 8px;
}
.code-preview::-webkit-scrollbar-track, .sidebar__chat::-webkit-scrollbar-track, .dialog::-webkit-scrollbar-track {
  background: transparent;
}
.code-preview::-webkit-scrollbar-thumb {
  background: #444; border-radius: 4px;
}
.sidebar__chat::-webkit-scrollbar-thumb, .dialog::-webkit-scrollbar-thumb {
  background: #d1d5db; border-radius: 4px;
}

.dialog-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4); backdrop-filter: blur(4px);
  z-index: 9998;
}
.dialog {
  position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
  border: none; border-radius: 16px; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
  background: #fff; width: 90vw; max-width: 600px; padding: 0; color: var(--ink);
  z-index: 9999; margin: 0;
}
.dialog__close {
  background: transparent; border: none; font-size: 24px; color: #888;
  cursor: pointer; transition: color 0.2s; padding: 4px; line-height: 1;
}
.dialog__close:hover { color: #111; }

@keyframes fade-in { from { opacity:0 } to { opacity:1 } }
.fade-in { animation:fade-in .4s ease-out; }
@keyframes rise { from { opacity:0; transform:translate3d(0,18px,0) } to { opacity:1; transform:translate3d(0,0,0) } }
.rise { animation:rise .9s var(--ease) both; animation-delay:calc(var(--i,0) * 70ms) }
`;

function RiseStyle(index: number) { return { "--i": index } as React.CSSProperties; }

function Logo() {
  return (
    <svg viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <path d="M16 2.5 29.5 16 16 29.5 2.5 16 16 2.5Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
      <path d="M16 9.5 22.5 16 16 22.5 9.5 16 16 9.5Z" fill="currentColor" />
    </svg>
  );
}

function Icon({ kind }: { kind: "plus" | "mic" | "send" | "check" | "download" }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={kind === "mic" ? 1.7 : 1.9} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {kind === "plus" && <path d="M12 5v14M5 12h14" />}
      {kind === "mic" && <><rect x="9" y="2.5" width="6" height="11" rx="3" /><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" /></>}
      {kind === "send" && <path d="M4 12h15M13 6l6 6-6 6" />}
      {kind === "check" && <path d="M20 6L9 17l-5-5" />}
      {kind === "download" && <><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></>}
    </svg>
  );
}

export default function JungleMind({
  // Defaults to the user's OWN machine, because that is where the desktop
  // half of SARA runs. A hardcoded localhost worked locally and silently
  // pointed every deployed browser at itself in production.
  apiBaseUrl = `${process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000"}/frontend-developer`,
}: JungleMindProps) {
  const base = apiBaseUrl.replace(/\/+$/, "");
  const [viewMode, setViewMode] = useState<ViewMode>("landing");
  const [messages, setMessages] = useState<Message[]>([]);
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>("preview");
  const [generatedCode, setGeneratedCode] = useState<string>("");
  const [history, setHistory] = useState<SessionHistory[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [panel, setPanel] = useState<string | null>(null);
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);
  const [isListening, setIsListening] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(400);
  
  const chatEndRef = useRef<HTMLDivElement>(null);
  const recognitionRef = useRef<any>(null);
  const isDragging = useRef(false);

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDragging.current) return;
      const newWidth = Math.max(250, Math.min(e.clientX, window.innerWidth - 300));
      setSidebarWidth(newWidth);
    };
    
    const handleMouseUp = () => {
      if (isDragging.current) {
        isDragging.current = false;
        document.body.classList.remove('resizing');
      }
    };

    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
      document.body.classList.remove('resizing');
    };
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined") {
      const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
      if (SpeechRecognition) {
        const recognition = new SpeechRecognition();
        recognition.continuous = false;
        recognition.interimResults = false;
        
        recognition.onresult = (event: any) => {
          const transcript = event.results[0][0].transcript;
          setPrompt(prev => prev ? prev + " " + transcript : transcript);
        };
        
        recognition.onend = () => {
          setIsListening(false);
        };
        
        recognitionRef.current = recognition;
      }
    }
  }, []);

  const handleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    isDragging.current = true;
    document.body.classList.add('resizing');
  };

  const handleMicClick = () => {
    if (!recognitionRef.current) {
      alert("Speech recognition is not supported in this browser. Try Chrome or Edge.");
      return;
    }
    if (isListening) {
      recognitionRef.current.stop();
      setIsListening(false);
    } else {
      recognitionRef.current.start();
      setIsListening(true);
    }
  };

  // Load history on mount
  useEffect(() => {
    const saved = localStorage.getItem("junglemind_history");
    if (saved) {
      try { setHistory(JSON.parse(saved)); } catch(e) {}
    }
  }, []);

  // Save history on change
  useEffect(() => {
    localStorage.setItem("junglemind_history", JSON.stringify(history));
  }, [history]);

  // Auto-scroll chat
  useEffect(() => {
    if (chatEndRef.current) {
      chatEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, loading, viewMode]);

  const extractHtml = (text: string) => {
    // 1. Try to find markdown code block (html or no lang)
    const regex = /```(?:html|xml)?\s*([\s\S]*?)```/i;
    const match = text.match(regex);
    if (match && match[1]) {
      return match[1].trim();
    }
    // 2. Fallback: look for <html> tags
    const htmlMatch = text.match(/<html[\s\S]*?<\/html>/i);
    if (htmlMatch) {
      return htmlMatch[0];
    }
    // 3. Fallback: look for generic wrappers
    if (text.includes("<div") || text.includes("<body") || text.includes("<!DOCTYPE")) {
      return text.replace(/```/g, "").trim();
    }
    return "";
  };

  const formatAssistantMessage = (text: string) => {
    const extracted = extractHtml(text);
    if (extracted && text.length > 50) {
      // If code was found, replace it with a pill in the chat
      // We do a simple replacement of the code block if it exists
      const regex = /```(?:html|xml)?\s*[\s\S]*?```/i;
      const textWithoutCode = text.replace(regex, "").trim();
      return (
        <>
          {textWithoutCode && textWithoutCode !== text && <p style={{ margin: "0 0 8px 0", whiteSpace: "pre-wrap" }}>{textWithoutCode}</p>}
          <div className="artifact-pill">
            <Icon kind="check" /> Code generated & updated in workspace
          </div>
        </>
      );
    }
    return <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{text}</p>;
  };

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [attachedFile, setAttachedFile] = useState<File | null>(null);

  const handleAttachClick = () => {
    fileInputRef.current?.click();
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setAttachedFile(e.target.files[0]);
    }
  };

  const startNewChat = () => {
    setMessages([]);
    setGeneratedCode("");
    setPrompt("");
    setAttachedFile(null);
    setActiveTab("preview");
    setSessionId(null);
    setViewMode("landing");
  };

  const loadSession = (session: SessionHistory) => {
    setSessionId(session.id);
    setMessages(session.messages);
    setGeneratedCode(session.code);
    setViewMode("workspace");
    setPanel(null);
  };

  const deleteSession = (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    setHistory(prev => prev.filter(s => s.id !== id));
  };

  const shareSession = (e: React.MouseEvent, code: string) => {
    e.stopPropagation();
    if (navigator.clipboard) {
      navigator.clipboard.writeText(code);
      alert("Code copied to clipboard! You can share it anywhere.");
    }
  };

  const handleDownload = () => {
    if (!generatedCode) return;
    const blob = new Blob([generatedCode], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "junglemind_ui.html";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const handlePromptSubmit = async (e?: FormEvent) => {
    if (e) e.preventDefault();
    if (!prompt.trim() || loading) return;
    
    if (viewMode === "landing") {
      setViewMode("workspace");
    }
    
    const userMessage = prompt.trim();
    setPrompt("");
    setLoading(true);
    
    const newMessages = [...messages, { role: "user" as const, content: userMessage }];
    setMessages(newMessages);
    
    const currentSessionId = sessionId || Date.now().toString();
    if (!sessionId) setSessionId(currentSessionId);
    
    try {
      const response = await fetch(`${base}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: newMessages })
      });
      
      if (!response.ok) {
        const errData = await response.json().catch(() => null);
        let errorMsg = `Failed (${response.status})`;
        if (errData?.detail) {
          errorMsg = typeof errData.detail === 'string' ? errData.detail : JSON.stringify(errData.detail);
        }
        throw new Error(errorMsg);
      }
      
      const data = await response.json();
      const assistantMessage = data.answer;
      
      const updatedMessages = [...newMessages, { role: "assistant" as const, content: assistantMessage }];
      setMessages(updatedMessages);
      
      const extractedCode = extractHtml(assistantMessage);
      const updatedCode = extractedCode || generatedCode;
      if (extractedCode) {
        setGeneratedCode(extractedCode);
      }
      
      // Save to history
      setHistory(prev => {
        const existing = prev.filter(p => p.id !== currentSessionId);
        return [{
          id: currentSessionId,
          prompt: newMessages[0].content, // Label with the first prompt
          messages: updatedMessages,
          code: updatedCode,
          timestamp: Date.now()
        }, ...existing].slice(0, 20); // Keep last 20 chats
      });
    } catch (e: any) {
      setMessages([...newMessages, { role: "assistant", content: "Error: " + e.message }]);
    } finally {
      setLoading(false);
    }
  };

  if (viewMode === "workspace") {
    return (
      <div className="frontend-agent-wrapper fade-in">
        <style dangerouslySetInnerHTML={{ __html: styles }} />
        <div className="workspace">
          <aside className={`workspace__sidebar ${!isSidebarOpen ? "closed" : ""}`} style={{ width: isSidebarOpen ? sidebarWidth : 0 }}>
            <header className="sidebar__header">
              <a href="#" className="brand" onClick={startNewChat} style={{ fontSize: "20px", whiteSpace: "nowrap" }}>
                <Logo /> JungleMind
              </a>
              <button className="btn-new-chat" onClick={startNewChat} title="Start new chat" style={{ whiteSpace: "nowrap" }}>
                <Icon kind="plus" /> New Chat
              </button>
            </header>
            
            <div className="sidebar__chat">
              {messages.map((msg, idx) => (
                <div key={idx} className={`msg msg--${msg.role}`}>
                  <div className="msg__bubble">
                    {msg.role === "assistant" ? formatAssistantMessage(msg.content) : msg.content}
                  </div>
                </div>
              ))}
              {loading && (
                <div className="msg msg--assistant">
                  <div className="msg__bubble" style={{ color: "var(--muted)" }}>Thinking...</div>
                </div>
              )}
              <div ref={chatEndRef} />
            </div>

            <div className="sidebar__input-area">
              {attachedFile && (
                <div style={{ display: "inline-flex", alignItems: "center", gap: "6px", background: "#f3f4f6", padding: "4px 10px", borderRadius: "6px", fontSize: "12px", marginBottom: "8px" }}>
                  <span>{attachedFile.name}</span>
                  <button type="button" onClick={() => setAttachedFile(null)} style={{ background: "none", border: "none", cursor: "pointer", color: "#888" }}>×</button>
                </div>
              )}
              <form 
                className="prompt" 
                style={{ marginTop: 0, width: "100%", borderRadius: "16px", padding: "16px 14px 12px" }}
                onSubmit={handlePromptSubmit}
              >
                <input type="file" ref={fileInputRef} style={{ display: "none" }} onChange={handleFileChange} />
                <textarea
                  className="prompt__input"
                  style={{ minHeight: "50px", fontSize: "14px" }}
                  placeholder="Describe your UI requirements..."
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      handlePromptSubmit();
                    }
                  }}
                  disabled={loading}
                />
                <div className="prompt__bar">
                  <button type="button" className="icon-btn icon-btn--bare" disabled={loading} title="Attach file" onClick={handleAttachClick}>
                    <Icon kind="plus" />
                  </button>
                  <div className="prompt__right">
                    <button type="button" className="icon-btn icon-btn--bare" disabled={loading} onClick={handleMicClick} title="Dictate prompt" style={{ color: isListening ? "#ef4444" : "inherit" }}>
                      <Icon kind="mic" />
                    </button>
                    <button type="submit" className="icon-btn icon-btn--send" style={{ width: "36px", height: "36px", borderRadius: "10px" }} disabled={loading}>
                      <Icon kind="send" />
                    </button>
                  </div>
                </div>
              </form>
            </div>
          </aside>
          {isSidebarOpen && <div className="resizer" onMouseDown={handleMouseDown} />}
          <main className="workspace__main">
            <header className="main__header">
              <button className="tab-btn" onClick={() => setIsSidebarOpen(!isSidebarOpen)} title="Toggle Chat" style={{ marginRight: "8px" }}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 3h18v18H3zM9 3v18"/></svg>
              </button>
              <button 
                className={`tab-btn ${activeTab === "preview" ? "active" : ""}`}
                onClick={() => setActiveTab("preview")}
              >
                Live Preview
              </button>
              <button 
                className={`tab-btn ${activeTab === "code" ? "active" : ""}`}
                onClick={() => setActiveTab("code")}
              >
                Code (HTML)
              </button>
              <div style={{ flex: 1 }}></div>
              {generatedCode && (
                <button className="tab-btn" onClick={handleDownload} title="Download HTML File" style={{ color: "#10b981", background: "rgba(16, 185, 129, 0.1)" }}>
                  <Icon kind="download" /> Download Code
                </button>
              )}
            </header>
            
            <div className="main__content">
              {activeTab === "preview" && (
                <iframe
                  className="iframe-preview fade-in"
                  srcDoc={generatedCode || "<div style='display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;color:#888;'>Waiting for code generation...</div>"}
                  sandbox="allow-scripts allow-same-origin"
                />
              )}
              
              {activeTab === "code" && (
                <pre className="code-preview fade-in">
                  {generatedCode || "/* No code generated yet */"}
                </pre>
              )}
            </div>
          </main>
        </div>
      </div>
    );
  }

  // Landing Page Mode
  return (
    <div className="frontend-agent-wrapper fade-in">
      <style dangerouslySetInnerHTML={{ __html: styles }} />
      <div className="hero">
        <div className="hero__bg">
          <video src={VIDEO} poster={POSTER} autoPlay loop muted playsInline />
        </div>
        
        <div className="hero__inner">
          <header className="nav">
            <a href="#" className="brand rise" style={RiseStyle(0)}><Logo /> JungleMind</a>
            <nav className="nav__nav">
              <ul className="nav__links">
                <li><a onClick={() => setPanel("history")} className="rise" style={RiseStyle(1)}>History</a></li>
                <li><a onClick={() => setPanel("help")} className="rise" style={RiseStyle(2)}>Help</a></li>
              </ul>
            </nav>
            <div className="nav__cta"></div>
          </header>

          <main className="stage">
            <div className="badge rise" style={RiseStyle(5)}>
              <span className="badge__tag">v2.0</span> Intelligent Workspace
            </div>
            <h1 className="headline rise" style={RiseStyle(6)}>
              Intelligence that <span className="brk" /> moves with you.
            </h1>
            <p className="sub rise" style={RiseStyle(7)}>
              JungleMind is an experimental interface exploring dynamic compute distribution
              across edge environments.
            </p>

            <form className="prompt rise" style={RiseStyle(8)} onSubmit={handlePromptSubmit}>
              <textarea
                className="prompt__input"
                placeholder="Ask JungleMind to synthesize data, draft code, or analyze trends..."
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handlePromptSubmit();
                  }
                }}
                disabled={loading}
              />
              <div className="prompt__bar">
                <button type="button" className="icon-btn icon-btn--bare" disabled={loading} onClick={startNewChat} title="Reset chat">
                  <Icon kind="plus" />
                </button>
                <div className="prompt__right">
                  <button type="button" className="icon-btn icon-btn--bare" disabled={loading} onClick={handleMicClick} title="Dictate prompt" style={{ color: isListening ? "#ef4444" : "inherit" }}>
                    <Icon kind="mic" />
                  </button>
                  <button type="submit" className="icon-btn icon-btn--send" disabled={loading}>
                    <Icon kind="send" />
                  </button>
                </div>
              </div>
            </form>
          </main>
        </div>
      </div>

      {panel === "history" && (
        <>
          <div className="dialog-backdrop" onClick={() => setPanel(null)} />
          <dialog className="dialog fade-in" open style={{ padding: "0" }}>
            <div style={{ padding: "20px 24px", borderBottom: "1px solid #e5e7eb", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h2 style={{ margin: 0, fontSize: "20px" }}>Recent Sessions</h2>
              <button className="dialog__close" onClick={() => setPanel(null)} style={{ float: "none" }}>×</button>
            </div>
            <div style={{ padding: "24px", maxHeight: "60vh", overflowY: "auto" }}>
              {history.length === 0 ? (
                <p style={{ color: "#6b7280", textAlign: "center", margin: "20px 0" }}>No history found.</p>
              ) : (
                history.map(session => (
                  <div key={session.id} className="history-item" onClick={() => loadSession(session)} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <div style={{ flex: 1, minWidth: 0, paddingRight: "16px" }}>
                      <div style={{ fontWeight: 500, fontSize: "14px", marginBottom: "4px", color: "#111", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {session.prompt}
                      </div>
                      <div style={{ fontSize: "12px", color: "#6b7280" }}>
                        {new Date(session.timestamp).toLocaleDateString()}
                      </div>
                    </div>
                    <div style={{ display: "flex", gap: "8px" }}>
                      <button 
                        onClick={(e) => shareSession(e, session.code)}
                        style={{ background: "#f3f4f6", border: "1px solid #e5e7eb", borderRadius: "6px", padding: "6px", cursor: "pointer", display: "flex", alignItems: "center" }}
                        title="Copy Code"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#374151" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
                      </button>
                      <button 
                        onClick={(e) => deleteSession(e, session.id)}
                        style={{ background: "#fef2f2", border: "1px solid #fecaca", borderRadius: "6px", padding: "6px", cursor: "pointer", display: "flex", alignItems: "center" }}
                        title="Delete Session"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="2"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>
                      </button>
                    </div>
                  </div>
                ))
              )}
            </div>
          </dialog>
        </>
      )}

      {panel === "help" && (
        <>
          <div className="dialog-backdrop" onClick={() => setPanel(null)} />
          <dialog className="dialog fade-in" open style={{ padding: "0" }}>
            <div style={{ padding: "20px 24px", borderBottom: "1px solid #e5e7eb", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h2 style={{ margin: 0, fontSize: "20px" }}>Keyboard Shortcuts & Help</h2>
              <button className="dialog__close" onClick={() => setPanel(null)} style={{ float: "none" }}>×</button>
            </div>
            <div style={{ padding: "24px", lineHeight: "1.6" }}>
              <p>Welcome to <strong>JungleMind</strong>. Describe what you want to build and the AI will generate and instantly preview a single-file React/HTML component.</p>
              <ul style={{ paddingLeft: "20px", marginTop: "12px", color: "#4b5563" }}>
                <li><strong>Enter</strong> - Send message</li>
                <li><strong>Shift + Enter</strong> - New line</li>
                <li><strong>History</strong> - Your chats are saved automatically locally.</li>
                <li><strong>Mic Icon</strong> - Click to dictate your prompt directly!</li>
              </ul>
            </div>
          </dialog>
        </>
      )}
    </div>
  );
}
