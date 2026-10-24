"use client";

import { useState, useRef, useEffect, Suspense } from "react";
import ChatInterface from "@/components/ChatInterface";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useSearchParams } from "next/navigation";
import { SARA_API_URL, saraFetch } from "@/lib/api";
import { LocalChat } from "./layout";

interface AgentInfoBannerProps {
  agentName: string;
  defaultDescription: string;
  capabilities: string[];
}

export function AgentInfoBanner({ agentName, defaultDescription, capabilities }: AgentInfoBannerProps) {
  const [language, setLanguage] = useState("English");
  const [displayDescription, setDisplayDescription] = useState(defaultDescription);
  const [isTranslating, setIsTranslating] = useState(false);

  const languages = ["English", "Hindi", "Kannada", "Spanish", "French"];

  const handleLanguageChange = async (newLang: string) => {
    if (newLang === language) return;
    setLanguage(newLang);
    setIsTranslating(true);

    if (newLang === "English") {
      setDisplayDescription(defaultDescription);
      setIsTranslating(false);
      return;
    }

    try {
      const res = await saraFetch(`${SARA_API_URL}/api/chat/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ 
          user_id: "system", 
          chat_id: "translation_request", 
          question: `Translate the following agent description into ${newLang}. Return ONLY the translated text, no other words: "${defaultDescription}"` 
        }),
      });

      if (res.ok) {
        const data = await res.json();
        setDisplayDescription(data.answer);
      }
    } catch (error) {
      console.error("Translation failed", error);
    } finally {
      setIsTranslating(false);
    }
  };

  return (
    <div className="sara-surface-raised mb-6 w-full max-w-[56rem] mx-auto rounded-[var(--sara-r-2xl)] p-6 sara-fade-in relative overflow-hidden">
      <div className="absolute -top-12 -right-12 w-40 h-40 bg-[var(--sara-accent-glow)] rounded-full blur-3xl opacity-50 pointer-events-none" />
      
      <div className="flex justify-between items-start mb-4 gap-4 relative z-10">
        <div>
          <h2 className="text-xl font-semibold tracking-[0.05em] text-[var(--sara-accent)] mb-3">
            {agentName}
          </h2>
          <div className="flex flex-wrap gap-2">
            {capabilities.map((cap, index) => (
              <span key={index} className="sara-chip sara-chip-accent">
                ✓ {cap}
              </span>
            ))}
          </div>
        </div>

        <select
          value={language}
          onChange={(e) => handleLanguageChange(e.target.value)}
          className="sara-select w-auto min-w-[8rem] bg-[var(--sara-bg-2)] text-white"
          aria-label="Translate description"
        >
          {languages.map((lang) => (
            <option key={lang} value={lang}>
              {lang}
            </option>
          ))}
        </select>
      </div>

      <div className={`text-[0.9375rem] leading-relaxed text-[var(--sara-muted)] transition-opacity duration-300 relative z-10 ${isTranslating ? 'opacity-50' : 'opacity-100'}`}>
        {isTranslating ? "Translating command sequences..." : displayDescription}
      </div>
    </div>
  );
}

export type Message = {
  id: string;
  role: "user" | "ai";
  content: string;
};

function CodeBlock({ language, code }: { language: string, code: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="sara-surface-flat rounded-[var(--sara-r-lg)] my-4 overflow-hidden shadow-[var(--sara-sh-1)] group">
      <div className="flex justify-between items-center bg-black/40 px-4 py-2 border-b border-[var(--sara-line-soft)]">
        <div className="text-[10px] text-[var(--sara-dim)] uppercase font-bold tracking-[0.2em] font-mono">
          {language}
        </div>
        <button
          onClick={handleCopy}
          title="Copy Code"
          className="sara-focus-ring flex items-center gap-1.5 text-xs text-[var(--sara-dim)] hover:text-white transition-colors p-1"
        >
          {copied ? (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-[var(--sara-ok)]"><polyline points="20 6 9 17 4 12"></polyline></svg>
          ) : (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
          )}
          {copied ? <span className="text-[var(--sara-ok)]">Copied</span> : "Copy"}
        </button>
      </div>
      <div className="p-5 overflow-x-auto text-[0.875rem] font-mono sara-scroll bg-black/20">
        <code className="text-[var(--sara-ink-2)]">{code}</code>
      </div>
    </div>
  );
}

function MessageCopyButton({ content }: { content: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <button
      onClick={handleCopy}
      title="Copy Message"
      className="sara-focus-ring absolute bottom-2 right-4 p-1.5 rounded-md text-[var(--sara-faint)] hover:text-white hover:bg-[var(--sara-surface-2)] transition-all opacity-0 group-hover:opacity-100"
    >
      {copied ? (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-[var(--sara-ok)]"><polyline points="20 6 9 17 4 12"></polyline></svg>
      ) : (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
      )}
    </button>
  );
}

function WorkspaceContent() {
  const searchParams = useSearchParams();
  const initialQuery = searchParams.get("q");
  const chatId = searchParams.get("chatId");
  
  const [messages, setMessages] = useState<Message[]>([]);
  const chatEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!chatId) { setMessages([]); }
  }, [chatId]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (!chatId) return;
    const stored = localStorage.getItem("sara_chats");
    if (stored) {
      const parsed = JSON.parse(stored);
      const chat = parsed.find((c: LocalChat) => c.id === chatId);
      if (chat && messages.length === 0) {
        setMessages([
          { id: '1', role: 'user', content: chat.title }, 
          { id: '2', role: 'ai', content: "I have restored this conversation. How can I assist you further?" }
        ]);
      }
    }
  }, [chatId]);

  const isNewChat = messages.length === 0 && !chatId;

  return (
    <div className="relative w-full h-full flex flex-col overflow-hidden bg-transparent">
      
      {/* Background Video from Fastshot */}
      <video 
        autoPlay muted loop playsInline 
        className={`absolute inset-0 w-full h-full object-cover z-0 transition-opacity duration-1000 ${isNewChat ? 'opacity-[0.85]' : 'opacity-20'}`}
        src="https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260826_124724_bc041163-d651-425f-aea3-2acc1efc2c96.mp4"
      />

      {/* ── EMPTY HERO STATE ── */}
      <div 
        className={`absolute inset-x-0 flex flex-col items-center justify-center px-4 transition-all duration-[800ms] ease-[var(--sara-ease-slow)] z-10 ${isNewChat ? 'top-[35%] opacity-100 translate-y-0 pointer-events-auto' : 'top-[10%] opacity-0 -translate-y-10 pointer-events-none'}`}
      >
        <h1 className="text-[2.4rem] md:text-[3.25rem] text-white mb-3 text-center tracking-tight font-medium" style={{ textShadow: '0 2px 22px rgba(0,0,0,.30)' }}>
          Describe a task. We'll execute it.
        </h1>
        <p className="text-center max-w-[600px] text-[1.05rem] text-gray-300 drop-shadow-md">
          Ask SARA a question, drop a file, or orchestrate your intelligent agents.
        </p>
      </div>

      {/* ── MAIN CHAT LOG ── */}
      <div 
        className={`relative z-10 flex-1 overflow-y-auto px-6 pt-12 pb-[200px] w-full sara-scroll transition-all duration-[700ms] ${isNewChat ? 'opacity-0 translate-y-10 pointer-events-none' : 'opacity-100 translate-y-0 pointer-events-auto'}`}
      >
        <div className="max-w-[56rem] mx-auto flex flex-col gap-8 pt-4">
          
          <AgentInfoBanner 
            agentName="SARA General Interface" 
            defaultDescription="This agent can assist with general analysis, file processing, and exploring database entries directly from this workspace." 
            capabilities={["File Analysis", "General Chat", "Data Queries"]} 
          />

          {messages.map((msg) => (
            <div key={msg.id} className={`flex w-full ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
              <div className={`flex gap-4 max-w-[85%] md:max-w-[75%] ${msg.role === "user" ? "flex-row-reverse" : "flex-row"}`}>
                
                {/* Avatar */}
                <div className={`w-10 h-10 rounded-full flex items-center justify-center shrink-0 font-mono text-xs font-bold tracking-widest shadow-md ${
                  msg.role === "user" 
                    ? "bg-white text-black" 
                    : "sara-surface-raised border border-[var(--sara-accent-glow)] text-[var(--sara-accent)] shadow-[0_0_15px_var(--sara-accent-glow)]"
                }`}>
                  {msg.role === "user" ? "ME" : "SA"}
                </div>

                {/* Bubble */}
                <div className={`group relative px-6 py-5 pb-10 text-[0.9375rem] leading-relaxed shadow-[var(--sara-sh-3)] rounded-[1.75rem] ${
                  msg.role === "user" 
                    ? "rounded-tr-[6px] bg-[rgba(255,255,255,0.08)] backdrop-blur-[16px] text-white border border-[rgba(255,255,255,0.12)]" 
                    : "rounded-tl-[6px] sara-surface text-[var(--sara-ink)]"
                }`}>
                  {msg.role === "user" ? (
                    <div className="whitespace-pre-wrap">{msg.content}</div>
                  ) : (
                    <ReactMarkdown 
                      remarkPlugins={[remarkGfm]}
                      components={{
                        code({ node, inline, className, children, ...props }: any) {
                          const match = /language-(\w+)/.exec(className || "");
                          return !inline && match ? (
                            <CodeBlock language={match[1]} code={String(children).replace(/\n$/, '')} />
                          ) : (
                            <code className="bg-[rgba(255,255,255,0.06)] text-[#e879f9] px-1.5 py-0.5 rounded-md text-[0.85em] font-mono" {...props}>{children}</code>
                          );
                        },
                        p: ({children}) => <p className="mb-4 last:mb-0">{children}</p>,
                        ul: ({children}) => <ul className="list-disc pl-6 mb-4">{children}</ul>,
                        ol: ({children}) => <ol className="list-decimal pl-6 mb-4">{children}</ol>,
                        h3: ({children}) => <h3 className="text-[1.25rem] font-semibold mt-6 mb-3 text-white">{children}</h3>,
                        strong: ({children}) => <strong className="font-semibold text-white">{children}</strong>,
                      }}
                    >
                      {msg.content}
                    </ReactMarkdown>
                  )}
                  
                  <MessageCopyButton content={msg.content} />
                </div>
              </div>
            </div>
          ))}
          <div ref={chatEndRef} className="h-4" />
        </div>
      </div>

      {/* ── THE CLOUD OMNIBAR ── */}
      <div 
        className={`absolute inset-x-0 z-50 flex justify-center px-4 md:px-10 transition-all duration-[900ms] ease-[var(--sara-ease-slow)] ${isNewChat ? 'top-[55%] -translate-y-1/2' : 'bottom-6'}`}
      >
         <div className="w-full max-w-[56rem] pointer-events-auto">
           <ChatInterface setMessages={setMessages} initialQuery={initialQuery || undefined} />
         </div>
      </div>

    </div>
  );
}

export default function WorkspacePage() {
  return (
    <Suspense fallback={<div className="h-screen w-full bg-[var(--sara-bg)]"></div>}>
      <WorkspaceContent />
    </Suspense>
  );
}