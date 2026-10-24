'use client';

/**
 * SARA ULTRON — command center.
 *
 * A supervisory HUD built around the holographic core, which stays the visual
 * and functional centre of the surface: every readout on this page exists to
 * explain what the core is doing, and the core's own state is driven by real
 * backend phases rather than a timer.
 *
 *   region              real source
 *   ------------------  -----------------------------------------------------
 *   holographic core    phase events + real completed/planned step ratio
 *   VOICE CHANNEL       GET /ultron/voice/state (derived, never guessed)
 *   ACTIVE AGENTS       GET /ultron/agents (live HTTP health probes)
 *   TASK PROGRESS       GET /ultron/tasks (the session store, authenticated)
 *   CONNECTED DEVICES   GET /ultron/devices (heartbeat-derived state)
 *   lifecycle strip     `phase` events from the orchestrator/executor
 *   execution plan      `plan`, `replan`, `tool_call`, `tool_result`
 *   live trace          `log` and `done`
 *   memory + capability GET /ultron/memory, GET /ultron/capabilities
 *
 * The four HUD regions above the console are new; everything below the console
 * (plan, steps, trace, fleet detail, memory, capability surface) is the surface
 * that was already here, kept intact.
 *
 * Auth: writes go through `saraFetch`, which attaches the shared API key from
 * `NEXT_PUBLIC_SARA_API_KEY`. No credential appears in this file.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ULTRON_API_URL, saraFetch } from '@/lib/api';
import UltronCoreCanvas from '@/components/hub/UltronCoreCanvas';
import AgentStrip from '@/components/ultron/AgentStrip';
import ContextHud from '@/components/ultron/ContextHud';
import DevicePanel from '@/components/ultron/DevicePanel';
import FleetPanel from '@/components/ultron/FleetPanel';
import MemoryPanel from '@/components/ultron/MemoryPanel';
import TaskBoard from '@/components/ultron/TaskBoard';
import VoiceRail, { useVoiceState } from '@/components/ultron/VoiceRail';
import { useAwareness } from '@/components/ultron/ContextHud';
import { PhaseStrip, PlanList, StepTimeline } from '@/components/ultron/Timeline';
import { useUltronRun } from '@/components/ultron/useUltronRun';
import type { UltronCoreMode } from '@/lib/ultronCore';
import '@/components/ultron/console.css';
import '@/components/ultron/hud.css';

const API = `${ULTRON_API_URL}/ultron`;

/** Real run statuses the backend emits on `done`, with their honest labels. */
const STATUS_TONE: Record<string, { label: string; color: string }> = {
  idle: { label: 'STANDBY', color: '#8b99a6' },
  starting: { label: 'SUBMITTING', color: '#ffaa00' },
  running: { label: 'EXECUTING', color: '#00f0ff' },
  completed: { label: 'GOAL VERIFIED', color: '#22d3a5' },
  completed_with_issues: { label: 'COMPLETED // ISSUES', color: '#ffaa00' },
  failed: { label: 'GOAL NOT MET', color: '#ff5470' },
  stopped: { label: 'HALTED BY OPERATOR', color: '#f97316' },
};

/** Concrete objectives the configured tool surface can actually attempt. */
const PROMPT_SUGGESTIONS: Array<{ label: string; prompt: string }> = [
  {
    label: 'TIDY DESKTOP',
    prompt: 'organize the files on my desktop into folders by file type',
  },
  {
    label: 'SYSTEM HEALTH',
    prompt: 'check CPU, memory and disk usage and tell me if anything needs attention',
  },
  {
    label: 'WEATHER',
    prompt: "check today's weather and summarize it in one sentence",
  },
  {
    label: 'WHO ARE YOU',
    prompt: 'who are you and what can you do?',
  },
];

