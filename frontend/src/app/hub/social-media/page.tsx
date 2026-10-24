'use client';

import React, { useState, useEffect, useRef, useCallback } from 'react';
import * as THREE from 'three';
import { motion, AnimatePresence } from 'framer-motion';
import { 
  Camera, Users, Briefcase, MonitorPlay, MessageSquare, Box, 
  BrainCircuit, Database, FileText, UploadCloud, Rocket,
  Play, Pause, RefreshCw, CheckCircle2, XCircle, AlertCircle, Music
} from 'lucide-react';
import { SOCIAL_API_URL } from "@/lib/api";

async function readSocialJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${SOCIAL_API_URL}${path}`, init);
  if (!response.ok) {
    let detail = `Social API request failed (${response.status})`;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch { /* use the status fallback */ }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

// --- 3D Background Component ---
const ThreeBackground = () => {
  const mountRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!mountRef.current) return;
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1000);
    const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
    
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    mountRef.current.appendChild(renderer.domElement);

    const geometry = new THREE.BufferGeometry();
    const particlesCount = 1500;
    const posArray = new Float32Array(particlesCount * 3);
    for(let i = 0; i < particlesCount * 3; i++) {
        posArray[i] = (Math.random() - 0.5) * 18; 
    }
    geometry.setAttribute('position', new THREE.BufferAttribute(posArray, 3));
    
    const material = new THREE.PointsMaterial({
        size: 0.018,
        color: 0x5df0a8,
        transparent: true,
        opacity: 0.5,
        blending: THREE.AdditiveBlending
    });
    
    const particlesMesh = new THREE.Points(geometry, material);
    scene.add(particlesMesh);
    camera.position.z = 5.5;

    let mouseX = 0;
    let mouseY = 0;
    const handleMouseMove = (event: MouseEvent) => {
        mouseX = (event.clientX / window.innerWidth) - 0.5;
        mouseY = (event.clientY / window.innerHeight) - 0.5;
    };
    window.addEventListener('mousemove', handleMouseMove);

    let animationFrameId: number;
    const animate = () => {
        animationFrameId = requestAnimationFrame(animate);
        particlesMesh.rotation.y += 0.00045;
        particlesMesh.rotation.x += 0.00018;
        particlesMesh.rotation.y += mouseX * 0.005;
        particlesMesh.rotation.x += mouseY * 0.005;
        renderer.render(scene, camera);
    };
    animate();

    const handleResize = () => {
        camera.aspect = window.innerWidth / window.innerHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(window.innerWidth, window.innerHeight);
    };
    window.addEventListener('resize', handleResize);

    return () => {
        window.removeEventListener('mousemove', handleMouseMove);
        window.removeEventListener('resize', handleResize);
        cancelAnimationFrame(animationFrameId);
        if (mountRef.current) mountRef.current.removeChild(renderer.domElement);
        geometry.dispose();
        material.dispose();
        renderer.dispose();
    };
  }, []);

  return <div ref={mountRef} className="fixed inset-0 z-0 pointer-events-none opacity-[0.28]" />;
};

const GlassPanel = ({ children, title, icon: Icon, delay = 0, className = "", extraHeader }: any) => (
  <motion.div 
    initial={{ opacity: 0, y: 20 }}
    animate={{ opacity: 1, y: 0 }}
    transition={{ duration: 0.6, delay, ease: "easeOut" }}
    className={`bg-[rgba(12,18,38,.55)] backdrop-blur-2xl border border-white/10 rounded-[18px] p-5 shadow-[0_24px_70px_rgba(0,0,0,.28)] overflow-hidden relative group ${className}`}
  >
    <div className="absolute inset-0 bg-[radial-gradient(120%_120%_at_0%_0%,rgba(93,240,168,.09),transparent_45%)] opacity-0 group-hover:opacity-100 transition-opacity duration-700 pointer-events-none" />
    
    {title && (
      <div className="flex items-center gap-2 mb-4 border-b border-white/5 pb-3 relative z-10">
        {Icon && <Icon className="w-4 h-4 text-[#5df0a8]" />}
        <h3 className="text-sm font-bold text-white tracking-wide uppercase">{title}</h3>
        {extraHeader && <div className="ml-auto">{extraHeader}</div>}
      </div>
    )}
    <div className="relative z-10">{children}</div>
  </motion.div>
);

export default function UltraPremiumDashboard() {
  const [accounts, setAccounts] = useState<any[]>([]);
  const [jobs, setJobs] = useState<any[]>([]);
  const [pilot, setPilot] = useState<any | null>(null);
  const [running, setRunning] = useState(true);
  const [syncError, setSyncError] = useState("");
  
  const [commandText, setCommandText] = useState("");
  const [sheetsUrl, setSheetsUrl] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [knowledgeStatus, setKnowledgeStatus] = useState("");
  const [isSyncingSheets, setIsSyncingSheets] = useState(false);
  const [isExecuting, setIsExecuting] = useState(false);

  // New states for Video & Manual Tokens
  const [contentType, setContentType] = useState<"text" | "video">("video"); // Default to video as requested
  const [showTokenModal, setShowTokenModal] = useState<string | null>(null);
  const [loginTab, setLoginTab] = useState<"token" | "password" | "app_keys">("token");
  const [manualToken, setManualToken] = useState("");
  const [manualUsername, setManualUsername] = useState("");
  const [manualPassword, setManualPassword] = useState("");
  const [appClientId, setAppClientId] = useState("");
  const [appClientSecret, setAppClientSecret] = useState("");
  const [redirectUri, setRedirectUri] = useState("");

  const prevAccountsLength = useRef(0);

  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([
      readSocialJson<{ accounts?: any[] }>("/social/accounts"),
      readSocialJson<{ jobs?: any[] }>("/social/jobs?limit=50"),
      readSocialJson<{ pilot?: any }>("/social/pilot"),
      readSocialJson<{ running?: boolean }>("/social/running"),
    ]);
    const [accountsResult, jobsResult, pilotResult, runningResult] = results;
    if (accountsResult.status === "fulfilled" && accountsResult.value.accounts) setAccounts(accountsResult.value.accounts);
    if (jobsResult.status === "fulfilled" && jobsResult.value.jobs) setJobs(jobsResult.value.jobs);
    if (pilotResult.status === "fulfilled" && pilotResult.value.pilot) setPilot(pilotResult.value.pilot);
    if (runningResult.status === "fulfilled" && runningResult.value.running !== undefined) {
      setRunning(Boolean(runningResult.value.running));
    }
    const failed = results.find((result) => result.status === "rejected");
    setSyncError(failed?.status === "rejected" ? failed.reason?.message || "Some Social Media data is unavailable." : "");
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, [refresh]);

  const toggleAutopilot = async (forceState?: boolean) => {
    const newVal = forceState !== undefined ? forceState : !(pilot?.enabled);
    if (pilot?.enabled === newVal) return; // Already in desired state
    
    setPilot((p: any) => p ? { ...p, enabled: newVal } : { enabled: newVal }); 
    try {
      const data = await readSocialJson<{ pilot?: any }>("/social/pilot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: newVal, niche: pilot?.niche || "Default Config" }),
      });
      if (data.pilot) setPilot(data.pilot);
    } catch (e) {
      setPilot((p: any) => p ? { ...p, enabled: !newVal } : { enabled: !newVal });
      setSyncError(e instanceof Error ? e.message : "Could not update autopilot.");
    }
  };

  // Automatically enable autopilot when a new account is connected
  useEffect(() => {
    if (accounts.length > prevAccountsLength.current) {
      if (pilot && !pilot.enabled) {
        toggleAutopilot(true);
      }
    }
    prevAccountsLength.current = accounts.length;
  }, [accounts.length, pilot]);

  const handleExecute = async () => {
    if (!commandText.trim()) return;
    setIsExecuting(true);
    try {
      await readSocialJson("/social/schedule", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workspace_id: "demo_user",
          account_ids: accounts.map(a => a.id),
          content: commandText,
          content_kind: contentType, // Uses 'video' or 'text'
        }),
      });
      setCommandText("");
      setSyncError("");
      await refresh();
    } catch (e) {
      setSyncError(e instanceof Error ? e.message : "Could not schedule the campaign.");
    } finally {
      setIsExecuting(false);
    }
  };

  // Fixed OAuth URL route
  const connectOAuth = (platform: string) => {
    window.location.href = `${SOCIAL_API_URL}/social/oauth/${platform}/login?workspace_id=demo_user`;
  };

  // The redirect URI the backend actually sends to providers (from /social/health).
  // It must be registered EXACTLY in the developer app or the provider refuses
  // the login with a redirect/403 error.
  useEffect(() => {
    if (!showTokenModal) return;
    let cancelled = false;
    readSocialJson<{ redirect_uris?: Record<string, string> }>("/social/health")
      .then((d) => {
        if (!cancelled) setRedirectUri(d.redirect_uris?.[showTokenModal] ?? "");
      })
      .catch(() => {
        if (!cancelled) setRedirectUri("");
      });
    return () => { cancelled = true; };
  }, [showTokenModal]);

  // Manual Token / Password injection
  const submitManualToken = async () => {
    if (!showTokenModal) return;
    if (loginTab === "token" && !manualToken.trim()) return;
    if (loginTab === "password" && (!manualUsername.trim() || !manualPassword.trim())) return;
    
    try {
      const credentials = loginTab === "token" 
        ? { access_token: manualToken }
        : { username: manualUsername, password: manualPassword };

      await readSocialJson("/social/accounts/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workspace_id: "demo_user",
          platform: showTokenModal,
          credentials: credentials,
        }),
      });
      setShowTokenModal(null);
      setManualToken("");
      setManualUsername("");
      setManualPassword("");
      await refresh();
    } catch (e) {
      setSyncError(e instanceof Error ? e.message : "Manual account connection failed.");
    }
  };

  const submitAppKeys = async () => {
    if (!showTokenModal || !appClientId.trim() || !appClientSecret.trim()) return;
    try {
      await readSocialJson("/social/apps", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workspace_id: "demo_user",
          platform: showTokenModal,
          client_id: appClientId,
          client_secret: appClientSecret,
        }),
      });
      setShowTokenModal(null);
      setAppClientId("");
      setAppClientSecret("");
      setKnowledgeStatus("Developer app keys saved. OAuth is ready for this workspace.");
      await refresh();
    } catch (e) {
      setSyncError(e instanceof Error ? e.message : "Could not save developer app keys.");
    }
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setIsUploading(true);
    setKnowledgeStatus("");
    try {
      const contentBase64 = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
        reader.onerror = () => reject(new Error("Could not read the selected file."));
        reader.readAsDataURL(file);
      });
      await readSocialJson("/social/knowledge/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, content_base64: contentBase64, mime: file.type }),
      });
      setKnowledgeStatus(`${file.name} added to the agency knowledge base.`);
    } catch (error) {
      setKnowledgeStatus(error instanceof Error ? error.message : "Knowledge upload failed.");
    } finally {
      setIsUploading(false);
      e.target.value = "";
    }
  };

  const connectSheets = async () => {
    setIsSyncingSheets(true);
    setKnowledgeStatus("");
    try {
      const result = await readSocialJson<{ authorization_url?: string; url?: string }>("/social/integrations/sheets/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: sheetsUrl.trim() || "Social data sheet" }),
      });
      const authorizationUrl = result.authorization_url || result.url;
      if (authorizationUrl) {
        window.location.href = authorizationUrl;
      } else {
        setKnowledgeStatus("Google Sheets authorization is not configured yet.");
      }
    } catch (error) {
      setKnowledgeStatus(error instanceof Error ? error.message : "Google Sheets connection failed.");
    } finally {
      setIsSyncingSheets(false);
    }
  };

  const syncSheets = async () => {
    setIsSyncingSheets(true);
    setKnowledgeStatus("");
    try {
      const result = await readSocialJson<{ rows?: number }>("/social/integrations/sheets/sync", { method: "POST" });
      setKnowledgeStatus(`Synced ${result.rows ?? "the latest"} rows to Google Sheets.`);
    } catch (error) {
      setKnowledgeStatus(error instanceof Error ? error.message : "Google Sheets sync failed.");
    } finally {
      setIsSyncingSheets(false);
    }
  };

  const platformsData = [
    { id: 'instagram', label: 'Instagram', Icon: Camera, color: 'text-pink-500', activeColor: 'bg-pink-500/20' },
                    { id: 'facebook', label: 'Facebook', Icon: Users, color: 'text-[#9aa6c4]', activeColor: 'bg-white/10' },
    { id: 'linkedin', label: 'LinkedIn', Icon: Briefcase, color: 'text-[#9aa6c4]', activeColor: 'bg-white/10' },
    { id: 'x', label: 'X (Twitter)', Icon: MessageSquare, color: 'text-neutral-300', activeColor: 'bg-neutral-500/20' },
    { id: 'tiktok', label: 'TikTok', Icon: Music, color: 'text-teal-400', activeColor: 'bg-teal-400/20' },
    { id: 'youtube', label: 'YouTube', Icon: MonitorPlay, color: 'text-red-500', activeColor: 'bg-red-500/20' },
  ];

  const stats = {
    connected: accounts.length,
    success: jobs.filter(j => j.status === 'success').length,
    queue: jobs.filter(j => j.status === 'pending' || j.status === 'processing').length,
    failed: jobs.filter(j => j.status === 'failed').length,
  };

  return (
    <div className="relative min-h-[calc(100vh-64px)] w-full text-white font-sans overflow-hidden bg-[#04060f]">
      <ThreeBackground />

      <div className="relative z-10 w-full h-full p-4 lg:p-8 max-w-[1600px] mx-auto space-y-6">
        
        <motion.div 
          initial={{ opacity: 0, y: -20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6 }}
          className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4"
        >
          <div>
            <div className="mb-3 flex items-center gap-3 text-[10px] font-semibold uppercase tracking-[0.24em] text-[#5df0a8]">
              <span className="text-base leading-none">▲</span>
              ASCEND / SOCIAL OPERATIONS
            </div>
            <h1 className="max-w-2xl text-4xl font-extrabold leading-[1.02] tracking-[-0.055em] text-white sm:text-5xl">
              Scale your signal,<br /><span className="bg-gradient-to-b from-[#5df0a8] to-[#2fd38a] bg-clip-text text-transparent">not your busywork.</span>
            </h1>
            <p className="mt-4 max-w-xl text-base leading-7 text-[#9aa6c4]">Run capture, creation, scheduling, and publishing from one clear command surface built for fast-moving teams.</p>
          </div>
          
          <div className="flex items-center gap-4 rounded-full border border-white/10 bg-[rgba(16,24,48,.65)] px-5 py-2 shadow-[0_20px_50px_rgba(0,0,0,.32)] backdrop-blur-xl">
            <div className="flex items-center gap-2">
              <div className={`w-2.5 h-2.5 rounded-full ${running ? 'bg-[#5df0a8] shadow-[0_0_10px_#5df0a8]' : 'bg-red-500'}`} />
              <span className="text-xs font-semibold uppercase tracking-wider text-[#dce8e3]">Engine {running ? 'Online' : 'Offline'}</span>
            </div>
            <div className="w-px h-4 bg-white/10" />
            <div className="flex items-center gap-2">
              <div className={`w-2.5 h-2.5 rounded-full ${pilot?.enabled ? 'bg-[#8fe8bd] shadow-[0_0_10px_#5df0a8]' : 'bg-yellow-500'}`} />
              <span className="text-xs font-semibold uppercase tracking-wider text-[#dce8e3]">Autopilot {pilot?.enabled ? 'Engaged' : 'Standby'}</span>
            </div>
          </div>

          {syncError && (
            <div className="flex items-center gap-3 rounded-xl border border-amber-400/20 bg-amber-300/5 px-4 py-3 text-xs text-amber-100/80">
              <AlertCircle className="h-4 w-4 shrink-0 text-amber-300" />
              <span className="flex-1">{syncError}</span>
              <button type="button" onClick={() => void refresh()} className="font-semibold text-amber-200 hover:text-white">
                Retry
              </button>
            </div>
          )}
        </motion.div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
          <div className="lg:col-span-3 space-y-6 flex flex-col">
            <GlassPanel title="Platform Grid" icon={Box} delay={0.1}>
              <div className="space-y-3">
                {platformsData.map(p => {
                  const isConnected = accounts.some(a => a.platform === p.id);
                  return (
                    <div key={p.id} className="flex items-center justify-between p-2.5 rounded-xl bg-black/20 border border-white/10 hover:border-[#5df0a8]/35 transition-colors">
                      <div className="flex items-center gap-3">
                        <div className={`p-1.5 rounded-lg bg-white/5 ${isConnected ? 'text-[#5df0a8]' : 'text-gray-600'}`}>
                          <p.Icon className="w-4 h-4" />
                        </div>
                        <span className={`text-xs font-bold ${isConnected ? 'text-gray-200' : 'text-gray-500'}`}>{p.label}</span>
                      </div>
                      <div className="flex gap-1.5">
                        <button 
                          onClick={() => !isConnected && connectOAuth(p.id)}
                          className={`text-[9px] font-bold px-2 py-1.5 rounded-lg uppercase tracking-wider transition-all ${isConnected ? 'bg-[#5df0a8]/15 text-[#5df0a8] w-full text-center px-4' : 'bg-white/10 text-gray-400 hover:bg-white/20 cursor-pointer'}`}
                        >
                          {isConnected ? 'Linked' : 'OAuth'}
                        </button>
                        {!isConnected && (
                          <button 
                            onClick={() => setShowTokenModal(p.id)}
                            className="text-[9px] font-bold px-2 py-1.5 rounded-lg uppercase tracking-wider transition-all bg-transparent border border-white/10 text-gray-500 hover:border-[#5df0a8]/45 hover:text-gray-200"
                            title="Connect via API Key / Access Token"
                          >
                            API Key
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </GlassPanel>

            <GlassPanel title="Knowledge Base" icon={Database} delay={0.2} className="flex-grow">
              <div className="space-y-5">
                <div className="relative group cursor-pointer h-[120px]">
                  <input type="file" accept=".pdf" className="absolute inset-0 w-full h-full opacity-0 cursor-pointer z-20" onChange={handleFileUpload} />
                  <div className={`absolute inset-0 flex flex-col items-center justify-center p-6 border border-dashed rounded-xl transition-all duration-300 ${isUploading ? 'border-[#5df0a8] bg-[#5df0a8]/10' : 'border-white/20 bg-black/40 group-hover:border-white/40 group-hover:bg-white/5'}`}>
                    <UploadCloud className={`w-8 h-8 mb-3 transition-colors ${isUploading ? 'text-[#5df0a8] animate-bounce' : 'text-gray-500 group-hover:text-gray-300'}`} />
                    <span className="text-xs font-semibold tracking-wide text-gray-400 group-hover:text-gray-300 transition-colors">
                      {isUploading ? 'Ingesting Data...' : 'Drop PDF Context'}
                    </span>
                  </div>
                </div>

                <div className="space-y-2 pt-2 border-t border-white/5">
                  <label className="text-[10px] uppercase tracking-wider text-gray-500 font-bold flex items-center gap-2">
                    Google Sheets Sync
                  </label>
                  <div className="flex bg-black/40 border border-white/10 rounded-xl overflow-hidden focus-within:border-[#5df0a8]/50 transition-colors">
                    <input 
                      type="text" 
                      placeholder="Paste Sheet URL..." 
                      className="w-full bg-transparent text-xs text-white px-3 py-2.5 outline-none placeholder:text-gray-600"
                      value={sheetsUrl}
                      onChange={(e) => setSheetsUrl(e.target.value)}
                    />
                    <button type="button" onClick={() => void (sheetsUrl.trim() ? syncSheets() : connectSheets())} disabled={isSyncingSheets} className="bg-white/5 hover:bg-white/10 disabled:opacity-50 px-3 flex items-center justify-center transition-colors" aria-label={sheetsUrl.trim() ? "Sync Google Sheet" : "Connect Google Sheets"}>
                      <RefreshCw className="w-4 h-4 text-gray-400" />
                    </button>
                  </div>
                </div>
                {knowledgeStatus && <p role="status" className="text-[11px] leading-relaxed text-[#9fc8ed]">{knowledgeStatus}</p>}
              </div>
            </GlassPanel>
          </div>

          <div className="lg:col-span-6 space-y-6 flex flex-col">
            <GlassPanel 
              title="Execution Terminal" 
              icon={Rocket} 
              delay={0.3} 
              className="flex flex-col"
              extraHeader={
                <div className="flex items-center gap-1 bg-black/50 border border-white/10 p-1 rounded-lg">
                  <button 
                    onClick={() => setContentType("text")}
                    className={`px-3 py-1 rounded text-[9px] font-bold uppercase transition-all ${contentType === 'text' ? 'bg-[#5df0a8] text-[#032018] shadow-[0_0_10px_rgba(93,240,168,0.3)]' : 'text-gray-500 hover:text-gray-300'}`}
                  >
                    Text Mode
                  </button>
                  <button 
                    onClick={() => setContentType("video")}
                    className={`px-3 py-1 rounded text-[9px] font-bold uppercase transition-all ${contentType === 'video' ? 'bg-[#5df0a8] text-[#032018] shadow-[0_0_10px_rgba(93,240,168,0.3)]' : 'text-gray-500 hover:text-gray-300'}`}
                  >
                    Video Mode
                  </button>
                </div>
              }
            >
              <div className="flex-grow flex flex-col space-y-4">
                <textarea 
                  className="w-full min-h-[140px] bg-black/40 border border-white/10 rounded-xl p-4 text-sm text-gray-200 placeholder:text-gray-600 outline-none focus:border-[#5df0a8]/50 focus:bg-black/60 transition-all resize-none font-mono"
                  placeholder={`> INSTRUCT AI AGENCY TO GENERATE ${contentType.toUpperCase()}...\n> e.g. 'Analyze PDF and generate 3 ${contentType === 'video' ? 'TikTok videos' : 'LinkedIn posts'} for next week.'`}
                  value={commandText}
                  onChange={(e) => setCommandText(e.target.value)}
                />
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-4">
                    <button 
                      onClick={() => void toggleAutopilot()}
                      className={`flex items-center gap-2 px-5 py-2.5 rounded-xl text-xs font-bold uppercase tracking-wider transition-all duration-300 ${pilot?.enabled ? 'bg-[#5df0a8] text-[#032018] shadow-[0_0_20px_rgba(93,240,168,0.35)]' : 'bg-white/10 hover:bg-white/20 text-gray-400'}`}
                    >
                      {pilot?.enabled ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
                      Autopilot
                    </button>
                    <div className="flex flex-col">
                      <span className="text-[10px] font-bold text-gray-400 uppercase tracking-wider">Status</span>
                      <span className={`text-[11px] ${pilot?.enabled ? 'text-[#5df0a8]' : 'text-gray-500'}`}>
                        {pilot?.enabled ? 'Agents Operating Independently' : 'Awaiting Manual Prompt'}
                      </span>
                    </div>
                  </div>
                  <button 
                    onClick={handleExecute}
                    disabled={isExecuting || !commandText.trim()}
                    className="bg-white text-black hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed px-8 py-2.5 rounded-xl text-xs font-bold uppercase tracking-widest transition-colors flex items-center gap-2 shadow-[0_0_15px_rgba(255,255,255,0.2)]"
                  >
                    {isExecuting ? <RefreshCw className="w-4 h-4 animate-spin" /> : null}
                    {isExecuting ? 'Processing' : 'Engage'}
                  </button>
                </div>
              </div>
            </GlassPanel>

            <GlassPanel title="Agent Network Status" icon={BrainCircuit} delay={0.4}>
              <div className="grid grid-cols-3 gap-4">
                {[
                  { name: 'Strategy', desc: 'Planning', status: pilot?.enabled ? 'Active' : 'Standby', color: pilot?.enabled ? 'text-[#2A9DF8] bg-[#2A9DF8]/10 border-[#2A9DF8]/30' : 'text-gray-500 bg-white/5 border-white/5' },
                  { name: 'Creation', desc: 'AI Synthesis', status: pilot?.drafts?.length ? 'Drafting' : 'Idle', color: pilot?.drafts?.length ? 'text-yellow-400 bg-yellow-400/10 border-yellow-400/30' : 'text-gray-500 bg-white/5 border-white/5' },
                  { name: 'Publisher', desc: 'Deployment', status: jobs.length ? 'Queued' : 'Idle', color: jobs.length ? 'text-green-400 bg-green-400/10 border-green-400/30' : 'text-gray-500 bg-white/5 border-white/5' },
                ].map(agent => (
                  <div key={agent.name} className={`flex flex-col items-center justify-center p-4 border rounded-xl transition-colors duration-500 ${agent.color}`}>
                    <BrainCircuit className="w-6 h-6 mb-3 opacity-80" />
                    <span className="text-xs font-extrabold text-white mb-0.5 tracking-wide">{agent.name}</span>
                    <span className="text-[9px] text-gray-400 mb-2">{agent.desc}</span>
                    <span className="text-[9px] uppercase tracking-widest font-bold opacity-90">{agent.status}</span>
                  </div>
                ))}
              </div>
            </GlassPanel>
            
            <GlassPanel title="Content Factory (Drafts)" icon={FileText} delay={0.5} className="flex-grow">
              {pilot?.drafts?.length > 0 ? (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 max-h-[200px] overflow-y-auto pr-2 custom-scrollbar">
                  {pilot.drafts.map((d: any, i: number) => (
                     <div key={i} className="p-4 bg-black/40 border border-white/10 rounded-xl flex flex-col gap-2 relative group hover:border-[#2A9DF8]/50 transition-colors">
                        <p className="text-[12px] text-gray-300 line-clamp-3 leading-relaxed">{d.text}</p>
                        <div className="flex justify-between items-center mt-2 pt-2 border-t border-white/10">
                           <span className="text-[9px] uppercase tracking-wider text-gray-500 font-bold">{d.platform || 'Cross-platform'}</span>
                           <span className="text-[9px] text-[#2A9DF8] uppercase opacity-0 group-hover:opacity-100 transition-opacity font-bold">Ready</span>
                        </div>
                     </div>
                  ))}
                </div>
              ) : (
                <div className="flex flex-col items-center justify-center py-10 text-gray-600">
                  <FileText className="w-10 h-10 mb-3 opacity-30" />
                  <span className="text-xs font-medium tracking-wide">No drafts generated. Agency awaiting orders.</span>
                </div>
              )}
            </GlassPanel>
          </div>

          <div className="lg:col-span-3 space-y-6 flex flex-col">
            <GlassPanel title="Live Metrics" delay={0.6}>
              <div className="grid grid-cols-2 gap-3">
                <div className="bg-black/30 p-4 rounded-xl border border-white/5">
                  <div className="text-3xl font-black text-white mb-1">{stats.success}</div>
                  <div className="text-[9px] uppercase tracking-widest text-gray-500 font-bold">Posts Live</div>
                </div>
                <div className="bg-black/30 p-4 rounded-xl border border-white/5">
                  <div className="text-3xl font-black text-white mb-1">{stats.queue}</div>
                  <div className="text-[9px] uppercase tracking-widest text-gray-500 font-bold">In Queue</div>
                </div>
                <div className="bg-black/30 p-4 rounded-xl border border-white/5">
                  <div className="text-3xl font-black text-[#2A9DF8] mb-1">{stats.connected}</div>
                  <div className="text-[9px] uppercase tracking-widest text-gray-500 font-bold">Accounts</div>
                </div>
                <div className="bg-black/30 p-4 rounded-xl border border-white/5">
                  <div className="text-3xl font-black text-red-400 mb-1">{stats.failed}</div>
                  <div className="text-[9px] uppercase tracking-widest text-gray-500 font-bold">Failed</div>
                </div>
              </div>
            </GlassPanel>

            <GlassPanel title="Activity Log" delay={0.7} className="flex-grow flex flex-col">
              <div className="flex-grow overflow-y-auto pr-2 custom-scrollbar max-h-[350px] space-y-2.5">
                {jobs.length > 0 ? jobs.slice(0, 15).map((job, i) => (
                  <div key={i} className="flex items-start gap-3 p-3 bg-black/30 border border-white/5 rounded-xl hover:bg-black/50 transition-colors">
                    <div className="mt-0.5">
                      {job.status === 'success' ? <CheckCircle2 className="w-4 h-4 text-green-500" /> :
                       job.status === 'failed' ? <XCircle className="w-4 h-4 text-red-500" /> :
                       job.status === 'pending' || job.status === 'processing' ? <RefreshCw className="w-4 h-4 text-[#2A9DF8] animate-spin" /> :
                       <AlertCircle className="w-4 h-4 text-yellow-500" />}
                    </div>
                    <div className="flex flex-col flex-grow overflow-hidden">
                      <span className="text-xs font-bold text-gray-200 truncate">{job.campaign || job.platform_label || 'Manual Direct Push'}</span>
                      {job.content_preview && (
                        <span className="text-[10px] text-gray-400 mt-1 line-clamp-2 leading-snug">{job.content_preview}</span>
                      )}
                      <span className="text-[9px] text-gray-500 uppercase tracking-widest mt-1 font-bold">{job.status} • {job.platform || 'General'}</span>
                    </div>
                  </div>
                )) : (
                  <div className="flex flex-col items-center justify-center h-full py-10 text-xs text-gray-600 font-medium">
                    Pipeline is completely clear.
                  </div>
                )}
              </div>
            </GlassPanel>
          </div>

        </div>
      </div>
      
      {/* Manual API Token Modal */}
      <AnimatePresence>
        {showTokenModal && (
          <motion.div 
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.2 }}
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md p-4"
          >
            <motion.div 
              initial={{ scale: 0.9, y: 20 }} animate={{ scale: 1, y: 0 }} exit={{ scale: 0.95, y: 10 }} transition={{ duration: 0.3 }}
              className="bg-[#0A0A0C] border border-white/10 rounded-2xl p-6 w-full max-w-md shadow-2xl relative"
            >
              <button onClick={() => setShowTokenModal(null)} className="absolute top-4 right-4 text-gray-500 hover:text-white transition-colors">
                <XCircle className="w-5 h-5" />
              </button>
              <h2 className="text-xl font-bold text-white mb-2 flex items-center gap-2">
                <Database className="w-5 h-5 text-[#2A9DF8]" />
                Direct API Connection
              </h2>
              <p className="text-xs text-gray-400 mb-4">Bypass standard OAuth and securely connect your account using direct methods.</p>
              
              <div className="flex gap-2 mb-6 border-b border-white/10 pb-2 overflow-x-auto custom-scrollbar">
                <button 
                  onClick={() => setLoginTab("token")}
                  className={`text-[10px] whitespace-nowrap font-bold uppercase tracking-wider px-3 py-1.5 rounded transition-all ${loginTab === 'token' ? 'bg-white/10 text-white' : 'text-gray-500 hover:text-gray-300'}`}
                >
                  Access Token
                </button>
                <button 
                  onClick={() => setLoginTab("password")}
                  className={`text-[10px] whitespace-nowrap font-bold uppercase tracking-wider px-3 py-1.5 rounded transition-all ${loginTab === 'password' ? 'bg-white/10 text-white' : 'text-gray-500 hover:text-gray-300'}`}
                >
                  ID & Password
                </button>
                <button 
                  onClick={() => setLoginTab("app_keys")}
                  className={`text-[10px] whitespace-nowrap font-bold uppercase tracking-wider px-3 py-1.5 rounded transition-all ${loginTab === 'app_keys' ? 'bg-white/10 text-white' : 'text-gray-500 hover:text-gray-300'}`}
                >
                  Developer App Keys
                </button>
              </div>

              <div className="space-y-4">
                {loginTab === "token" && (
                  <div>
                    <label className="text-[10px] font-bold uppercase tracking-wider text-gray-500 mb-2 block">Access Token / API Key</label>
                    <input 
                      type="password"
                      value={manualToken}
                      onChange={(e) => setManualToken(e.target.value)}
                      className="w-full bg-black/50 border border-white/10 rounded-xl px-4 py-3 text-sm text-white outline-none focus:border-[#2A9DF8]/50 transition-colors"
                      placeholder="Enter raw access token..."
                    />
                  </div>
                )}
                {loginTab === "password" && (
                  <>
                    <div>
                      <label className="text-[10px] font-bold uppercase tracking-wider text-gray-500 mb-2 block">Username / ID</label>
                      <input 
                        type="text"
                        value={manualUsername}
                        onChange={(e) => setManualUsername(e.target.value)}
                        className="w-full bg-black/50 border border-white/10 rounded-xl px-4 py-3 text-sm text-white outline-none focus:border-[#2A9DF8]/50 transition-colors"
                        placeholder="Enter username..."
                      />
                    </div>
                    <div>
                      <label className="text-[10px] font-bold uppercase tracking-wider text-gray-500 mb-2 block">Password</label>
                      <input 
                        type="password"
                        value={manualPassword}
                        onChange={(e) => setManualPassword(e.target.value)}
                        className="w-full bg-black/50 border border-white/10 rounded-xl px-4 py-3 text-sm text-white outline-none focus:border-[#2A9DF8]/50 transition-colors"
                        placeholder="Enter password..."
                      />
                    </div>
                  </>
                )}
                {loginTab === "app_keys" && (
                  <>
                    <div className="bg-blue-500/10 border border-blue-500/20 rounded-lg p-3 mb-4">
                      <p className="text-[11px] text-blue-400 font-medium leading-relaxed">
                        These keys are securely stored and encrypted in the database strictly for your workspace. Once saved, click the <strong className="text-white">OAUTH</strong> button to launch your authentic login popup!
                      </p>
                      <p className="text-[11px] text-blue-400 font-medium leading-relaxed mt-2">
                        Before that, add this <strong className="text-white">Authorized redirect URI</strong> in your developer app (Google Cloud → Credentials → your OAuth Client, or the equivalent Meta/X console page) — the login fails with a redirect/403 error without it:
                      </p>
                      <input
                        readOnly
                        value={redirectUri || "(start the backend to load it)"}
                        onFocus={(e) => e.currentTarget.select()}
                        className="mt-2 w-full bg-black/60 border border-blue-500/30 rounded-lg px-3 py-2 text-[11px] text-white font-mono outline-none cursor-copy"
                      />
                    </div>
                    <div>
                      <label className="text-[10px] font-bold uppercase tracking-wider text-gray-500 mb-2 block">App Client ID</label>
                      <input 
                        type="text"
                        value={appClientId}
                        onChange={(e) => setAppClientId(e.target.value)}
                        className="w-full bg-black/50 border border-white/10 rounded-xl px-4 py-3 text-sm text-white outline-none focus:border-[#2A9DF8]/50 transition-colors"
                        placeholder="e.g. 8493820492..."
                      />
                    </div>
                    <div>
                      <label className="text-[10px] font-bold uppercase tracking-wider text-gray-500 mb-2 block">App Client Secret</label>
                      <input 
                        type="password"
                        value={appClientSecret}
                        onChange={(e) => setAppClientSecret(e.target.value)}
                        className="w-full bg-black/50 border border-white/10 rounded-xl px-4 py-3 text-sm text-white outline-none focus:border-[#2A9DF8]/50 transition-colors"
                        placeholder="Enter App Secret..."
                      />
                    </div>
                  </>
                )}
                
                {loginTab === "app_keys" ? (
                  <button 
                    onClick={submitAppKeys}
                    disabled={!appClientId.trim() || !appClientSecret.trim()}
                    className="w-full py-3 bg-[#2A9DF8] hover:bg-[#1C8BE2] text-white text-sm font-bold rounded-xl transition-colors disabled:opacity-50 disabled:cursor-not-allowed mt-2"
                  >
                    Save Keys to Workspace
                  </button>
                ) : (
                  <button 
                    onClick={submitManualToken}
                    disabled={loginTab === "token" ? !manualToken.trim() : (!manualUsername.trim() || !manualPassword.trim())}
                    className="w-full py-3 bg-[#2A9DF8] hover:bg-[#1C8BE2] text-white text-sm font-bold rounded-xl transition-colors disabled:opacity-50 disabled:cursor-not-allowed mt-2"
                  >
                    Connect Instantly
                  </button>
                )}
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Global Styles for Custom Scrollbar */}
      <style dangerouslySetInnerHTML={{__html: `
        .custom-scrollbar::-webkit-scrollbar { width: 4px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: rgba(0,0,0,0.1); border-radius: 4px; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 4px; }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.3); }
      `}} />
    </div>
  );
}
