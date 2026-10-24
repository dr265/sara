"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Play, Pause, Download, Music, Mic, Sparkles, AlertCircle, CheckCircle2,
  Loader2, Layers, RefreshCw, Trash2, AudioWaveform,
} from "lucide-react";
import { API_MUSIC_URL } from "@/lib/api";

// ---------------------------------------------------------------- types
interface Track {
  id: string;
  url: string;
  prompt: string;
  engine: string;
  metadata: {
    genre?: string;
    mood?: string;
    tempo_bpm?: number | null;
    duration_seconds?: number | null;
    instruments?: string[] | null;
    vocals?: string | null;
    variation?: number;
    file_bytes?: number;
  };
  createdAt: string;
}

interface Health {
  status: string;
  llm: { configured: boolean; model: string | null };
  engines: { modelslab: boolean; procedural: boolean };
  library_items: number;
}

interface GenerateResponse {
  tracks: Track[];
  brief: Record<string, unknown>;
  enhanced_prompt: string;
  engine: string;
  warning: string | null;
}

const API_BASE = `${API_MUSIC_URL}/api/music`;

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* keep default */ }
    throw new Error(detail);
  }
  return (await res.json()) as T;
};

// ---------------------------------------------------------------- styles
const STYLES = `
.ms-root { color: #e7ecef; }
.ms-shell { border: 1px solid #ffffff18; border-radius: 18px; overflow: hidden;
  background: rgba(12, 15, 22, .42); backdrop-filter: blur(20px); box-shadow: 0 30px 80px #00000035; }
.ms-stage { position: relative; height: 340px; overflow: hidden; background: radial-gradient(90% 120% at 50% 0%, #26334a 0%, #0b0d13 70%); }
.ms-stage::before { content: ""; position: absolute; width: 48%; aspect-ratio: 1; left: 26%; top: -35%; border: 1px solid #d6e1f222; border-radius: 50%; box-shadow: 0 0 90px #7b8ca822, inset 0 0 50px #cdd6e211; animation: ms-drift 12s ease-in-out infinite; pointer-events: none; }
.ms-stage::after { content: ""; position: absolute; inset: 18% 12%; border: 1px solid #ffffff0d; border-radius: 50%; transform: rotate(-14deg) perspective(320px) rotateX(62deg); pointer-events: none; }
@keyframes ms-drift { 0%, 100% { transform: translate3d(0, 0, 0) rotate(0deg); } 50% { transform: translate3d(0, 18px, 0) rotate(8deg); } }
.ms-stage canvas { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }
.ms-stage-title { position: absolute; inset: 0; display: grid; place-items: center; pointer-events: none;
  font-size: clamp(26px, 4vw, 44px); font-weight: 800; letter-spacing: .32em; color: transparent;
  -webkit-text-stroke: 1px #8b93a755; }
.ms-player { position: absolute; left: 18px; right: 18px; bottom: 18px; display: flex; align-items: center; gap: 18px;
  padding: 14px 18px; border-radius: 14px; border: 1px solid #ffffff14; background: #10131bcc;
  backdrop-filter: blur(14px); }
.ms-play { width: 46px; height: 46px; border-radius: 999px; border: 0; background: #e7ecef; color: #0b0d13;
  display: grid; place-items: center; cursor: pointer; flex: none; }
.ms-play:hover { transform: scale(1.04); }
.ms-track-title { font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 8px; }
.ms-track-meta { font-size: 12px; color: #9aa4b2; display: flex; gap: 8px; flex-wrap: wrap; margin-top: 3px; }
.ms-tag { background: #ffffff10; border: 1px solid #ffffff10; border-radius: 6px; padding: 1px 7px; }
.ms-progress { height: 4px; border-radius: 4px; background: #ffffff14; overflow: hidden; cursor: pointer; margin-top: 8px; }
.ms-progress > div { height: 100%; background: #cdd6e2; }
.ms-times { display: flex; justify-content: space-between; font-size: 11px; color: #8b93a7; margin-top: 4px; }
.ms-panel { padding: 24px; display: grid; gap: 18px; }
.ms-textarea { width: 100%; min-height: 110px; resize: vertical; background: #0d1017; color: #e7ecef;
  border: 1px solid #ffffff16; border-radius: 14px; padding: 16px 18px; outline: none; font: inherit; line-height: 1.55; }
.ms-textarea:focus { border-color: #8fa3c8; box-shadow: 0 0 0 3px #8fa3c822; }
.ms-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 14px; }
.ms-label { font-size: 11px; text-transform: uppercase; letter-spacing: .1em; color: #8b93a7; margin-bottom: 7px; display: block; }
.ms-select, .ms-range { width: 100%; background: #0d1017; color: #dfe5ec; border: 1px solid #ffffff16;
  border-radius: 10px; padding: 10px 12px; outline: none; font: inherit; }
.ms-range { padding: 0; accent-color: #9db4d8; }
.ms-toggle { width: 100%; display: flex; align-items: center; justify-content: center; gap: 8px; cursor: pointer;
  background: #0d1017; border: 1px solid #ffffff16; color: #aab4c2; border-radius: 10px; padding: 10px 12px; font: inherit; }
.ms-toggle[aria-checked="true"] { background: #e7ecef; color: #0b0d13; border-color: #e7ecef; }
.ms-generate { display: flex; align-items: center; justify-content: center; gap: 10px; width: 100%; height: 52px;
  border: 0; border-radius: 14px; background: #e7ecef; color: #0b0d13; font-weight: 700; font-size: 15px; cursor: pointer; }
.ms-generate:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 10px 30px #cdd6e233; }
.ms-generate:disabled { opacity: .45; cursor: not-allowed; }
.ms-generate.busy { background: #1a2130; color: #dfe5ec; border: 1px solid #ffffff20;
  animation: ms-pulse 2s infinite; }
@keyframes ms-pulse { 0%,100% { border-color: #ffffff20; } 50% { border-color: #cdd6e2; box-shadow: 0 0 16px #cdd6e233; } }
.ms-banner { display: flex; align-items: center; gap: 9px; padding: 11px 14px; border-radius: 10px; font-size: 13.5px; }
.ms-banner--err { color: #f0a99b; background: #3a1d1622; border: 1px solid #b9645645; }
.ms-banner--ok { color: #a9d8b4; background: #16281c22; border: 1px solid #3f6a4a55; }
.ms-banner--warn { color: #e8d9a8; background: #2b251322; border: 1px solid #8a7c4555; }
.ms-spin { animation: ms-spin 1s linear infinite; }
@keyframes ms-spin { to { transform: rotate(360deg); } }
.ms-chip-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ms-chip { font-size: 12px; color: #9aa4b2; background: #ffffff08; border: 1px solid #ffffff12;
  border-radius: 999px; padding: 4px 11px; }
.ms-dot { width: 8px; height: 8px; border-radius: 999px; display: inline-block; }
.ms-lib { display: grid; gap: 10px; max-height: 430px; overflow: auto; }
.ms-track-card { display: flex; align-items: center; gap: 13px; padding: 13px 15px; border-radius: 12px;
  border: 1px solid #ffffff12; background: #ffffff05; cursor: pointer; }
.ms-track-card:hover { border-color: #ffffff2b; background: #ffffff09; }
.ms-track-card.active { border-color: #cdd6e2; box-shadow: 0 0 14px #cdd6e222; }
.ms-cover { width: 42px; height: 42px; border-radius: 10px; flex: none; display: grid; place-items: center;
  background: linear-gradient(145deg, #223054, #10141d); color: #9db4d8; }
.ms-brief { font-size: 12.5px; color: #9aa4b2; margin-top: 2px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.ms-iconbtn { background: transparent; border: 1px solid #ffffff14; color: #9aa4b2; width: 34px; height: 34px;
  border-radius: 999px; display: grid; place-items: center; cursor: pointer; flex: none; }
.ms-iconbtn:hover { color: #e7ecef; border-color: #ffffff2b; }
.ms-enhanced { border: 1px dashed #ffffff1c; border-radius: 12px; padding: 13px 15px; font-size: 13px;
  color: #b9c2cf; line-height: 1.6; }
.ms-enhanced b { color: #e7ecef; }
.ms-kicker { color: #9db4d8; font-size: 10px; font-weight: 700; letter-spacing: .28em; text-transform: uppercase; }
.ms-heading { margin: 6px 0 7px; color: #f1f4f8; font-size: clamp(28px, 4vw, 44px); letter-spacing: -.055em; line-height: 1; font-weight: 700; }
.ms-intro { max-width: 610px; color: #9aa4b2; font-size: 14px; line-height: 1.6; }
@media (max-width: 860px) { .ms-stage { height: 260px; } }
`;

