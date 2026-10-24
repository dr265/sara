"use client";

import { useEffect, useRef, useState } from "react";
import { SARA_API_URL, saraFetch } from "@/lib/api";

export default function VoiceAgentPage() {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const rafRef = useRef<number>(0);
  
  const [isListening, setIsListening] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [aiResponse, setAiResponse] = useState("");
  const [inputQuery, setInputQuery] = useState("");
  const recognitionRef = useRef<any>(null);

  useEffect(() => {
    if (typeof window !== "undefined" && (window.SpeechRecognition || window.webkitSpeechRecognition)) {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      recognitionRef.current = new SpeechRecognition();
      recognitionRef.current.continuous = false;
      recognitionRef.current.interimResults = true;

      recognitionRef.current.onresult = (event: any) => {
        let currentText = "";
        for (let i = event.resultIndex; i < event.results.length; i++) {
          currentText += event.results[i][0].transcript;
        }
        setTranscript(currentText);
        setInputQuery(currentText);
      };

      recognitionRef.current.onend = () => {
        setIsListening(false);
        stopAudioVisualizer();
      };
    }
    return () => stopAudioVisualizer();
  }, []); // Fixed static dependency array to prevent React render errors

  const startListening = async () => {
    setTranscript("");
    setAiResponse("");
    setIsListening(true);
    recognitionRef.current?.start();

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioContextRef.current = new (window.AudioContext || (window as any).webkitAudioContext)();
      analyserRef.current = audioContextRef.current.createAnalyser();
      analyserRef.current.fftSize = 256;
      
      const source = audioContextRef.current.createMediaStreamSource(stream);
      source.connect(analyserRef.current);
      
      visualizeAudio();
    } catch (err) {
      console.error("Microphone access denied", err);
    }
  };

  const stopAudioVisualizer = () => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    if (audioContextRef.current?.state !== 'closed') audioContextRef.current?.close();
    if (iframeRef.current) {
      iframeRef.current.contentWindow?.postMessage({ type: 'MIC_VOLUME', value: 0 }, '*');
    }
  };

  const visualizeAudio = () => {
    if (!analyserRef.current || !iframeRef.current) return;
    const dataArray = new Uint8Array(analyserRef.current.frequencyBinCount);
    analyserRef.current.getByteFrequencyData(dataArray);
    
    const averageVolume = dataArray.reduce((a, b) => a + b) / dataArray.length;
    iframeRef.current.contentWindow?.postMessage({ type: 'MIC_VOLUME', value: averageVolume }, '*');
    
    rafRef.current = requestAnimationFrame(visualizeAudio);
  };

  const executeCommand = async (textToProcess: string) => {
    if (!textToProcess.trim()) return;

    setAiResponse("SARA is processing your command...");
    
    try {
        const res = await saraFetch(`${SARA_API_URL}/api/voice/execute`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ user_id: "user_123", command: textToProcess }),
});
      
      const data = await res.json();
      if (res.ok) {
        setAiResponse(`${data.message}\n\nExecution Payload:\n${JSON.stringify(data.agent_payload, null, 2)}`);
      } else {
        setAiResponse(`Error: ${data.detail || "Failed to execute command."}`);
      }
    } catch (error) {
      setAiResponse("Connection error: Make sure FastAPI backend is running on port 5000.");
    }
  };

  const handleSendClick = () => {
    executeCommand(inputQuery);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      executeCommand(inputQuery);
    }
  };

  return (
    <div style={{ position: 'relative', width: '100%', height: '100vh', overflow: 'hidden', backgroundColor: '#04050c' }}>
      
      <iframe 
        ref={iframeRef}
        src="/voice-tunnel.html" 
        style={{ 
          position: 'absolute', top: 0, left: 0, width: '100%', height: '100%', border: 'none', zIndex: 0,
          opacity: isListening ? 1 : 0.25, transform: isListening ? 'scale(1)' : 'scale(0.98)',
          transition: 'all 0.8s cubic-bezier(0.16,1,0.3,1)', pointerEvents: 'none'
        }}
      />
      
      <div style={{ 
        position: 'absolute', left: 0, right: 0, top: '22%', display: 'flex', flexDirection: 'column', 
        alignItems: 'center', justifyContent: 'center', padding: '0 24px', zIndex: 10, pointerEvents: 'none' 
      }}>
        <h1 style={{ fontSize: '2.5rem', fontWeight: 600, color: '#ffffff', marginBottom: '16px', textShadow: '0 10px 30px rgba(0,0,0,0.5)' }}>
          {isListening ? "Listening..." : "Voice Interface Agent"}
        </h1>
        <p style={{ color: '#b9becf', fontSize: '1.125rem', textAlign: 'center', maxWidth: '600px', marginBottom: '24px' }}>
          {transcript || "Type your instruction below or click the microphone to speak."}
        </p>

        {aiResponse && (
          <div style={{ 
            width: '100%', maxWidth: '700px', padding: '20px', borderRadius: '16px', fontSize: '1rem', color: '#e879f9', lineHeight: 1.6, whiteSpace: 'pre-wrap',
            backgroundColor: 'rgba(10, 10, 15, 0.85)', backdropFilter: 'blur(20px)', border: '1px solid rgba(217,70,239,0.3)', pointerEvents: 'auto',
            maxHeight: '30vh', overflowY: 'auto'
          }}>
            {aiResponse}
          </div>
        )}
      </div>

      <div style={{ position: 'absolute', left: 0, right: 0, bottom: '40px', zIndex: 50, display: 'flex', justifyContent: 'center', padding: '0 40px' }}>
        <div style={{ width: '100%', maxWidth: '900px', pointerEvents: 'auto', display: 'flex', alignItems: 'center', gap: '12px', background: 'rgba(15, 15, 25, 0.75)', border: '1px solid rgba(255, 255, 255, 0.15)', borderRadius: '24px', padding: '12px 20px', backdropFilter: 'blur(24px)', boxShadow: '0 20px 40px rgba(0,0,0,0.4)' }}>
          
          <input 
            type="text" 
            placeholder="Write a mail to recipient@gmail.com for meeting at 5am..." 
            value={inputQuery}
            onChange={(e) => setInputQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            style={{ flex: 1, background: 'transparent', border: 'none', color: '#ffffff', fontSize: '1rem', outline: 'none' }}
          />

          <button 
            onClick={isListening ? () => recognitionRef.current?.stop() : startListening}
            title={isListening ? "Stop listening" : "Start voice input"}
            style={{
              width: '40px', height: '40px', borderRadius: '50%', border: 'none', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
              backgroundColor: isListening ? '#e879f9' : 'rgba(255,255,255,0.1)',
              color: isListening ? '#04050c' : '#ffffff',
              transition: 'all 0.3s ease'
            }}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" y1="19" x2="12" y2="22"></line></svg>
          </button>

          <button 
            onClick={handleSendClick}
            title="Execute command"
            style={{
              padding: '10px 18px', borderRadius: '16px', border: 'none', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '6px',
              backgroundColor: '#ffffff', color: '#04050c', fontWeight: 600, fontSize: '0.9rem',
              transition: 'all 0.2s ease'
            }}
          >
            <span>Send</span>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
          </button>

        </div>
      </div>

    </div>
  );
}