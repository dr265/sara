'use client';

/*
 * Trading Signal Agent — /hub/trading-agent
 *
 * Live screen-observing workspace: OCR reads the current-price readout straight
 * off a chart, 1-minute candles are built in-browser, the same ensemble engine
 * (momentum / volatility / price-action / trend with adaptive weights) runs
 * live and raises tappable CALL/PUT alerts. Execution stays manual — the
 * optional "paper auto-trade" mode only records what following every alert
 * would have returned.
 *
 * Everything runs client-side; the identical engine also lives server-side on
 * the unified backend under /trading/* (backtest + /trading/live/*) — one
 * server, no standalone state daemon. On mount this page pings the backend
 * engine health endpoint (GET /trading/health) so the Nexus Hub shows the
 * server-side engine state next to the live client engine.
 */

import React, { useEffect, useRef, useState } from "react";
import type { Worker } from "tesseract.js";
import { TRADING_CSS } from "./trading.css";
import { SARA_API_URL, saraFetch } from "@/lib/api";

/* ───────────────────────────── types & config ───────────────────────────── */

type Dir = "CALL" | "PUT" | "NEUTRAL";
type Candle = { ts: number; open: number; high: number; low: number; close: number; volume: number };
const SUBSYSTEMS = ["momentum", "volatility", "price_action", "trend"] as const;
type Sub = (typeof SUBSYSTEMS)[number];
type Scores = Record<Sub, number>;
type Horizon = { step: number; direction: Dir; target: number; confidence: number };
type Trade = { ts: number; direction: 1 | -1; entry: number; stake: number; result: number | null };
type Region = { x: number; y: number; w: number; h: number };
type Phase = "idle" | "picking" | "ready" | "active" | "paused";
type TestRead = {
  at: number;
  url: string;             // processed crop PNG — exactly what tesseract sees
  raw: string;             // raw OCR text
  parsed: number | null;   // extracted price, if any
  conf: number;            // tesseract confidence 0-100
  dark: boolean;           // true if dark-mode inversion was applied
  bright: number;          // mean brightness 0-255 of the crop
  medianOk: boolean | null; // live-loop sanity check result (null = no prior readings)
  w: number;
  h: number;
  region: Region;
  error?: string;
};

type Config = {
  bufferSize: number; warmup: number; horizons: number; scoreScale: number; horizonDecay: number;
  neutralBand: number; eta: number; reward: number; weightFloor: number; lossMemory: number;
  defenseTrigger: number; baseThreshold: number; defenseThreshold: number; accuracyWindow: number;
  cooldownSec: number; autoTrade: boolean; stake: number; payout: number;
};

const DEFAULT_CFG: Config = {
  bufferSize: 200, warmup: 60, horizons: 5, scoreScale: 4, horizonDecay: 0.85, neutralBand: 0.08,
  eta: 0.15, reward: 0.03, weightFloor: 0.05, lossMemory: 5, defenseTrigger: 3,
  baseThreshold: 0.85, defenseThreshold: 0.9, accuracyWindow: 20, cooldownSec: 60,
  autoTrade: false, stake: 10, payout: 0.82,
};

/* ───────────────────────────── math helpers ───────────────────────────── */

const clip = (x: number) => (Number.isFinite(x) ? Math.max(-1, Math.min(1, x)) : 0);
const last = (a: number[], d = 0) => { const v = a[a.length - 1]; return Number.isFinite(v) ? v : d; };
const at = (a: number[], i: number, d = 0) => { const v = a[a.length + i]; return Number.isFinite(v) ? v : d; };
const tail = (a: number[], n: number) => a.slice(Math.max(0, a.length - n)).filter(Number.isFinite);
const mean = (a: number[]) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : NaN);
const std = (a: number[]) => { const m = mean(a); return a.length > 1 ? Math.sqrt(a.reduce((s, v) => s + (v - m) ** 2, 0) / (a.length - 1)) : NaN; };
const quantile = (a: number[], q: number) => { if (!a.length) return NaN; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(q * s.length))]; };

const sma = (x: number[], n: number) => x.map((_, i) => (i >= n - 1 ? mean(x.slice(i - n + 1, i + 1)) : NaN));
const ema = (x: number[], n: number) => { const k = 2 / (n + 1); let p = NaN; return x.map((v) => (p = Number.isNaN(p) ? v : v * k + p * (1 - k))); };
const rma = (x: number[], n: number) => { let p = NaN; return x.map((v, i) => { if (Number.isNaN(v)) return NaN; p = Number.isNaN(p) ? v : (p * (n - 1) + v) / n; return i < n - 1 ? NaN : p; }); };

