'use client';

/**
 * Voice rail - the state machine the hologram is bound to.
 *
 * Renders `GET /ultron/voice/state` and nothing else. The backend derives the
 * state from the live voice stack and the live run phases; this component never
 * decides a state itself, which is what stops the rail from claiming the
 * assistant is LISTENING when nothing is recording.
 *
 * The waveform ring is driven by the returned state, not by a synthesised audio
 * signal - there is no analyser node here and none is faked. When Ultron is
 * speaking, the bars ride a real elapsed-time sine; when the echo guard is
 * counting down, the ring visibly drains. Both are honest descriptions of
 * something the server reported.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { ULTRON_API_URL } from '@/lib/api';
import MicButton from './MicButton';

const API = `${ULTRON_API_URL}/ultron`;

/** Poll interval. Fast enough that speaking is visible, slow enough to be free. */
const POLL_MS = 1500;

export type VoiceStateName =
  | 'IDLE'
  | 'LISTENING'
  | 'THINKING'
  | 'EXECUTING'
  | 'VERIFYING'
  | 'SPEAKING'
  | 'ERROR'
  | 'PERMISSION_REQUIRED'
  | 'UNAVAILABLE';

/** The rail's own colour language, keyed by state. */
const STATE_COLOR: Record<VoiceStateName, string> = {
  IDLE: '#7d8fa3',
  LISTENING: '#00f0ff',
  THINKING: '#a78bfa',
  EXECUTING: '#ffaa00',
  VERIFYING: '#22d3a5',
  SPEAKING: '#22d3a5',
  ERROR: '#ff2d6f',
  PERMISSION_REQUIRED: '#ffaa00',
  UNAVAILABLE: '#ff2d6f',
};

/** Order the rail renders, so the machine always reads left-to-right. */
const STATE_ORDER: VoiceStateName[] = [
  'IDLE',
  'LISTENING',
  'THINKING',
  'EXECUTING',
  'VERIFYING',
  'SPEAKING',
  'ERROR',
];

interface VoiceState {
  state: VoiceStateName;
  reason: string;
  active_runs: number;
  phases: string[];
  dominant_phase: string | null;
  listening: boolean;
  speaking: boolean;
  microphone: boolean;
  transcriber: boolean;
  speech_synth: boolean;
  model: string | null;
  echo_guard_s: number;
  problems: string[];
  continuous: boolean;
  continuous_detail: string;
  states?: string[];
  inputs?: { last_error?: { message?: string } | null };
}

export interface VoiceRailProps {
  /** The backend's authoritative state; the rail renders this verbatim. */
  state: VoiceStateName;
  reason: string;
  echoGuardS: number;
  activeRuns: number;
  microphone: boolean;
  transcriber: boolean;
  speechSynth: boolean;
  model: string | null;
  continuous: boolean;
  continuousDetail: string;
  problems: string[];
  onListen: () => void;
  listening: boolean;
  disabled: boolean;
  disabledReason: string;
  /** The real browser microphone: capture, VAD, upload, transcript. */
  onTranscript: (text: string) => void;
  /** Whether the backend host still has a usable microphone of its own. */
  serverMicAvailable: boolean;
  serverMicDetail: string;
}

/** Twelve bars is enough to read as a waveform at 74px without turning to mush. */
const BAR_COUNT = 12;