const GENRES = ["Cinematic", "Electronic", "Ambient", "Hip Hop", "Rock", "Jazz", "Classical", "Pop", "Synthwave", "Lo-fi"];
const MOODS = ["Epic", "Dark", "Uplifting", "Melancholic", "Aggressive", "Chill", "Mysterious", "Dreamy"];

const fmtTime = (s: number) => {
  if (!s || !isFinite(s)) return "0:00";
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
};

// ---------------------------------------------------------------- visualizer
function Visualizer({ analyser, active }: { analyser: AnalyserNode | null; active: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const bins = new Uint8Array(analyser ? analyser.frequencyBinCount : 64);
    let phase = 0;

    const draw = () => {
      const w = (canvas.width = canvas.clientWidth * devicePixelRatio);
      const h = (canvas.height = canvas.clientHeight * devicePixelRatio);
      ctx.clearRect(0, 0, w, h);

      if (analyser) analyser.getByteFrequencyData(bins);
      phase += active ? 0.05 : 0.012;

      const bars = 64;
      const gap = 3 * devicePixelRatio;
      const bw = (w - gap * (bars - 1)) / bars;
      for (let i = 0; i < bars; i++) {
        let v: number;
        if (analyser && active) {
          v = bins[Math.floor((i / bars) * bins.length * 0.72)] / 255;
        } else {
          v = 0.06 + 0.05 * (Math.sin(phase + i * 0.32) + 1) / 2;
        }
        const bh = Math.max(2 * devicePixelRatio, v * h * 0.82);
        const x = i * (bw + gap);
        const grad = ctx.createLinearGradient(0, h, 0, h - bh);
        grad.addColorStop(0, "#3b4a6b");
        grad.addColorStop(1, "#cdd6e2");
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.roundRect(x, h - bh, bw, bh, 3 * devicePixelRatio);
        ctx.fill();
      }
      rafRef.current = requestAnimationFrame(draw);
    };
    rafRef.current = requestAnimationFrame(draw);
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
  }, [analyser, active]);

  return <canvas ref={canvasRef} aria-hidden />;
}