function rsi(c: number[], n = 14) {
  const out = new Array(c.length).fill(NaN); let ag = 0, al = 0;
  for (let i = 1; i < c.length; i++) {
    const d = c[i] - c[i - 1], g = Math.max(d, 0), l = Math.max(-d, 0);
    if (i <= n) { ag += g / n; al += l / n; }
    else { ag = (ag * (n - 1) + g) / n; al = (al * (n - 1) + l) / n; }
    if (i >= n) out[i] = 100 - 100 / (1 + ag / (al || 1e-12));
  }
  return out;
}
function macdHist(c: number[]) { const e12 = ema(c, 12), e26 = ema(c, 26); const m = c.map((_, i) => (e12[i] ?? 0) - (e26[i] ?? 0)); const s = ema(m, 9); return m.map((v, i) => v - (s[i] ?? 0)); }
function stoch(h: number[], l: number[], c: number[], n = 14, k = 3, d = 3) {
  const raw = c.map((v, i) => { if (i < n - 1) return NaN; const hh = Math.max(...h.slice(i - n + 1, i + 1)), ll = Math.min(...l.slice(i - n + 1, i + 1)); return ((v - ll) / (hh - ll || 1e-12)) * 100; });
  const K = sma(raw, k); return { K, D: sma(K, d) };
}
function trueRange(h: number[], l: number[], c: number[]) { return h.map((v, i) => (i === 0 ? v - l[i] : Math.max(v - l[i], Math.abs(v - c[i - 1]), Math.abs(l[i] - c[i - 1])))); }
const atr = (h: number[], l: number[], c: number[], n = 14) => rma(trueRange(h, l, c), n);
function bbands(c: number[], n = 20, k = 2) {
  const mid = sma(c, n); const sd = c.map((_, i) => (i >= n - 1 ? std(c.slice(i - n + 1, i + 1)) : NaN));
  const up = mid.map((m, i) => (m ?? 0) + k * (sd[i] ?? 0)), lo = mid.map((m, i) => (m ?? 0) - k * (sd[i] ?? 0));
  return { pctB: c.map((v, i) => (v - (lo[i] ?? v)) / ((up[i] ?? v) - (lo[i] ?? v) || 1e-12)), width: up.map((u, i) => ((u ?? 0) - (lo[i] ?? 0)) / ((mid[i] ?? 0) || 1e-12)) };
}
function vwap(h: number[], l: number[], c: number[], v: number[]) {
  let pv = 0, vv = 0; return c.map((_, i) => { const tp = (h[i] + l[i] + c[i]) / 3, vol = v[i] > 0 ? v[i] : 1; pv += tp * vol; vv += vol; return pv / vv; });
}
function supertrendDir(h: number[], l: number[], c: number[], n = 10, m = 3) {
  const a = atr(h, l, c, n); const out = new Array(c.length).fill(NaN); let fu = NaN, fl = NaN, d = 1;
  for (let i = 0; i < c.length; i++) {
    if (Number.isNaN(a[i])) continue;
    const hl2 = (h[i] + l[i]) / 2, bu = hl2 + m * (a[i] ?? 0), bl = hl2 - m * (a[i] ?? 0);
    if (Number.isNaN(fu)) { fu = bu; fl = bl; d = c[i] > hl2 ? 1 : -1; }
    else { fl = bl > fl || c[i - 1] < fl ? bl : fl; fu = bu < fu || c[i - 1] > fu ? bu : fu; d = d === 1 ? (c[i] < fl ? -1 : 1) : c[i] > fu ? 1 : -1; }
    out[i] = d;
  }
  return out;
}

/* ───────────────────────────── ensemble ───────────────────────────── */

function computeScores(cs: Candle[]): { scores: Scores; atr: number } {
  const o = cs.map((c) => c.open), h = cs.map((c) => c.high), l = cs.map((c) => c.low), c = cs.map((c) => c.close), v = cs.map((c) => c.volume);
  const atrS = atr(h, l, c, 14), A = last(atrS, 1e-9) + 1e-12;

  // momentum: RSI with dynamic bands, MACD histogram divergence, Stochastic
  const r = rsi(c, 14), rw = tail(r, 50), rNow = last(r, 50), rMid = rw.length >= 20 ? mean(rw) : 50, rBand = Math.max(rw.length >= 20 ? std(rw) : 10, 1e-9);
  const up = rMid + 1.5 * rBand, lo = rMid - 1.5 * rBand;
  const rsiScore = rNow > up ? -Math.min((rNow - up) / rBand, 1) : rNow < lo ? Math.min((lo - rNow) / rBand, 1) : (rNow - rMid) / (1.5 * rBand);
  const hist = macdHist(c), hNow = last(hist), hPrev = at(hist, -2), hScale = mean(tail(hist, 30).map(Math.abs)) + 1e-12;
  let histScore = Math.tanh(hNow / hScale); if (hNow > 0 !== hNow > hPrev) histScore *= 0.5;
  const { K, D } = stoch(h, l, c), k = last(K, 50), d = last(D, 50);
  const stochScore = clip(0.6 * clip((k - d) / 20) + 0.4 * (k > 80 || k < 20 ? clip((50 - k) / 30) : 0));
  const momentum = clip(0.4 * rsiScore + 0.35 * histScore + 0.25 * stochScore);

  // volatility: Bollinger %B mean reversion / squeeze break, scaled by ATR ratio
  const bb = bbands(c), pctB = last(bb.pctB, 0.5), wNow = last(bb.width), wQ = quantile(tail(bb.width, 50), 0.2);
  const atrRatio = last(atrS, 1) / (mean(tail(atrS, 50)) + 1e-12);
  const brk = c[c.length - 1] - c[c.length - 2];
  const volBase = wNow < wQ ? (brk === 0 ? 0 : Math.sign(brk) * 0.5) : clip((0.5 - pctB) * 2);
  const volatility = clip(volBase * Math.min(Math.max(atrRatio, 0.5), 1.5));

  // price action: pin bar, engulfing, inside bar, 3-candle body momentum
  const n = c.length - 1, rng = h[n] - l[n] + 1e-12, body = Math.abs(c[n] - o[n]);
  const uw = h[n] - Math.max(o[n], c[n]), lw = Math.min(o[n], c[n]) - l[n]; let pa = 0;
  if (body / rng < 0.33) { if (lw / rng > 0.6) pa += 0.7; else if (uw / rng > 0.6) pa -= 0.7; }
  const prevBody = Math.abs(c[n - 1] - o[n - 1]);
  if (c[n] > o[n] && c[n - 1] < o[n - 1] && c[n] >= o[n - 1] && o[n] <= c[n - 1] && body > prevBody) pa += 0.8;
  else if (c[n] < o[n] && c[n - 1] > o[n - 1] && c[n] <= o[n - 1] && o[n] >= c[n - 1] && body > prevBody) pa -= 0.8;
  if (h[n] <= h[n - 1] && l[n] >= l[n - 1]) pa += c[n - 1] > o[n - 1] ? 0.3 : -0.3;
  pa += 0.2 * Math.sign(c.slice(-3).reduce((s, v, i) => s + v - o.slice(-3)[i], 0));
  const price_action = clip(pa);

  // trend: EMA 9/21 slope & spread, VWAP bias, SuperTrend direction
  const e9 = ema(c, 9), e21 = ema(c, 21);
  const slope9 = (last(e9) - at(e9, -4)) / (3 * A), spread = (last(e9) - last(e21)) / A;
  const emaScore = clip(0.5 * Math.tanh(2 * slope9) + 0.5 * Math.tanh(spread));
  const vwapScore = clip(Math.tanh((last(c) - last(vwap(h, l, c, v), last(c))) / A));
  const trend = clip(0.45 * emaScore + 0.25 * vwapScore + 0.3 * last(supertrendDir(h, l, c), 0));

  return { scores: { momentum, volatility, price_action, trend }, atr: A };
}