export default function VoiceRail(props: VoiceRailProps) {
  const color = STATE_COLOR[props.state] ?? STATE_COLOR.IDLE;

  // Real elapsed time, only advanced while a frame is actually rendered and the
  // state says something is happening. No setInterval, so an idle command
  // center does no work at all between polls.
  const [pulse, setPulse] = useState(0);
  const animating = props.state === 'SPEAKING' || props.state === 'LISTENING';

  useEffect(() => {
    if (!animating) return undefined;
    let frame = 0;
    let cancelled = false;
    let previous = performance.now();
    const step = (now: number) => {
      if (cancelled) return;
      setPulse((now - previous) / 1000);
      previous = now;
      frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => {
      cancelled = true;
      cancelAnimationFrame(frame);
    };
  }, [animating]);

  const bars = useMemo(
    () =>
      Array.from({ length: BAR_COUNT }, (_, i) => {
        if (!animating) return 6 + ((i * 7) % 6);
        if (props.state === 'SPEAKING') {
          // A real envelope of the last reply would need audio; this is an
          // explicitly synthetic "utterance in progress" pulse, and it only
          // runs while the server says the synthesiser is playing.
          const h = 0.5 + 0.5 * Math.sin(pulse * 5.2 + i * 0.55);
          return 8 + h * 46;
        }
        return 10 + (0.5 + 0.5 * Math.sin(pulse * 3.1 + i * 0.7)) * 40;
      }),
    [animating, pulse, props.state],
  );

  return (
    <section
      className="hud-plate"
      style={{ ['--state-color' as string]: color }}
      aria-label="Voice state"
    >
      <header className="hud-plate__head">
        <h2>VOICE CHANNEL</h2>
        <span className="hud-tag">
          {/* Two different microphones, deliberately labelled as such: the
              browser's (always available over https/localhost) and the backend
              host's own (absent on a cloud host). Conflating them is how a
              "MIC READY" badge ends up on a page where the mic cannot work. */}
          BROWSER MIC
          {props.transcriber ? ' · STT' : ''}
          {props.speechSynth ? ' · TTS' : ''}
          {props.microphone ? ' · HOST MIC READY' : ' · NO HOST MIC'}
        </span>
      </header>

      <div className="hud-plate__body">
        <div className="hud-voice">
          <div style={{ minWidth: 0 }}>
            <div
              style={{
                fontFamily: 'var(--sara-font-mono)',
                fontSize: 17,
                letterSpacing: '0.16em',
                color,
                textShadow: `0 0 14px ${color}`,
              }}
            >
              {props.state}
            </div>
            <p
              className="hud-tag"
              style={{
                margin: '5px 0 0',
                letterSpacing: '0.06em',
                textTransform: 'none',
                color: 'var(--sara-muted)',
                fontSize: 10.5,
                lineHeight: 1.6,
              }}
            >
              {props.reason}
            </p>

            <ul className="hud-voice__states" style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {STATE_ORDER.map((name) => (
                <li
                  key={name}
                  className={`hud-voice__state${
                    name === props.state ? ' hud-voice__state--active' : ''
                  }`}
                  style={
                    {
                      '--state-color': STATE_COLOR[name],
                    } as React.CSSProperties
                  }
                >
                  {name}
                </li>
              ))}
            </ul>
          </div>

          {/* One right-hand column: the real microphone button (which carries
              its own "use server mic" fallback when the browser refuses
              permission), then the state ring. */}
          <div style={{ display: 'grid', gap: 10, justifyItems: 'center' }}>
            <MicButton
              onTranscript={props.onTranscript}
              onFallback={props.onListen}
              fallbackAvailable={props.serverMicAvailable}
              fallbackReason={props.serverMicDetail}
              disabled={props.disabled || props.listening}
              color={color}
            />
            <svg
              className="hud-voice__meter"
              viewBox="0 0 100 100"
              aria-hidden="true"
            >
              <circle
                cx="50"
                cy="50"
                r="44"
                fill="none"
                stroke={color}
                strokeOpacity="0.22"
                strokeWidth="1.5"
              />
              {/* The echo guard is a real countdown the server reports, so it
                  drains visibly instead of being a decorative arc. */}
              <circle
                cx="50"
                cy="50"
                r="44"
                fill="none"
                stroke={color}
                strokeWidth="3"
                strokeLinecap="round"
                strokeDasharray={`${2 * Math.PI * 44}`}
                strokeDashoffset={`${2 * Math.PI * 44 * (1 - Math.min(1, props.echoGuardS / 8))}`}
                transform="rotate(-90 50 50)"
                opacity={props.echoGuardS > 0 ? 0.9 : 0}
              />
              <g>
                {bars.map((height, i) => (
                  <rect
                    key={i}
                    x={44 + i * 4}
                    y={50 - height / 2}
                    width="2.4"
                    height={height}
                    rx="1.2"
                    fill={color}
                    opacity={0.55 + 0.45 * (i / BAR_COUNT)}
                  />
                ))}
              </g>
            </svg>
          </div>
        </div>

        <div
          style={{
            display: 'flex',
            gap: 14,
            flexWrap: 'wrap',
            marginTop: 10,
            paddingTop: 10,
            borderTop: '1px solid rgba(0,240,255,0.12)',
          }}
        >
          <Readout
            label="ECHO GUARD"
            value={props.echoGuardS > 0 ? `${props.echoGuardS.toFixed(1)}s` : 'CLEAR'}
            tone={props.echoGuardS > 0 ? '#ffaa00' : undefined}
          />
          <Readout label="ACTIVE RUNS" value={String(props.activeRuns)} />
          <Readout label="WHISPER" value={props.model ?? '—'} />
          <Readout
            label="HANDS-FREE"
            value={props.continuous ? 'ACTIVE' : 'NOT IMPLEMENTED'}
            tone={props.continuous ? '#22d3a5' : '#ffaa00'}
          />
        </div>

        {!props.continuous && (
          <p className="hud-tag" style={{ margin: '8px 0 0', textTransform: 'none' }}>
            {props.continuousDetail}
          </p>
        )}
        {props.problems.length > 0 && (
          <p className="ultron-warn" style={{ margin: '8px 0 0' }}>
            {props.problems.join(' ')}
          </p>
        )}
      </div>
    </section>
  );
}

function Readout({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: string;
}) {
  return (
    <div>
      <div className="hud-tag">{label}</div>
      <div
        style={{
          fontFamily: 'var(--sara-font-mono)',
          fontSize: 12,
          color: tone ?? 'var(--sara-ink)',
          marginTop: 2,
        }}
      >
        {value}
      </div>
    </div>
  );
}

/**
 * Polls the derived voice state.
 *
 * Kept next to the rail so the page does not have to own a second timer. The
 * poll is a plain interval that is torn down on unmount; it stops rather than
 * throwing when the backend is restarting, and the rail shows UNAVAILABLE,
 * which is what the server would have said anyway.
 */
export function useVoiceState(): {
  data: VoiceState | null;
  error: string;
  refresh: () => void;
} {
  const [data, setData] = useState<VoiceState | null>(null);
  const [error, setError] = useState('');
  const [nonce, setNonce] = useState(0);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetch(`${API}/voice/state`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = (await res.json()) as VoiceState;
        if (!cancelled) {
          setData(body);
          setError('');
        }
      } catch (err: unknown) {
        if (cancelled) return;
        setError(
          `Voice state unavailable: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [nonce]);

  return { data, error, refresh };
}