// ---------------------------------------------------------------- page
export default function MusicStudioPage() {
  const [prompt, setPrompt] = useState("");
  const [genre, setGenre] = useState("Cinematic");
  const [mood, setMood] = useState("Epic");
  const [duration, setDuration] = useState(30);
  const [vocal, setVocal] = useState(false);

  const [health, setHealth] = useState<Health | null>(null);
  const [library, setLibrary] = useState<Track[]>([]);
  const [current, setCurrent] = useState<Track | null>(null);
  const [enhanced, setEnhanced] = useState("");
  const [warning, setWarning] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [isPlaying, setIsPlaying] = useState(false);
  const [progress, setProgress] = useState(0);
  const [audioDur, setAudioDur] = useState(0);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const [analyser, setAnalyser] = useState<AnalyserNode | null>(null);

  const loadHealth = useCallback(async () => {
    try { setHealth(await api<Health>("/health")); }
    catch { setHealth(null); }
  }, []);

  const loadLibrary = useCallback(async () => {
    try { setLibrary(await api<Track[]>("/history")); }
    catch { /* chip surfaces connectivity */ }
  }, []);

  useEffect(() => {
    void loadHealth();
    void loadLibrary();
    audioRef.current = new Audio();
    const el = audioRef.current;
    // REQUIRED for the WebAudio analyser: without CORS approval the browser
    // taints the cross-origin stream and outputs SILENCE (timer still runs).
    // The backend's CORS middleware allows all loopback origins.
    el.crossOrigin = "anonymous";
    el.preload = "auto";
    const onTime = () => setProgress(el.duration ? (el.currentTime / el.duration) * 100 : 0);
    const onMeta = () => setAudioDur(el.duration || 0);
    const onEnd = () => setIsPlaying(false);
    el.addEventListener("timeupdate", onTime);
    el.addEventListener("loadedmetadata", onMeta);
    el.addEventListener("ended", onEnd);
    return () => {
      el.pause();
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("loadedmetadata", onMeta);
      el.removeEventListener("ended", onEnd);
    };
  }, [loadHealth, loadLibrary]);

  // Self-healing status chip while the backend is unreachable.
  useEffect(() => {
    if (health != null) return;
    const id = window.setInterval(() => { void loadHealth(); }, 8000);
    return () => window.clearInterval(id);
  }, [health, loadHealth]);

  const ensureAnalyser = useCallback((): AnalyserNode | null => {
    if (!audioRef.current) return null;
    if (audioCtxRef.current) return analyserRef.current;
    try {
      const ctx = new AudioContext();
      const src = ctx.createMediaElementSource(audioRef.current);
      const node = ctx.createAnalyser();
      node.fftSize = 256;
      src.connect(node);
      node.connect(ctx.destination);
      audioCtxRef.current = ctx;
      analyserRef.current = node;
      setAnalyser(node);
      return node;
    } catch {
      return null; // visualizer falls back to idle animation
    }
  }, []);

  const play = useCallback((track: Track) => {
    const el = audioRef.current;
    if (!el) return;
    ensureAnalyser();
    audioCtxRef.current?.resume().catch(() => undefined);
    if (current?.id !== track.id) {
      el.crossOrigin = "anonymous"; // must precede src assignment
      el.src = `${API_MUSIC_URL}${track.url}`;
      setCurrent(track);
      setProgress(0);
    }
    el.play().then(() => setIsPlaying(true)).catch(() => setIsPlaying(false));
  }, [current, ensureAnalyser]);

  const toggle = useCallback(() => {
    const el = audioRef.current;
    if (!el || !current) return;
    if (isPlaying) { el.pause(); setIsPlaying(false); }
    else { audioCtxRef.current?.resume().catch(() => undefined); el.play().then(() => setIsPlaying(true)).catch(() => undefined); }
  }, [current, isPlaying]);

  const seek = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const el = audioRef.current;
    if (!el || !el.duration) return;
    const rect = e.currentTarget.getBoundingClientRect();
    el.currentTime = ((e.clientX - rect.left) / rect.width) * el.duration;
  }, []);

  const generate = useCallback(async () => {
    if (!prompt.trim() || busy) return;
    setBusy(true);
    setError("");
    setWarning("");
    try {
      const res = await api<GenerateResponse>("/generate", {
        method: "POST",
        body: JSON.stringify({ prompt: prompt.trim(), genre, mood, duration, vocal }),
      });
      setEnhanced(res.enhanced_prompt || "");
      if (res.warning) setWarning(res.warning);
      await loadLibrary();
      if (res.tracks[0]) play(res.tracks[0]);
      void loadHealth();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Generation failed");
    } finally {
      setBusy(false);
    }
  }, [prompt, busy, genre, mood, duration, vocal, loadLibrary, loadHealth, play]);

  const remove = useCallback(async (id: string) => {
    try {
      await api(`/history/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (current?.id === id) {
        audioRef.current?.pause();
        setIsPlaying(false);
        setCurrent(null);
      }
      await loadLibrary();
      void loadHealth();
    } catch { /* card remains */ }
  }, [current, loadLibrary, loadHealth]);

  const online = health != null;

  return (
    <div className="ms-root">
      <style>{STYLES}</style>

      <div className="mb-6">
        <div className="ms-kicker">SARA / MUSIC LAB</div>
        <h1 className="ms-heading">Turn a feeling into a track.</h1>
        <p className="ms-intro">Compose a direction, shape the texture, and audition real generated audio in one quiet studio surface.</p>
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <span className="ms-chip">
          <span className="ms-dot" style={{ background: online ? "#4ade80" : "#f87171", boxShadow: online ? "0 0 10px #4ade8088" : "0 0 10px #f8717188", marginRight: 7 }} />
          {online ? `Backend online · ${health?.llm.model ?? "LLM ready"}` : "Backend offline · retrying…"}
        </span>
        <button className="ms-chip ms-iconbtn" style={{ width: 28, height: 28 }} onClick={() => void loadHealth()} aria-label="Refresh status">
          <RefreshCw size={13} />
        </button>
        <span className="ms-chip">🎹 Engine: {health?.engines.modelslab ? "ModelsLab + built-in synth" : "Built-in synthesizer"}</span>
        <span className="ms-chip">🎧 {health?.library_items ?? library.length} tracks in library</span>
      </div>

      <div className="ms-shell">
        {/* stage + player */}
        <div className="ms-stage">
          <Visualizer analyser={analyser} active={isPlaying || busy} />
          {!current && (
            <div className="ms-stage-title">SONIC SCULPTURE</div>
          )}
          {current && (
            <div className="ms-player">
              <button className="ms-play" onClick={toggle} aria-label={isPlaying ? "Pause" : "Play"}>
                {isPlaying ? <Pause size={20} fill="currentColor" /> : <Play size={20} fill="currentColor" style={{ marginLeft: 3 }} />}
              </button>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="ms-track-title">
                  <AudioWaveform size={15} />
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{current.prompt}</span>
                </div>
                <div className="ms-track-meta">
                  <span className="ms-tag">{current.metadata.genre}</span>
                  <span className="ms-tag">{current.metadata.mood}</span>
                  {current.metadata.tempo_bpm ? <span className="ms-tag">{current.metadata.tempo_bpm} BPM</span> : null}
                  <span className="ms-tag">{current.engine}</span>
                </div>
                <div className="ms-progress" onClick={seek}>
                  <div style={{ width: `${progress}%` }} />
                </div>
                <div className="ms-times">
                  <span>{fmtTime((progress / 100) * audioDur)}</span>
                  <span>{fmtTime(audioDur)}</span>
                </div>
              </div>
              <a className="ms-iconbtn" title="Download audio" href={`${API_MUSIC_URL}${current.url}`} download>
                <Download size={16} />
              </a>
            </div>
          )}
        </div>

        {/* creator panel */}
        <div className="ms-panel" style={{ borderTop: "1px solid #ffffff12" }}>
          <textarea
            className="ms-textarea"
            placeholder="Describe the music you want… e.g. “A 60-second dark synthwave chase theme with driving arpeggios and heavy drums”"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            disabled={busy}
          />

          <div className="ms-grid">
            <div>
              <label className="ms-label">Genre</label>
              <select className="ms-select" value={genre} onChange={(e) => setGenre(e.target.value)} disabled={busy}>
                {GENRES.map((g) => <option key={g}>{g}</option>)}
              </select>
            </div>
            <div>
              <label className="ms-label">Mood</label>
              <select className="ms-select" value={mood} onChange={(e) => setMood(e.target.value)} disabled={busy}>
                {MOODS.map((m) => <option key={m}>{m}</option>)}
              </select>
            </div>
            <div>
              <label className="ms-label">Duration · {duration}s</label>
              <input className="ms-range" type="range" min={15} max={180} step={5}
                value={duration} onChange={(e) => setDuration(parseInt(e.target.value, 10))} disabled={busy} />
            </div>
            <div>
              <label className="ms-label">Vocals</label>
              <button type="button" className="ms-toggle" aria-checked={vocal} onClick={() => setVocal(!vocal)} disabled={busy}>
                <Mic size={15} /> {vocal ? "Vocal included" : "Instrumental"}
              </button>
            </div>
          </div>

          <button className={`ms-generate ${busy ? "busy" : ""}`} onClick={() => void generate()} disabled={!prompt.trim() || busy || !online}>
            {busy ? (<><Loader2 size={19} className="ms-spin" /> Enhancing prompt & synthesizing…</>) : (<><Sparkles size={19} /> Generate Track</>)}
          </button>

          {error && <div className="ms-banner ms-banner--err"><AlertCircle size={16} /> {error}</div>}
          {warning && <div className="ms-banner ms-banner--warn"><AlertCircle size={16} /> {warning}</div>}
          {enhanced && !busy && (
            <div className="ms-enhanced">
              <b>Enhanced prompt</b> — {enhanced}
            </div>
          )}
          {!online && (
            <div className="ms-banner ms-banner--warn">
              <AlertCircle size={16} /> Backend offline — it retries automatically every few seconds. Start everything with start_all.bat.
            </div>
          )}
        </div>
      </div>

      {/* library */}
      <div className="mt-8 flex items-center gap-3">
        <Layers size={17} className="text-gray-400" />
        <h2 className="text-lg font-semibold text-gray-100">Library</h2>
        <button className="ms-iconbtn" onClick={() => void loadLibrary()} aria-label="Refresh library"><RefreshCw size={14} /></button>
      </div>
      <div className="ms-lib mt-4">
        {library.length === 0 && (
          <div className="rounded-xl border border-dashed border-gray-800 p-10 text-center text-sm text-gray-500">
            No generations yet. Describe a track above and press <b>Generate Track</b> — real audio lands here.
          </div>
        )}
        {library.map((t) => (
          <div key={t.id} className={`ms-track-card ${current?.id === t.id ? "active" : ""}`} onClick={() => play(t)}>
            <div className="ms-cover"><Music size={18} /></div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="text-sm font-medium text-gray-100" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.prompt}</div>
              <div className="ms-brief">
                {t.metadata.genre} · {t.metadata.mood}
                {t.metadata.tempo_bpm ? ` · ${t.metadata.tempo_bpm} BPM` : ""}
                {t.metadata.duration_seconds ? ` · ${t.metadata.duration_seconds}s` : ""}
                {t.metadata.instruments?.length ? ` · ${t.metadata.instruments.slice(0, 3).join(", ")}` : ""}
              </div>
            </div>
            <span className="ms-chip">{new Date(t.createdAt).toLocaleTimeString()}</span>
            <button
              className="ms-iconbtn"
              aria-label="Delete track"
              onClick={(e) => { e.stopPropagation(); void remove(t.id); }}
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