/* ───────────────────────────── engine ───────────────────────────── */

class Engine {
  cfg: Config; buffer: Candle[] = [];
  w: Record<Sub, number> = { momentum: 0.25, volatility: 0.25, price_action: 0.25, trend: 0.25 };
  errors: boolean[] = []; results: boolean[] = []; updates = 0;
  calN = new Array(10).fill(0); calHits = new Array(10).fill(0);
  scores: Scores = { momentum: 0, volatility: 0, price_action: 0, trend: 0 }; horizons: Horizon[] = []; atr = 0;
  prevScores: Scores | null = null; prevPred: 0 | 1 | -1 = 0; prevConf = 0.5;
  trades: Trade[] = []; pending: Trade | null = null; lastTradeTs = -Infinity; pnl = 0;
  log: string[] = [];

  constructor(cfg: Config) { this.cfg = cfg; }

  get defending() { return this.errors.filter(Boolean).length >= this.cfg.defenseTrigger; }
  get state(): "NORMAL" | "LEARNING" | "DEFENDING" { return this.defending ? "DEFENDING" : this.results.length < this.cfg.accuracyWindow ? "LEARNING" : "NORMAL"; }
  get threshold() { return this.defending ? this.cfg.defenseThreshold : this.cfg.baseThreshold; }
  get accuracy() { return this.results.length ? this.results.filter(Boolean).length / this.results.length : NaN; }
  get winRate() { return this.trades.length ? this.trades.filter((t) => (t.result ?? 0) > 0).length / this.trades.length : NaN; }
  aggregate(s: Scores) { return SUBSYSTEMS.reduce((a, k) => a + this.w[k] * s[k], 0); }

  private calIdx(conf: number) { return Math.min(Math.floor(((conf - 0.5) / 0.5) * 10), 9); }
  empirical(conf: number) { const i = this.calIdx(conf); return this.calN[i] >= 5 ? this.calHits[i] / this.calN[i] : NaN; }

  private update(scores: Scores, predicted: number, actual: number): boolean | null {
    if (predicted === 0 || actual === 0) return null;
    const correct = predicted === actual;
    this.results.push(correct); if (this.results.length > this.cfg.accuracyWindow) this.results.shift();
    this.errors.push(!correct); if (this.errors.length > this.cfg.lossMemory) this.errors.shift();
    this.updates++;
    for (const k of SUBSYSTEMS) {
      const s = scores[k]; if (s === 0) continue;
      if (Math.sign(s) !== actual) this.w[k] *= 1 - this.cfg.eta * Math.abs(s);      // MWU penalty
      else if (correct) this.w[k] *= 1 + this.cfg.reward * Math.abs(s);
    }
    for (const k of SUBSYSTEMS) this.w[k] = Math.max(this.w[k], this.cfg.weightFloor);
    const tot = SUBSYSTEMS.reduce((a, k) => a + this.w[k], 0);
    for (const k of SUBSYSTEMS) this.w[k] /= tot;
    return correct;
  }

  private forecast(close: number, agg: number): Horizon[] {
    return Array.from({ length: this.cfg.horizons }, (_, i) => {
      const k = i + 1, bias = agg * this.cfg.horizonDecay ** (k - 1);
      const confidence = 1 / (1 + Math.exp(-Math.abs(bias * this.cfg.scoreScale)));
      const direction: Dir = bias > this.cfg.neutralBand ? "CALL" : bias < -this.cfg.neutralBand ? "PUT" : "NEUTRAL";
      return { step: k, direction, target: close + k * this.atr * bias, confidence };
    });
  }

