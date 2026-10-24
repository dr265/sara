"use client";

import { useState, useEffect, useRef } from "react";
import type { Message } from "@/app/workspace/page";
import { useSearchParams, useRouter } from "next/navigation";
import { SARA_API_URL, saraFetch } from "@/lib/api";

interface ChatInterfaceProps {
  userId?: string;
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>;
  initialQuery?: string;
}

declare global {
  interface Window {
    SpeechRecognition: any;
    webkitSpeechRecognition: any;
  }
}

export default function ChatInterface({ userId, setMessages, initialQuery }: ChatInterfaceProps) {
  const router = useRouter(); 
  const searchParams = useSearchParams();
  const voiceParam = searchParams.get("voice");
  const currentChatId = searchParams.get("chatId");
  
  const [query, setQuery] = useState(initialQuery || "");
  const [isLoading, setIsLoading] = useState(false);
  const [hasAutoSearched, setHasAutoSearched] = useState(false);
  
  const [isListening, setIsListening] = useState(false);
  const recognitionRef = useRef<any>(null);

  useEffect(() => {
    if (typeof window !== "undefined" && (window.SpeechRecognition || window.webkitSpeechRecognition)) {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      recognitionRef.current = new SpeechRecognition();
      recognitionRef.current.continuous = false;
      recognitionRef.current.interimResults = true;

      recognitionRef.current.onresult = (event: any) => {
        let currentTranscript = "";
        for (let i = event.resultIndex; i < event.results.length; i++) {
          currentTranscript += event.results[i][0].transcript;
        }
        setQuery(currentTranscript);
      };

      recognitionRef.current.onerror = (event: any) => {
        if (event.error === 'not-allowed') {
          console.warn("Microphone access denied by user.");
        } else {
          console.warn("Speech recognition error", event.error);
        }
        setIsListening(false);
      };

      recognitionRef.current.onend = () => {
        setIsListening(false);
      };
    }
  }, []);

  useEffect(() => {
    if (initialQuery && !hasAutoSearched) {
      setHasAutoSearched(true);
      handleAsk(undefined, initialQuery);
    }
  }, [initialQuery, hasAutoSearched]);

  useEffect(() => {
    if (voiceParam === 'true' && recognitionRef.current && !isListening) {
      toggleListen();
    }
  }, [voiceParam]);

  const toggleListen = () => {
    if (isListening) {
      recognitionRef.current?.stop();
      setIsListening(false);
    } else {
      setQuery("");
      recognitionRef.current?.start();
      setIsListening(true);
    }
  };

  const handleAsk = async (e?: React.FormEvent, forceQuery?: string) => {
    if (e) e.preventDefault();
    if (isListening) {
      recognitionRef.current?.stop();
      setIsListening(false);
    }

    const textToSearch = forceQuery || query;
    if (!textToSearch.trim()) return;

    let activeChatId = currentChatId;
    if (!activeChatId) {
       activeChatId = Date.now().toString();
       try {
         const existing = JSON.parse(localStorage.getItem("sara_chats") || "[]");
         const newChat = {
           id: activeChatId,
           title: textToSearch.substring(0, 30) + (textToSearch.length > 30 ? "..." : ""),
           updatedAt: Date.now()
         };
         localStorage.setItem("sara_chats", JSON.stringify([newChat, ...existing]));
         window.dispatchEvent(new Event("chatsUpdated"));
         router.push(`/workspace?chatId=${activeChatId}`);
       } catch(err) { console.error(err); }
    }

    const userMsg: Message = { id: Date.now().toString(), role: "user", content: textToSearch };
    setMessages((prev) => [...prev, userMsg]);
    
    setQuery(""); 
    setIsLoading(true);

    try {
      const tempAiId = (Date.now() + 1).toString();
      setMessages((prev) => [...prev, { id: tempAiId, role: "ai", content: "Thinking..." }]);

      const res = await saraFetch(`${SARA_API_URL}/api/chat/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ 
          user_id: userId || "user", 
          chat_id: activeChatId, 
          question: textToSearch 
        }),
      });

      if (!res.ok) throw new Error(`Backend returned ${res.status}`);
      const data = await res.json();
      
      setMessages((prev) => 
        prev.map((msg) => msg.id === tempAiId ? { ...msg, content: data.answer } : msg)
      );
    } catch (error) {
      console.warn("Backend Error:", error);
      setMessages((prev) => [...prev, { id: Date.now().toString(), role: "ai", content: "Sorry, I encountered an error connecting to the AI. Is the SARA backend running? (python run_backend.py — default port 8000)" }]);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div
      className={`w-full max-w-[708px] mx-auto relative flex flex-col transition-all duration-500 ease-in-out p-4 pt-5 pb-3 gap-3 ${
        isListening 
          ? "bg-[rgba(25,25,28,0.95)] border border-[var(--sara-accent)] shadow-[0_0_40px_-10px_var(--sara-accent-glow)] rounded-[26px]" 
          : "bg-[rgba(41,41,43,0.955)] shadow-[inset_0_0_0_1px_rgba(214,228,255,0.14),0_22px_60px_rgba(0,0,0,0.30)] backdrop-blur-[26px] backdrop-saturate-[112%] rounded-[26px]"
      }`}
    >
      <form onSubmit={(e) => handleAsk(e)} className="w-full flex flex-col m-0 p-0 border-none outline-none relative">
        
        {/* Input Area */}
        <textarea
          className={`w-full bg-transparent border-none outline-none shadow-none resize-none px-3 py-1 text-[15px] font-medium leading-[1.4] ${
            isListening ? "text-[var(--sara-accent)]" : "text-[#E1E1E2]"
          } placeholder:text-[#8B8C8E] min-h-[50px] scrollbar-none`}
          placeholder={isListening ? "Listening... Speak now" : "Build a fintech tracking app with bank level privacy and..."}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          disabled={isLoading}
          aria-label="Ask SARA anything"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              handleAsk(e);
            }
          }}
        />

        {/* Bottom Toolbar Row */}
        <div className="flex items-center justify-between mt-1 relative">
          
          {/* Left: Chips */}
          <div className="flex items-center gap-[5.5px] overflow-x-auto scrollbar-none max-w-[50%] md:max-w-[60%]">
            <button type="button" className="shrink-0 h-[30px] rounded-[9px] px-3 flex items-center gap-1.5 text-[11px] font-medium text-[#909093] hover:text-[#c8c8cb] transition-colors border border-[rgba(255,255,255,0.05)] bg-[linear-gradient(180deg,rgba(255,255,255,0.088)_0%,rgba(255,255,255,0.050)_45%,rgba(255,255,255,0.038)_100%)] hover:bg-[linear-gradient(180deg,rgba(255,255,255,0.14)_0%,rgba(255,255,255,0.07)_100%)]">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2h12c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>
              <span className="translate-y-[1px]">Attach Screens</span>
            </button>
            <button type="button" className="shrink-0 h-[30px] rounded-[9px] px-3 flex items-center gap-1.5 text-[11px] font-medium text-[#909093] hover:text-[#c8c8cb] transition-colors border border-[rgba(255,255,255,0.05)] bg-[linear-gradient(180deg,rgba(255,255,255,0.088)_0%,rgba(255,255,255,0.050)_45%,rgba(255,255,255,0.038)_100%)] hover:bg-[linear-gradient(180deg,rgba(255,255,255,0.14)_0%,rgba(255,255,255,0.07)_100%)] hidden sm:flex">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-5 14H7v-2h7v2zm3-4H7v-2h10v2zm0-4H7V7h10v2z"/></svg>
              <span className="translate-y-[1px]">Attach a Figma</span>
            </button>
          </div>

          {/* Right Cluster */}
          <div className="flex items-center gap-3 md:gap-4 shrink-0 translate-y-1">
            
            {/* Model Label */}
            <div className="flex items-center gap-1.5 text-[#98999C] text-[11px] font-medium cursor-pointer hover:text-white transition-colors">
              <span className="translate-y-[0.5px]">Gemini 3.1 Pro</span>
              <svg width="7" height="7" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6"/></svg>
            </div>

            {/* Attach Button */}
            <label 
              htmlFor="workspace-file-upload" 
              className="text-[#A9AAAD] hover:text-white cursor-pointer transition-colors p-1"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
            </label>
            <input
              type="file"
              id="workspace-file-upload"
              className="hidden"
              onChange={(e) => {
                if (e.target.files && e.target.files.length > 0) {
                  alert(`File selected: ${e.target.files[0].name}.`);
                }
              }}
            />

            {/* Voice Toggle */}
            <button
              type="button"
              onClick={toggleListen}
              className={`w-7 h-7 rounded-full flex items-center justify-center transition-colors ${
                isListening ? "bg-[var(--sara-accent-glow)] text-[var(--sara-accent)]" : "text-[#A9AAAD] hover:text-white"
              }`}
              aria-label="Voice input"
            >
               <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" y1="19" x2="12" y2="22"></line></svg>
            </button>

            {/* Send Circle Button */}
            <button
              type="submit"
              disabled={isLoading || (!query.trim() && !isListening)}
              className="w-[35px] h-[35px] rounded-full flex items-center justify-center transition-all bg-[linear-gradient(163deg,#FBBC94_0%,#F49D70_46%,#E88654_100%)] shadow-[0_3px_12px_rgba(210,110,60,0.34)] hover:brightness-110 active:scale-95 disabled:opacity-50 disabled:grayscale disabled:cursor-not-allowed translate-y-[2px]"
              aria-label="Send"
            >
              {isLoading ? (
                <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              ) : (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
              )}
            </button>
            
          </div>
        </div>
      </form>
    </div>
  );
}