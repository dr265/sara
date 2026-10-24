"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createOrbScene, type OrbSceneApi } from "@/lib/orbScene";
import { HandTracker, type TrackerStatus } from "@/lib/handTracker";
import { SARA_API_URL, saraFetch } from "@/lib/api";
import "./sara-orb.css";

type CameraState = "off" | "starting" | "on" | "error";
type HistoryEntry = {
  id: string;
  role: "user" | "sara";
  text: string;
  type?: "info" | "success" | "error";
  payload?: { recipient: string; subject: string; body: string };
};

const MODE_LABEL: Record<TrackerStatus["mode"], string> = {
  idle: "STANDBY",
  spin: "SPIN",
  zoom: "ZOOM",
};

export default function SaraOrbPage() {
  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const overlayRef = useRef<HTMLCanvasElement>(null);
  const sceneRef = useRef<OrbSceneApi | null>(null);
  const trackerRef = useRef<HandTracker | null>(null);
  const chatBottomRef = useRef<HTMLDivElement>(null);
  
  // --- File Upload Ref & State ---
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);

  const [camera, setCamera] = useState<CameraState>("off");
  const [status, setStatus] = useState<TrackerStatus>({ hands: 0, mode: "idle" });
  const [error, setError] = useState<string | null>(null);

  // --- History State ---
  const [command, setCommand] = useState("");
  const [isProcessing, setIsProcessing] = useState(false);
  const [history, setHistory] = useState<HistoryEntry[]>([
    { id: "init", role: "sara", text: "SYSTEM STANDBY. WAITING FOR DIRECTIVE.", type: "info" }
  ]);
  
  const [isListening, setIsListening] = useState(false);
  const recognitionRef = useRef<any>(null);
  const silenceTimerRef = useRef<any>(null); 

  // Auto-scroll chat history to bottom
  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [history]);

  const speakResponse = (text: string) => {
    if (typeof window !== "undefined" && "speechSynthesis" in window) {
      window.speechSynthesis.cancel(); 
      const utterance = new SpeechSynthesisUtterance(text);
      const voices = window.speechSynthesis.getVoices();
      const aiVoice = voices.find(v => 
        v.name.includes("Zira") || v.name.includes("Samantha") || v.name.includes("Google UK English Female")
      );
      if (aiVoice) utterance.voice = aiVoice;
      utterance.pitch = 1.1; 
      utterance.rate = 1.05; 
      window.speechSynthesis.speak(utterance);
    }
  };

  useEffect(() => {
    if (typeof window !== "undefined") {
      const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
      if (SpeechRecognition) {
        const recognition = new SpeechRecognition();
        recognition.continuous = true; 
        recognition.interimResults = true;
        recognition.lang = "en-US";
        recognition.onstart = () => { silenceTimerRef.current = setTimeout(() => recognition.stop(), 5000); };
        recognition.onresult = (event: any) => {
          clearTimeout(silenceTimerRef.current);
          silenceTimerRef.current = setTimeout(() => recognition.stop(), 5000);
          let fullTranscript = "";
          for (let i = 0; i < event.results.length; i++) {
            fullTranscript += event.results[i][0].transcript;
          }
          setCommand(fullTranscript);
        };
        recognition.onerror = () => { clearTimeout(silenceTimerRef.current); setIsListening(false); };
        recognition.onend = () => { clearTimeout(silenceTimerRef.current); setIsListening(false); };
        recognitionRef.current = recognition;
      }
    }
  }, []);

  const toggleListening = () => {
    if (isListening) {
      recognitionRef.current?.stop();
      setIsListening(false);
    } else {
      setCommand(""); 
      recognitionRef.current?.start();
      setIsListening(true);
    }
  };

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const scene = createOrbScene(container);
    sceneRef.current = scene;
    return () => {
      trackerRef.current?.stop();
      scene.dispose();
    };
  }, []);

  const stopGestures = useCallback(() => {
    trackerRef.current?.stop();
    trackerRef.current = null;
    setCamera("off");
    setStatus({ hands: 0, mode: "idle" });
  }, []);

  const startGestures = useCallback(async () => {
    const video = videoRef.current;
    const overlay = overlayRef.current;
    if (!video || !overlay || trackerRef.current) return;
    setCamera("starting");
    setError(null);
    const tracker = new HandTracker(video, overlay, {
      onRotate: (dt, dp) => sceneRef.current?.rotateBy(dt, dp),
      onZoom: (factor) => sceneRef.current?.zoomBy(factor),
      onStatus: setStatus,
    });
    trackerRef.current = tracker;
    try {
      await tracker.start();
      setCamera("on");
    } catch (err) {
      trackerRef.current = null;
      tracker.stop();
      setCamera("error");
    }
  }, []);

  const toggleGestures = useCallback(() => {
    if (trackerRef.current) stopGestures();
    else void startGestures();
  }, [startGestures, stopGestures]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (document.activeElement?.tagName === "INPUT") return;
      switch (e.key) {
        case "+": case "=": sceneRef.current?.zoomIn(); break;
        case "-": case "_": sceneRef.current?.zoomOut(); break;
        case "r": case "R": sceneRef.current?.resetView(); break;
        case "g": case "G": toggleGestures(); break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleGestures]);

  // --- File Handler ---
  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setSelectedFile(e.target.files[0]);
    }
  };

  const handleCommandSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!command.trim() && !selectedFile) return;

    if (isListening) {
      recognitionRef.current?.stop();
      setIsListening(false);
    }

    const userText = command.trim() || `[Attached File: ${selectedFile?.name}]`;
    setHistory(prev => [...prev, { id: Date.now().toString(), role: "user", text: userText }]);

    setIsProcessing(true);
    const spinInterval = setInterval(() => { sceneRef.current?.rotateBy(0.5, 0); }, 50);

    try {
      let res;
      
      // IF A FILE IS ATTACHED: Send it to our Knowledge Ingestion endpoint
      if (selectedFile) {
        const formData = new FormData();
        formData.append("file", selectedFile);
        
        res = await saraFetch(`${SARA_API_URL}/api/voice/upload-knowledge`, {
          method: "POST",
          body: formData, 
        });
      } 
      // IF JUST A TEXT COMMAND: Send to the execute endpoint
      else {
        res = await saraFetch(`${SARA_API_URL}/api/voice/execute`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ 
              user_id: "user_123", 
              command: command,
          }) 
        });
      }
      
      const data = await res.json();
      
      if (data.status === "success") {
        const successEntry: HistoryEntry = {
          id: (Date.now() + 1).toString(),
          role: "sara",
          text: `SUCCESS: ${data.message}`,
          type: "success",
          payload: data.agent_payload
        };
        setHistory(prev => [...prev, successEntry]);
        
        speakResponse(data.message.length > 80 ? "Directive completed successfully." : data.message); 
      } else {
        const errorMsg = data?.message || "Unknown error occurred.";
        setHistory(prev => [...prev, { id: (Date.now() + 1).toString(), role: "sara", text: `FAILED: ${errorMsg}`, type: "error" }]);
        speakResponse("I encountered an error processing the directive."); 
      }
    } catch (err) {
      setHistory(prev => [...prev, { id: (Date.now() + 1).toString(), role: "sara", text: "CRITICAL ERROR: Failed to reach backend.", type: "error" }]);
      speakResponse("Critical error. Cannot connect to server."); 
    } finally {
      clearInterval(spinInterval);
      setIsProcessing(false);
      setCommand("");
      setSelectedFile(null); 
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const cameraOn = camera === "on";

  return (
    <div className="sara-orb-viewport">
      <div ref={containerRef} className="orb-root" />
      <div className="overlay-vignette" />
      <div className="overlay-grain" />
      <div className="overlay-scanlines" />
      <div className="hud hud-title">S.A.R.A. &nbsp; A.I.</div>

      <div className="hud-terminal">
        {/* --- Scrollable History Display --- */}
        <div className="terminal-history">
          {history.map((entry) => (
            <div key={entry.id} className={`terminal-log ${entry.role} ${entry.type || ""}`}>
              {entry.role === "user" ? "> YOU: " : "> SARA: "}{entry.text}
              
              {entry.payload && entry.payload.recipient && (
                <div className="payload-box">
                  <div><span className="payload-label">TO:</span> {entry.payload.recipient}</div>
                  <div><span className="payload-label">SUBJECT:</span> {entry.payload.subject}</div>
                  <div><span className="payload-label">BODY:</span> {entry.payload.body}</div>
                </div>
              )}
            </div>
          ))}
          <div ref={chatBottomRef} />
        </div>

        <form onSubmit={handleCommandSubmit} className="command-form">
          {/* --- File Attachment Button --- */}
          <button type="button" className="attach-btn" onClick={() => fileInputRef.current?.click()} title="Upload Document">
            +
          </button>
          <input 
            type="file" 
            ref={fileInputRef} 
            onChange={handleFileChange} 
            style={{ display: "none" }} 
            accept=".pdf,.txt,.csv,.json"
          />

          {/* Show a badge if a file is attached */}
          {selectedFile && (
            <div className="file-badge" title={selectedFile.name}>
              📄 {selectedFile.name}
            </div>
          )}

          <input 
            type="text" 
            className="command-input" 
            placeholder="Type directive or speak..." 
            value={command}
            onChange={(e) => setCommand(e.target.value)}
            disabled={isProcessing}
            autoComplete="off"
          />
          
          <button type="button" className={`mic-btn ${isListening ? "listening" : ""}`} onClick={toggleListening} disabled={isProcessing}>
            {isListening ? "🛑" : "🎙️"}
          </button>
          
          <button type="submit" className="command-btn" disabled={isProcessing || (!command.trim() && !selectedFile)}>
            {isProcessing ? "..." : "EXEC"}
          </button>
        </form>
      </div>

      {/* --- HUD Controls --- */}
      <div className="hud hud-controls">
        <div className={`camera-panel${cameraOn ? " visible" : ""}`}>
          <video ref={videoRef} muted playsInline className="camera-video" />
          <canvas ref={overlayRef} width={208} height={156} className="camera-overlay" />
        </div>
        {error && <div className="hud-error">{error}</div>}
        <div className="hud-row">
          <button type="button" className="hud-btn" onClick={toggleGestures}>{cameraOn ? "GESTURES ON" : "GESTURES OFF"}</button>
        </div>
      </div>
    </div>
  );
}