  onCandle(c: Candle): Horizon | null {
    this.buffer.push(c); if (this.buffer.length > this.cfg.bufferSize) this.buffer.shift();
    const actual = c.close > c.open ? 1 : c.close < c.open ? -1 : 0;
    if (this.pending) { const won = actual === this.pending.direction; this.pending.result = won ? this.pending.stake * this.cfg.payout : -this.pending.stake; this.pnl += this.pending.result; this.trades.push(this.pending); this.pending = null; }
    let verdict = "";
    if (this.prevScores) { const ok = this.update(this.prevScores, this.prevPred, actual); if (ok !== null) { const i = this.calIdx(this.prevConf); this.calN[i]++; this.calHits[i] += ok ? 1 : 0; verdict = ok ? "  HIT" : "  MISS"; } }
    this.log.unshift(`${new Date(c.ts * 1000).toTimeString().slice(0, 5)}  O ${c.open.toFixed(5)}  C ${c.close.toFixed(5)}  ${actual > 0 ? "UP" : actual < 0 ? "DN" : "--"}${verdict}`);
    if (this.log.length > 8) this.log.pop();
    if (this.buffer.length < this.cfg.warmup) { this.prevScores = null; this.prevPred = 0; return null; }

    const { scores, atr } = computeScores(this.buffer); this.scores = scores; this.atr = atr;
    this.horizons = this.forecast(c.close, this.aggregate(scores));
    const h1 = this.horizons[0];
    this.prevScores = { ...scores }; this.prevPred = h1.direction === "CALL" ? 1 : h1.direction === "PUT" ? -1 : 0; this.prevConf = h1.confidence;
    const fires = this.prevPred !== 0 && h1.confidence >= this.threshold;
    if (fires && this.cfg.autoTrade && !this.pending && c.ts - this.lastTradeTs >= this.cfg.cooldownSec) {
      this.pending = { ts: c.ts, direction: this.prevPred as 1 | -1, entry: c.close, stake: this.cfg.stake, result: null }; this.lastTradeTs = c.ts;
    }
    return fires ? h1 : null;
  }
}

class CandleAggregator {
  cur: Candle | null = null; private partial = true;
  add(ts: number, price: number): Candle | null {
    const bucket = Math.floor(ts / 60) * 60; let done: Candle | null = null;
    if (!this.cur) this.cur = { ts: bucket, open: price, high: price, low: price, close: price, volume: 1 };
    else if (bucket !== this.cur.ts) { if (!this.partial) done = this.cur; this.partial = false; this.cur = { ts: bucket, open: price, high: price, low: price, close: price, volume: 1 }; }
    else { this.cur.high = Math.max(this.cur.high, price); this.cur.low = Math.min(this.cur.low, price); this.cur.close = price; this.cur.volume++; }
    return done;
  }
}

/* ───────────────────────────── utils ───────────────────────────── */

const pct = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? "n/a" : `${(v * 100).toFixed(1)}%`);
const bar = (v: number, w = 10) => { const f = Math.round(Math.max(0, Math.min(1, v)) * w); return "█".repeat(f) + "░".repeat(w - f); };
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
function beep() { try { const ac = new AudioContext(), o = ac.createOscillator(), g = ac.createGain(); o.frequency.value = 880; o.connect(g); g.connect(ac.destination); g.gain.value = 0.2; o.start(); o.stop(ac.currentTime + 0.25); } catch { /* audio blocked */ } }
function parseCsv(text: string): Candle[] {
  return text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean).map((l) => l.split(",")).filter((p) => p.length >= 5 && !Number.isNaN(parseFloat(p[1])))
    .map((p) => { const t = Number(p[0]); return { ts: Number.isFinite(t) ? t : Date.parse(p[0]) / 1000, open: +p[1], high: +p[2], low: +p[3], close: +p[4], volume: +(p[5] ?? 0) || 0 }; });
}

/* ───────────────────────────── small components ───────────────────────────── */