interface Capabilities {
  agent?: string;
  version?: string;
  status?: string;
  tools?: string[];
  native_tools?: string[];
  tools_by_source?: Record<string, string[]>;
  approval_gated_tools?: string[];
  brahma?: { available?: number; unavailable?: number; source_dir?: string | null };
  memory?: { experiences?: number; rules?: number; success_rate?: number | null };
  delegation?: { agents?: string[] };
  llm_active_provider?: string | null;
  active_sessions?: number;
  error?: string;
}

export default function SaraUltronCommandCenter() {
  const run = useUltronRun();
  const voiceState = useVoiceState();

  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [capsError, setCapsError] = useState('');
  const [listening, setListening] = useState(false);
  const [voiceNote, setVoiceNote] = useState('');
  const [speakReplies, setSpeakReplies] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [stopAllNote, setStopAllNote] = useState('');

  const traceRef = useRef<HTMLDivElement>(null);

  /* ── capability probe (read-only, once per mount) ───────────────────────── */
  useEffect(() => {
    let cancelled = false;
    const probe = async () => {
      try {
        const res = await fetch(`${API}/capabilities`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = (await res.json()) as Capabilities;
        if (!cancelled) {
          setCaps(body);
          setCapsError('');
        }
      } catch (err: unknown) {
        if (!cancelled) {
          setCapsError(
            `Capability probe failed: ${err instanceof Error ? err.message : String(err)}`,
          );
        }
      }
    };
    void probe();
    return () => {
      cancelled = true;
    };
  }, []);

  /* ── keep the trace pinned to the newest real event ────────────────────── */
  useEffect(() => {
    // Assign scrollTop on the pane itself rather than calling scrollIntoView on
    // a child: scrollIntoView walks up and scrolls *every* scrollable ancestor,
    // including the document, which yanked the whole command center to the
    // bottom of the page on the first streamed event.
    const pane = traceRef.current;
    if (pane) pane.scrollTop = pane.scrollHeight;
  }, [run.logs, run.steps, run.summary]);

  /* ── voice: the browser mic, and the server mic as a fallback ───────────── */

  /**
   * A transcript from the browser microphone fills the objective box.
   *
   * It deliberately does NOT auto-submit: speech recognition gets names and
   * numbers wrong often enough that firing a machine-action plan off a
   * misheard word is not acceptable. The operator reads it and presses
   * INITIATE, or holds Ctrl+Enter.
   */
  const acceptTranscript = useCallback(
    (text: string) => {
      run.setGoal(text);
      setVoiceNote('');
    },
    [run],
  );

  /** The backend host's own microphone - a fallback, not the primary path. */
  const listen = useCallback(async () => {
    if (listening) return;
    setListening(true);
    setVoiceNote('');
    try {
      const res = await saraFetch(`${API}/voice/listen`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ timeout: 10, phrase_limit: 15 }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        const detail = data?.detail;
        setVoiceNote(
          typeof detail === 'string'
            ? detail
            : detail?.error || `Voice input failed (HTTP ${res.status}).`,
        );
        return;
      }
      const heard = String(data?.text ?? '').trim();
      if (!heard) {
        setVoiceNote('No speech was recognised. Try again, closer to the mic.');
        return;
      }
      run.setGoal(heard);
    } catch {
      setVoiceNote('Could not reach the voice endpoint. Is the backend running?');
    } finally {
      setListening(false);
    }
  }, [listening, run]);

  const speak = useCallback(async (text: string) => {
    if (!text.trim()) return;
    setSpeaking(true);
    try {
      const res = await saraFetch(`${API}/voice/speak`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) {
        const detail = (await res.json().catch(() => null))?.detail;
        setVoiceNote(
          typeof detail === 'string' ? detail : `Speech failed (HTTP ${res.status}).`,
        );
      }
    } catch {
      setVoiceNote('Could not reach the speech endpoint.');
    } finally {
      setSpeaking(false);
    }
  }, []);

  /* Speak each finished run only when the operator asked for it. */
  useEffect(() => {
    if (!speakReplies || !run.summary) return;
    // Kicked off from an async wrapper so the effect never sets state
    // synchronously while React is committing the render.
    const kickoff = async () => {
      await speak(run.summary);
    };
    void kickoff();
  }, [speakReplies, run.summary, speak]);

  /**
   * GLOBAL STOP. Server-side, not a loop of per-task deletes from the browser:
   * a stop that depends on the UI staying responsive is not a stop.
   */
  const stopAll = useCallback(async () => {
    setStopAllNote('Stopping…');
    try {
      const res = await saraFetch(`${API}/emergency-stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revoke_devices: false }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        setStopAllNote(`Global stop failed (HTTP ${res.status}).`);
        return;
      }
      setStopAllNote(
        `Halted ${body.stopped_count} task(s); cut ${body.links_cut.length} device link(s). ` +
          'Device tokens were kept — revoke a device explicitly if you meant to.',
      );
    } catch {
      setStopAllNote('Could not reach the emergency-stop endpoint.');
    }
  }, []);

  /* ── derived, real readouts ────────────────────────────────────────────── */
  const tone = STATUS_TONE[run.status] ?? STATUS_TONE.idle;
  const voice = voiceState.data;
  const awareness = useAwareness();
  const serverSaysSpeaking = voice?.state === 'SPEAKING';

  /**
   * The core's mode is a pure function of real backend state, in priority
   * order: an active run beats ambient voice, and an error beats everything
   * only while nothing is actually executing.
   */
  const coreMode: UltronCoreMode = (() => {
    if (run.busy) return run.coreMode;
    if (speaking || serverSaysSpeaking) return 'speaking';
    // The always-on loop is a genuine LISTENING state - the microphone really
    // is open - so the core looks like it is waiting, not like it is idle.
    if (awareness.data?.always_on.running) return 'listening';
    // Vision only shows while a screen read is actually in flight.
    if (awareness.data?.vision?.changed && awareness.data.vision.tier !== 'app_facts') {
      return 'vision';
    }
    if (voice?.state === 'ERROR') return 'error';
    if (voice?.state === 'EXECUTING') return 'executing';
    if (voice?.state === 'VERIFYING') return 'verifying';
    if (voice?.state === 'THINKING') return 'understanding';
    return run.coreMode;
  })();

  const coreTitle =
    coreMode !== 'idle'
      ? `CORE ${coreMode.toUpperCase()}`
      : run.status === 'idle'
        ? 'CORE IDLE // AWAITING OBJECTIVE'
        : `CORE ${tone.label}`;

  const toolCount = caps?.tools?.length ?? run.toolCount;
  const bySource = caps?.tools_by_source
    ? {
        ultron: caps.tools_by_source.ultron?.length ?? 0,
        desktop: caps.tools_by_source.desktop?.length ?? 0,
        brahma: caps.tools_by_source.brahma?.length ?? 0,
      }
    : null;

  // The scan sweep and the grid glow run only while there is real work in
  // flight, so an idle command center is genuinely still.
  const live =
    run.busy ||
    listening ||
    speaking ||
    serverSaysSpeaking ||
    awareness.data?.always_on.running === true;

  return (
    <main className="ultron-viewport hud" data-live={live ? 'true' : 'false'}>
      <div className="ultron-shell">
        {/* ── masthead ───────────────────────────────────────────────────── */}
        <header className="ultron-masthead">
          <div>
            <span className="ultron-kicker">
              <Link href="/hub" style={{ color: 'inherit', textDecoration: 'none' }}>
                SARA AI NEXUS HUB
              </Link>{' '}
              / SUPERVISORY CORE
            </span>
            <h1>
              SARA <strong>ULTRON</strong>
            </h1>
            <p className="ultron-tagline">
              Goal in, verified outcome out. Ultron decomposes the objective with the
              SARA LLM router, executes every step against the shared desktop action
              registry, re-plans around real failures, and records what happened.
            </p>
          </div>
          <div className="ultron-headstats">
            <div className="ultron-stat">
              <span className="ultron-stat__label">STATE</span>
              <span className="ultron-stat__value" style={{ color: tone.color }}>
                {tone.label}
              </span>
            </div>
            <div className="ultron-stat">
              <span className="ultron-stat__label">TOOLS</span>
              <span className="ultron-stat__value">{toolCount || '—'}</span>
            </div>
            <div className="ultron-stat">
              <span className="ultron-stat__label">PROVIDER</span>
              <span className="ultron-stat__value">
                {caps?.llm_active_provider ?? 'unknown'}
              </span>
            </div>
            <div className="ultron-stat">
              <span className="ultron-stat__label">SESSIONS</span>
              <span className="ultron-stat__value">{caps?.active_sessions ?? 0}</span>
            </div>
          </div>
        </header>

        {capsError && <p className="ultron-warn">{capsError}</p>}

        {/* ── the hologram, with its reticle ─────────────────────────────── */}
        <section className="hud-plate ultron-core">
          <UltronCoreCanvas
            className="ultron-core__canvas"
            mode={coreMode}
            progress={run.progress}
            eventTick={run.eventTick}
            failureTick={run.failureTick}
          />
          <div className="hud-grid" aria-hidden="true" />
          <div className="hud-reticle" aria-hidden="true" />
          <div className="hud-sweep" aria-hidden="true" />

          <div className="ultron-core__overlay">
            <h2 className="ultron-core__title">{coreTitle}</h2>
            <p className="ultron-core__subtitle">
              {run.plan.length > 0
                ? `PLAN ${run.steps.filter((s) => s.status !== 'running').length}/${run.plan.length} EXECUTED`
                : 'NO ACTIVE PLAN'}
              {run.replans > 0 ? ` · REPLANNED ${run.replans}×` : ''}
              {live ? ' · LIVE' : ''}
            </p>
            <div
              className={`ultron-core__bar${coreMode === 'idle' ? ' ultron-core__bar--idle' : ''}`}
            >
              <span style={{ width: `${Math.round(run.progress * 100)}%` }} />
            </div>
          </div>

          <div className="ultron-core__hint">DRAG TO ORBIT · SCROLL TO ZOOM</div>
          <div className="ultron-core__foot">
            <span>PHASE {run.phase}</span>
            {/* `some`, not `map`: mapping to booleans and reading .length is
                always truthy, which printed a bare "CURRENT" with no tool name. */}
            {run.steps.some((s) => s.status === 'running') && (
              <span>
                CURRENT{' '}
                {run.steps.filter((s) => s.status === 'running').slice(-1)[0]?.name}
              </span>
            )}
            <span>
              MEMORY {caps?.memory?.experiences ?? 0} runs / {caps?.memory?.rules ?? 0} rules
            </span>
          </div>
        </section>

        {/* ── the four HUD regions ───────────────────────────────────────── */}
        <div className="ultron-columns">
          <VoiceRail
            state={voice?.state ?? (voiceState.error ? 'UNAVAILABLE' : 'IDLE')}
            reason={voice?.reason ?? voiceState.error ?? 'Waiting for the voice probe…'}
            echoGuardS={voice?.echo_guard_s ?? 0}
            activeRuns={voice?.active_runs ?? 0}
            microphone={voice?.microphone ?? false}
            transcriber={voice?.transcriber ?? false}
            speechSynth={voice?.speech_synth ?? false}
            model={voice?.model ?? null}
            continuous={voice?.continuous ?? false}
            continuousDetail={voice?.continuous_detail ?? ''}
            problems={voice?.problems ?? []}
            onListen={() => void listen()}
            listening={listening}
            disabled={listening}
            disabledReason={voice?.microphone ? 'Record one spoken objective' : 'No microphone'}
            onTranscript={acceptTranscript}
            serverMicAvailable={voice?.microphone ?? false}
            serverMicDetail={
              voice?.microphone
                ? `The backend host has its own input device: ${voice.reason}`
                : 'The backend host has no microphone (expected on a cloud host) — use the browser mic.'
            }
          />

          <TaskBoard highlightTaskId={run.taskId} />
        </div>

        <AgentStrip />

        {/* Context awareness. Deliberately placed after the agent strip and
            before the console: it is a status strip, not a control surface,
            and the hologram stays the centrepiece above it. */}
        <ContextHud />

        <div className="ultron-columns">
          <DevicePanel />

          <section className="ultron-panel">
            <header className="ultron-panel__head">
              <h2>COMMAND CONSOLE</h2>
              <div className="ultron-panel__tools">
                {run.taskId && (
                  <span className="ultron-mono-dim">task {run.taskId.slice(0, 8)}</span>
                )}
                {bySource && (
                  <span className="ultron-mono-dim">
                    {bySource.ultron} native · {bySource.desktop} desktop ·{' '}
                    {bySource.brahma} brahma
                  </span>
                )}
              </div>
            </header>

            <label htmlFor="ultron-objective" className="ultron-subhead">
              OBJECTIVE
            </label>
            <textarea
              id="ultron-objective"
              className="ultron-textarea"
              value={run.goal}
              maxLength={4000}
              placeholder="Describe the outcome you want. Ultron will plan it, execute it, and report only what actually happened."
              onChange={(e) => run.setGoal(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  void run.run();
                }
              }}
            />

            <div className="ultron-row">
              <label className="ultron-toggle">
                <input
                  type="checkbox"
                  checked={run.mode === 'assisted'}
                  onChange={(e) =>
                    run.setMode(e.target.checked ? 'assisted' : 'autonomous')
                  }
                />
                ASSISTED — APPROVE EVERY STEP
              </label>

              <div className="ultron-row" style={{ marginLeft: 'auto' }}>
                <button
                  type="button"
                  className="ultron-btn ultron-btn--primary"
                  disabled={run.busy || !run.goal.trim()}
                  onClick={() => void run.run()}
                >
                  {run.busy ? 'EXECUTING…' : '▶ INITIATE'}
                </button>
                {run.busy && (
                  <button
                    type="button"
                    className="ultron-btn ultron-btn--danger"
                    onClick={() => void run.halt()}
                  >
                    ■ HALT
                  </button>
                )}
              </div>
            </div>

            <div className="ultron-row">
              <button
                type="button"
                className="ultron-btn ultron-btn--danger"
                onClick={() => void stopAll()}
                title="Halt every running task and cut every device link"
              >
                ⛔ STOP ALL
              </button>
              <button
                type="button"
                className="ultron-btn ultron-btn--ghost"
                disabled={!voice?.speech_synth}
                title={voice?.speech_synth ? 'Read each finished report aloud' : 'No speech synthesiser'}
                onClick={() => {
                  const next = !speakReplies;
                  setSpeakReplies(next);
                  if (!next) {
                    void saraFetch(`${API}/voice/cancel`, { method: 'POST' }).catch(
                      () => undefined,
                    );
                  }
                }}
              >
                {speakReplies ? '🔊 REPLIES ON' : '🔈 REPLIES OFF'}
              </button>
              {stopAllNote && <span className="ultron-mono-dim">{stopAllNote}</span>}
            </div>

            {voiceNote && <p className="ultron-warn">{voiceNote}</p>}

            <div className="ultron-suggestions">
              {PROMPT_SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion.label}
                  type="button"
                  className="ultron-suggestion"
                  title={suggestion.prompt}
                  disabled={run.busy}
                  onClick={() => void run.run(suggestion.prompt)}
                >
                  {suggestion.label}
                </button>
              ))}
            </div>

            {run.approval && (
              <div className="ultron-gate">
                <span className="ultron-gate__prompt">
                  ⚠ AUTHORIZATION REQUIRED — {run.approval.prompt}
                </span>
                <div className="ultron-row">
                  <button
                    type="button"
                    className="ultron-btn ultron-btn--primary"
                    onClick={() => void run.approve('APPROVE')}
                  >
                    APPROVE
                  </button>
                  <button
                    type="button"
                    className="ultron-btn ultron-btn--danger"
                    onClick={() => void run.approve('DENY')}
                  >
                    DENY
                  </button>
                </div>
              </div>
            )}
          </section>
        </div>

        <PhaseStrip phase={run.phase} failed={run.status === 'failed'} />

        {/* ── plan, steps, trace ──────────────────────────────────────────── */}
        <div className="ultron-columns">
          <div className="ultron-column">
            <PlanList plan={run.plan} dropped={run.dropped} steps={run.steps} />
            <StepTimeline steps={run.steps} />
          </div>

          <section className="ultron-panel">
            <header className="ultron-panel__head">
              <h2>LIVE TRACE</h2>
              <span className="ultron-mono-dim">
                {run.logs.length} event{run.logs.length === 1 ? '' : 's'} streamed
              </span>
            </header>
            <div className="ultron-trace sara-scroll" ref={traceRef}>
              {run.logs.length === 0 && (
                <p className="ultron-mono-dim">
                  No events yet. Submit an objective — every line here is emitted by
                  the backend while the run executes.
                </p>
              )}
              {run.logs.map((line, index) => (
                <div
                  key={`${index}-${line.slice(0, 12)}`}
                  className={`ultron-trace__line${
                    line.startsWith('[ULTRON]') ? ' ultron-trace__line--ultron' : ''
                  }`}
                >
                  {line}
                </div>
              ))}
              {run.summary && <div className="ultron-report">{run.summary}</div>}
            </div>
          </section>
        </div>

        {/* ── whole-fleet detail, memory, capabilities ───────────────────── */}
        <FleetPanel />
        <MemoryPanel />

        {caps && (
          <footer className="ultron-panel">
            <header className="ultron-panel__head">
              <h2>CAPABILITY SURFACE</h2>
              <span className="ultron-mono-dim">
                v{caps.version ?? '—'} · {caps.status ?? 'unknown'}
              </span>
            </header>
            <div className="ultron-columns">
              <div>
                <h3 className="ultron-subhead">
                  NATIVE TOOLS ({caps.native_tools?.length ?? 0})
                </h3>
                <p className="ultron-mono-dim">{caps.native_tools?.join(' · ') || '—'}</p>
                <h3 className="ultron-subhead" style={{ marginTop: 14 }}>
                  DELEGATION TARGETS ({caps.delegation?.agents?.length ?? 0})
                </h3>
                <p className="ultron-mono-dim">
                  {caps.delegation?.agents?.join(' · ') || '—'}
                </p>
              </div>
              <div>
                <h3 className="ultron-subhead">
                  APPROVAL-GATED ({caps.approval_gated_tools?.length ?? 0})
                </h3>
                <p className="ultron-mono-dim">
                  {caps.approval_gated_tools?.join(' · ') ||
                    'None — no tool in this build is gated.'}
                </p>
                <h3 className="ultron-subhead" style={{ marginTop: 14 }}>
                  VOICE
                </h3>
                <p className="ultron-mono-dim">
                  {voice
                    ? voice.microphone
                      ? `mic ready (${voice.model ?? 'whisper'})` +
                        (voice.speech_synth ? ' · synth ready' : ' · no synthesiser')
                      : voice.reason
                    : 'probe failed — see the VOICE CHANNEL plate'}
                </p>
                {caps.brahma?.source_dir && (
                  <p className="ultron-mono-dim">
                    Brahma-Echo source: <code>{caps.brahma.source_dir}</code> ·{' '}
                    {caps.brahma.available ?? 0} modules bridged
                    {caps.brahma.unavailable
                      ? `, ${caps.brahma.unavailable} unavailable`
                      : ''}
                  </p>
                )}
              </div>
            </div>
          </footer>
        )}
      </div>
    </main>
  );
}