function RegionPicker({ src, natural, onPick, onCancel }: { src: string; natural: { w: number; h: number }; onPick: (r: Region) => void; onCancel: () => void }) {
  const [start, setStart] = useState<{ x: number; y: number } | null>(null);
  const [rect, setRect] = useState<Region | null>(null);
  const dispW = Math.min(960, natural.w), scale = dispW / natural.w;
  const pos = (e: React.MouseEvent) => { const b = e.currentTarget.getBoundingClientRect(); return { x: e.clientX - b.left, y: e.clientY - b.top }; };
  return (
    <div style={S.card}>
      <div>Drag a box tightly around the <b>current price readout</b> on the chart.</div>
      <div style={{ position: "relative", width: dispW, cursor: "crosshair", userSelect: "none" }}
        onMouseDown={(e) => { const p = pos(e); setStart(p); setRect({ x: p.x, y: p.y, w: 0, h: 0 }); }}
        onMouseMove={(e) => { if (!start) return; const p = pos(e); setRect({ x: Math.min(start.x, p.x), y: Math.min(start.y, p.y), w: Math.abs(p.x - start.x), h: Math.abs(p.y - start.y) }); }}
        onMouseUp={() => setStart(null)}>
        <img src={src} alt="capture" draggable={false} style={{ width: dispW, display: "block", borderRadius: 6 }} />
        {rect && <div style={{ position: "absolute", left: rect.x, top: rect.y, width: rect.w, height: rect.h, border: "2px solid #22c55e", background: "rgba(34,197,94,.15)" }} />}
      </div>
      <div style={S.row}>
        <button style={S.btn} disabled={!rect || rect.w < 5} onClick={() => rect && onPick({ x: Math.round(rect.x / scale), y: Math.round(rect.y / scale), w: Math.round(rect.w / scale), h: Math.round(rect.h / scale) })}>Use region</button>
        <button style={S.btnGhost} onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

function ProjectionTable({ e }: { e: Engine }) {
  const col = { CALL: "#22c55e", PUT: "#ef4444", NEUTRAL: "#9ca3af" } as const;
  const icon = { CALL: "▲", PUT: "▼", NEUTRAL: "•" } as const;
  return (
    <div style={S.card}>
      <div style={S.title}>5-Candle Projection Matrix</div>
      <table style={S.table}>
        <thead><tr>{["Horizon", "Direction", "Target close", "Sigmoid conf", "Empirical hit-rate"].map((h) => <th key={h} style={S.th}>{h}</th>)}</tr></thead>
        <tbody>
          {e.horizons.length === 0 && <tr><td colSpan={5} style={{ ...S.td, opacity: 0.6 }}>warming up: {e.buffer.length}/{e.cfg.warmup} candles</td></tr>}
          {e.horizons.map((h) => (
            <tr key={h.step}>
              <td style={S.td}>+{h.step}</td>
              <td style={{ ...S.td, color: col[h.direction], fontWeight: 700 }}>{h.direction} {icon[h.direction]}</td>
              <td style={{ ...S.td, ...S.mono }}>{h.target.toFixed(5)}</td>
              <td style={{ ...S.td, ...S.mono }}>{pct(h.confidence)} {bar(h.confidence)}</td>
              <td style={{ ...S.td, ...S.mono }}>{h.step === 1 ? pct(e.empirical(h.confidence)) : "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ───────────────────────────── main component ───────────────────────────── */

export default function TradingAgent() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [asset, setAsset] = useState("EURUSD OTC");
  const [threshold, setThreshold] = useState(DEFAULT_CFG.baseThreshold);
  const [eta, setEta] = useState(DEFAULT_CFG.eta);
  const [paper, setPaper] = useState(false);
  const [hz, setHz] = useState(3);
  const [price, setPrice] = useState<number | null>(null);
  const [ocrFails, setOcrFails] = useState(0);
  const [ocrText, setOcrText] = useState("");
  const [testRead, setTestRead] = useState<TestRead | null>(null);
  const [testing, setTesting] = useState(false);
  const [snapshot, setSnapshot] = useState<{ src: string; w: number; h: number } | null>(null);
  const [region, setRegion] = useState<Region | null>(null);
  const [alert, setAlert] = useState<{ h: Horizon; expires: number } | null>(null);
  const [now, setNow] = useState(Date.now() / 1000);
  const [, bump] = useState(0);
  const [engineHealth, setEngineHealth] = useState<string | null>(null);

  const engine = useRef(new Engine({ ...DEFAULT_CFG }));
  const agg = useRef(new CandleAggregator());
  const video = useRef<HTMLVideoElement>(null);
  const work = useRef<HTMLCanvasElement>(null);
  const worker = useRef<Worker | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const recent = useRef<number[]>([]);
  const regionRef = useRef<Region | null>(null);

  useEffect(() => { const e = engine.current; e.cfg.baseThreshold = threshold; e.cfg.eta = eta; e.cfg.autoTrade = paper; }, [threshold, eta, paper]);
  useEffect(() => { regionRef.current = region; }, [region]);
  useEffect(() => { const id = setInterval(() => setNow(Date.now() / 1000), 250); return () => clearInterval(id); }, []);
  // Mirror the unified backend's ensemble engine (GET /trading/health) so the
  // hub shows server engine state — restored wiring, no behaviour change.
  useEffect(() => {
    let cancelled = false;
    saraFetch(`${SARA_API_URL}/trading/health`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!cancelled && d?.status) setEngineHealth(`${d.engine ?? "engine"} · ${d.status}`); })
      .catch(() => { if (!cancelled) setEngineHealth(null); });
    return () => { cancelled = true; };
  }, []);

  /* screen capture */
  async function connect() {
    try {
      const s = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 10 }, audio: false });
      stream.current = s; const v = video.current!; v.srcObject = s; await v.play();
      s.getVideoTracks()[0].onended = stop;
      const cv = document.createElement("canvas"); cv.width = v.videoWidth; cv.height = v.videoHeight;
      cv.getContext("2d")!.drawImage(v, 0, 0);
      setSnapshot({ src: cv.toDataURL("image/png"), w: v.videoWidth, h: v.videoHeight }); setPhase("picking");
      if (!worker.current) {
        // tesseract.js is browser-only — load it lazily so Next never bundles it
        // into the server render of this page.
        const { createWorker, PSM } = await import("tesseract.js");
        const w = await createWorker("eng");
        await w.setParameters({ tessedit_char_whitelist: "0123456789.,", tessedit_pageseg_mode: PSM.SINGLE_LINE });
        worker.current = w;
      }
    } catch (err) { setOcrText(`capture failed: ${(err as Error).message}`); }
  }
  function stop() { stream.current?.getTracks().forEach((t) => t.stop()); stream.current = null; setPhase("idle"); setRegion(null); setAlert(null); }

  /* Shared OCR pipeline: crop the region from the live feed, upscale 3x,
   * grayscale + binarize (inverting dark themes) — the exact pixels tesseract
   * receives — then recognize. Also returns the processed frame as a PNG so
   * the debugger can show what the engine actually sees. */
  async function ocrFrame() {
    const v = video.current!, r = regionRef.current!, cv = work.current!, Sx = 3;
    cv.width = r.w * Sx; cv.height = r.h * Sx;
    const ctx = cv.getContext("2d", { willReadFrequently: true })!;
    ctx.drawImage(v, r.x, r.y, r.w, r.h, 0, 0, cv.width, cv.height);
    const img = ctx.getImageData(0, 0, cv.width, cv.height), d = img.data; let sum = 0;
    for (let i = 0; i < d.length; i += 4) { const g = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; d[i] = g; sum += g; }
    const dark = sum / (d.length / 4) < 128;
    for (let i = 0; i < d.length; i += 4) { let g = d[i]; if (dark) g = 255 - g; const b = g > 150 ? 255 : 0; d[i] = d[i + 1] = d[i + 2] = b; d[i + 3] = 255; }
    ctx.putImageData(img, 0, 0);
    const { data } = await worker.current!.recognize(cv);
    const raw = data.text.trim();
    const m = raw.replace(/,/g, ".").match(/\d+(?:\.\d+)?/);
    const parsed = m ? parseFloat(m[0]) : null;
    return { url: cv.toDataURL("image/png"), raw, parsed, conf: data.confidence, dark, bright: sum / (d.length / 4), w: cv.width, h: cv.height };
  }

  async function readPrice(): Promise<number | null> {
    const f = await ocrFrame();
    setOcrText(f.raw);
    if (f.parsed == null || !(f.parsed > 0)) return null;
    const rec = recent.current;
    if (rec.length) { const med = [...rec].sort((a, b) => a - b)[rec.length >> 1]; if (Math.abs(f.parsed - med) / med > 0.005) return null; }
    rec.push(f.parsed); if (rec.length > 5) rec.shift(); return f.parsed;
  }

  /* One-shot debug read: same pipeline as the live loop, but the frame and
   * raw OCR are captured and rendered so you can tune the region in seconds. */
  async function runTestRead() {
    if (!regionRef.current || !worker.current) { setOcrText("connect screen and pick a region first"); return; }
    setTesting(true);
    try {
      const f = await ocrFrame();
      let medianOk: boolean | null = null;
      if (f.parsed != null && f.parsed > 0) {
        const rec = recent.current;
        if (rec.length) { const med = [...rec].sort((a, b) => a - b)[rec.length >> 1]; medianOk = Math.abs(f.parsed - med) / med <= 0.005; }
      }
      setTestRead({ at: Date.now(), ...f, medianOk, region: regionRef.current });
      setOcrText(f.raw);
    } catch (err) {
      setTestRead({ at: Date.now(), url: "", raw: "", parsed: null, conf: 0, dark: false, bright: 0, medianOk: null, w: 0, h: 0, region: regionRef.current, error: (err as Error).message });
    } finally { setTesting(false); }
  }

  function onClose(c: Candle) {
    const fired = engine.current.onCandle(c);
    if (fired) { setAlert({ h: fired, expires: agg.current.cur!.ts + 60 }); beep(); } else setAlert(null);
    bump((n) => n + 1);
  }

  /* OCR loop */
  useEffect(() => {
    if (phase !== "active") return; let stopped = false;
    (async () => {
      while (!stopped) {
        const t0 = performance.now();
        try { const p = await readPrice(); if (p == null) setOcrFails((f) => f + 1); else { setOcrFails(0); setPrice(p); const done = agg.current.add(Date.now() / 1000, p); if (done) onClose(done); } }
        catch { setOcrFails((f) => f + 1); }
        await sleep(Math.max(0, 1000 / hz - (performance.now() - t0)));
      }
    })();
    return () => { stopped = true; };
  }, [phase, hz]);

  function seed(file: File) { file.text().then((t) => { const cs = parseCsv(t); cs.forEach((c) => engine.current.onCandle(c)); engine.current.log.unshift(`seeded ${cs.length} candles from ${file.name}`); bump((n) => n + 1); }); }

  const e = engine.current;
  const secondsLeft = 60 - (now % 60);
  const atrState = e.atr && price ? (() => { const rel = (e.atr / price) * 1e4; return `${e.atr.toFixed(5)} (${rel > 8 ? "HIGH" : rel < 2 ? "LOW" : "NORMAL"})`; })() : "n/a";
  const liveAlert = alert && now < alert.expires ? alert : null;
  const stateColor = { NORMAL: "#22c55e", LEARNING: "#eab308", DEFENDING: "#ef4444" }[e.state];
  const phaseLabel = { idle: "NOT CONNECTED", picking: "PICK REGION", ready: "READY", active: e.buffer.length < e.cfg.warmup ? `WARMING ${e.buffer.length}/${e.cfg.warmup}` : "ACTIVE", paused: "PAUSED" }[phase];

  /* test-read verdict + hint */
  let verdict = "";
  let hint = "";
  let verdictColor = "#eab308";
  if (testRead) {
    if (testRead.error) { verdict = "OCR ERROR"; hint = testRead.error; verdictColor = "#ef4444"; }
    else if (!testRead.raw) { verdict = "EMPTY CROP"; hint = "No characters found. The box is likely over chart lines or blank space - re-pick a tight box around just the price digits."; verdictColor = "#ef4444"; }
    else if (testRead.parsed == null) { verdict = "NO DIGITS FOUND"; hint = `OCR saw text but no number (digits whitelist: 0-9 . ,). Raw text: "${testRead.raw}"`; verdictColor = "#ef4444"; }
    else if (!(testRead.parsed > 0)) { verdict = "INVALID VALUE"; hint = "Parsed value is not a positive price."; verdictColor = "#ef4444"; }
    else if (testRead.medianOk === false) { verdict = "LIKELY MISREAD"; hint = "This read jumps more than 0.5% from recent readings, so the live loop would discard it. Check the box isn't catching two numbers or a scrolling axis label."; verdictColor = "#eab308"; }
    else if (testRead.medianOk === null) { verdict = "OK - FIRST READ"; hint = "No prior readings to compare yet; the live loop starts trusting this once it repeats consistently."; verdictColor = "#22c55e"; }
    else { verdict = `OK - WOULD FEED THE ENGINE @ ${testRead.parsed.toFixed(5)}`; verdictColor = "#22c55e"; }
  }

  return (
    <div style={S.page}>
      <style dangerouslySetInnerHTML={{ __html: TRADING_CSS }} />
      <video ref={video} muted playsInline style={{ position: "fixed", width: 1, height: 1, opacity: 0, pointerEvents: "none" }} />
      <canvas ref={work} style={{ display: "none" }} />

      {/* header + controls */}
      <div style={S.card}>
        <div style={S.row}>
          <input value={asset} onChange={(ev) => setAsset(ev.target.value)} style={S.input} />
          <span style={{ ...S.mono, fontSize: 22 }}>{price?.toFixed(5) ?? "-"}</span>
          <span>ATR14 {atrState}</span>
          <span>close in <b style={S.mono}>{secondsLeft.toFixed(1)}s</b></span>
          <span style={{ color: phase === "active" ? "#22c55e" : "#eab308", fontWeight: 700 }}>{phaseLabel}</span>
          {engineHealth && <span style={S.mono}>{engineHealth}</span>}
        </div>
        <div style={S.row}>
          {phase === "idle" && <button style={S.btn} onClick={connect}>Connect screen</button>}
          {phase === "ready" && <button style={S.btn} onClick={() => setPhase("active")}>▶ Active</button>}
          {phase === "paused" && <button style={S.btn} onClick={() => setPhase("active")}>▶ Resume</button>}
          {phase === "active" && <button style={S.btnWarn} onClick={() => setPhase("paused")}>⏸ Pause</button>}
          {phase !== "idle" && phase !== "picking" && <button style={S.btnGhost} onClick={() => setPhase("picking")}>Re-pick region</button>}
          {phase !== "idle" && phase !== "picking" && (
            <button style={{ ...S.btnGhost, opacity: testing || !worker.current ? 0.5 : 1 }} disabled={testing || !worker.current} onClick={runTestRead}>
              {testing ? "Reading…" : "Test read"}
            </button>
          )}
          {phase !== "idle" && <button style={S.btnGhost} onClick={stop}>Stop</button>}
          <label style={S.lbl}>threshold {pct(threshold)}<input type="range" min={0.6} max={0.98} step={0.01} value={threshold} onChange={(ev) => setThreshold(+ev.target.value)} /></label>
          <label style={S.lbl}>η {eta.toFixed(2)}<input type="range" min={0.02} max={0.5} step={0.01} value={eta} onChange={(ev) => setEta(+ev.target.value)} /></label>
          <label style={S.lbl}>OCR {hz}Hz<input type="range" min={1} max={6} step={1} value={hz} onChange={(ev) => setHz(+ev.target.value)} /></label>
          <label style={S.lbl}><input type="checkbox" checked={paper} onChange={(ev) => setPaper(ev.target.checked)} /> paper auto-trade</label>
          <label style={S.lbl}>seed CSV <input type="file" accept=".csv" onChange={(ev) => ev.target.files?.[0] && seed(ev.target.files[0])} /></label>
        </div>
        <div style={{ ...S.mono, fontSize: 12, opacity: 0.7 }}>
          OCR: {ocrFails ? <span style={{ color: "#ef4444" }}>{ocrFails} misses</span> : "ok"} · raw {'"'}{ocrText}{'"'}
        </div>
      </div>

      {/* test-read debugger */}
      {testRead && (
        <div style={S.card}>
          <div style={{ ...S.row, justifyContent: "space-between" }}>
            <div style={S.title}>Test read debugger</div>
            <button style={S.btnGhost} onClick={() => setTestRead(null)}>Dismiss</button>
          </div>
          {testRead.error ? (
            <div style={{ ...S.mono, fontSize: 12, color: "#ef4444" }}>error: {testRead.error}</div>
          ) : (
            <div style={{ ...S.row, alignItems: "flex-start" }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div style={{ fontSize: 12, opacity: 0.7 }}>What tesseract sees (3x upscale, binarized{testRead.dark ? ", dark mode inverted" : ""})</div>
                <img src={testRead.url} alt="OCR crop" style={{ border: "1px solid #374151", borderRadius: 6, imageRendering: "pixelated", maxWidth: 340, maxHeight: 130, background: "#fff" }} />
                <div style={{ ...S.mono, fontSize: 11, opacity: 0.6 }}>
                  {testRead.w}x{testRead.h}px crop · region {testRead.region.x},{testRead.region.y} {testRead.region.w}x{testRead.region.h} · brightness {Math.round(testRead.bright)}/255 · conf {Math.round(testRead.conf)}%
                </div>
              </div>
              <div style={{ ...S.mono, fontSize: 13, display: "flex", flexDirection: "column", gap: 4 }}>
                <div>raw OCR: '{testRead.raw || "(empty)"}'</div>
                <div>parsed: {testRead.parsed != null ? testRead.parsed.toFixed(5) : "none"}</div>
                <div style={{ fontWeight: 700, color: verdictColor }}>verdict: {verdict}</div>
                {hint && <div style={{ fontSize: 12, opacity: 0.75 }}>{hint}</div>}
              </div>
            </div>
          )}
        </div>
      )}

      {phase === "picking" && snapshot && (
        <RegionPicker src={snapshot.src} natural={{ w: snapshot.w, h: snapshot.h }}
          onPick={(r) => { setRegion(r); recent.current = []; setPhase("ready"); }}
          onCancel={() => setPhase(region ? "ready" : "idle")} />
      )}

      {/* signal banner */}
      <div style={{ ...S.banner, background: liveAlert ? (liveAlert.h.direction === "CALL" ? "#16a34a" : "#dc2626") : "#1f2937" }}>
        {liveAlert
          ? `${liveAlert.h.direction === "CALL" ? "▲" : "▼"}  ${liveAlert.h.direction}  ·  ${pct(liveAlert.h.confidence)}  ·  target ${liveAlert.h.target.toFixed(5)}  ·  place within ${Math.max(0, liveAlert.expires - now).toFixed(0)}s`
          : "no signal above threshold"}
      </div>

      <ProjectionTable e={e} />

      {/* diagnostics + paper */}
      <div style={S.grid2}>
        <div style={S.card}>
          <div style={S.title}>Adaptation Diagnostics</div>
          {SUBSYSTEMS.map((k) => (
            <div key={k} style={{ ...S.row, ...S.mono, fontSize: 13 }}>
              <span style={{ width: 110 }}>{k}</span>
              <span style={{ color: e.scores[k] > 0 ? "#22c55e" : e.scores[k] < 0 ? "#ef4444" : "#9ca3af" }}>{e.scores[k] >= 0 ? "+" : ""}{e.scores[k].toFixed(3)}</span>
              <span>w {e.w[k].toFixed(3)} {bar(e.w[k])}</span>
            </div>
          ))}
          <div style={S.row}>
            <span style={{ color: stateColor, fontWeight: 700 }}>{e.state}</span>
            <span>entry threshold {pct(e.threshold)}</span>
            <span>acc(20) {pct(e.accuracy)}</span>
            <span>updates {e.updates}</span>
          </div>
        </div>
        <div style={S.card}>
          <div style={S.title}>Paper Execution {paper ? "(AUTO)" : "(OFF)"} · 60s lock</div>
          <div style={S.row}>
            <span>trades {e.trades.length}</span><span>win {pct(e.winRate)}</span>
            <span style={{ color: e.pnl >= 0 ? "#22c55e" : "#ef4444" }}>PnL {e.pnl >= 0 ? "+" : ""}{e.pnl.toFixed(2)}</span>
            <span>pending {e.pending ? `${e.pending.direction > 0 ? "CALL" : "PUT"} @ ${e.pending.entry.toFixed(5)}` : "-"}</span>
          </div>
          <div style={{ fontSize: 12, opacity: 0.6 }}>Real orders are placed by you in the broker tab. Paper mode only records what following every alert would have returned at {Math.round(e.cfg.payout * 100)}% payout.</div>
        </div>
      </div>

      <div style={S.card}>
        <div style={S.title}>Candle log</div>
        <pre style={{ ...S.mono, margin: 0, fontSize: 12, opacity: 0.85 }}>{e.log.join("\n") || "-"}</pre>
      </div>
    </div>
  );
}

/* ───────────────────────────── styles ───────────────────────────── */

const S = {
  page: { maxWidth: 1000, margin: "0 auto", padding: "4px 0 32px", display: "flex", flexDirection: "column", gap: 12, background: "transparent", color: "#e5e7eb", fontFamily: "system-ui, sans-serif", fontSize: 14 } as React.CSSProperties,
  card: { background: "#111827", borderRadius: 10, padding: 14, display: "flex", flexDirection: "column", gap: 10 } as React.CSSProperties,
  row: { display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" } as React.CSSProperties,
  grid2: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 } as React.CSSProperties,
  title: { fontWeight: 700, fontSize: 13, letterSpacing: 1, textTransform: "uppercase", opacity: 0.8 } as React.CSSProperties,
  banner: { padding: 16, borderRadius: 10, textAlign: "center", fontWeight: 800, fontSize: 18, color: "#fff", letterSpacing: 0.5 } as React.CSSProperties,
  table: { width: "100%", borderCollapse: "collapse" } as React.CSSProperties,
  th: { textAlign: "left", padding: "6px 8px", fontSize: 12, opacity: 0.7, borderBottom: "1px solid #374151" } as React.CSSProperties,
  td: { padding: "6px 8px", borderBottom: "1px solid #1f2937" } as React.CSSProperties,
  mono: { fontFamily: "ui-monospace, SFMono-Regular, monospace" } as React.CSSProperties,
  input: { background: "#1f2937", color: "#e5e7eb", border: "1px solid #374151", borderRadius: 6, padding: "6px 10px", fontWeight: 700 } as React.CSSProperties,
  lbl: { display: "flex", alignItems: "center", gap: 6, fontSize: 12 } as React.CSSProperties,
  btn: { background: "#22c55e", color: "#052e16", border: 0, borderRadius: 6, padding: "8px 14px", fontWeight: 700, cursor: "pointer" } as React.CSSProperties,
  btnWarn: { background: "#eab308", color: "#1c1917", border: 0, borderRadius: 6, padding: "8px 14px", fontWeight: 700, cursor: "pointer" } as React.CSSProperties,
  btnGhost: { background: "transparent", color: "#e5e7eb", border: "1px solid #374151", borderRadius: 6, padding: "8px 14px", cursor: "pointer" } as React.CSSProperties,
};
