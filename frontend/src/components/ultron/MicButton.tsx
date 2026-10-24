'use client';

/**
 * The microphone button.
 *
 * This is a real microphone, not a decorative control:
 *
 *   click  -> navigator.mediaDevices.getUserMedia({audio})
 *          -> AudioContext + ScriptProcessor capture
 *          -> live RMS level drives the ring and the bar
 *          -> voice activity detection stops the capture after a pause
 *          -> the samples are encoded to 16 kHz mono 16-bit WAV (lib/wav.ts)
 *          -> POST /ultron/voice/transcribe (raw WAV body)
 *          -> the backend runs the same local Whisper pass as the desktop path
 *
 * Why the browser records instead of the server: the server-side
 * ``/voice/listen`` opens the *backend host's* microphone. That works on a
 * laptop and can never work on a cloud host - Render has no audio input
 * device. The user's microphone is in their browser, so that is where capture
 * happens; only the transcription (which genuinely needs a model) stays on the
 * backend.
 *
 * Failure is handled, never hidden:
 *
 *   no getUserMedia / insecure context -> falls back to the server-side mic,
 *       and says which path is in use;
 *   permission denied                  -> an actionable message, button re-enabled;
 *   device busy                         -> the same;
 *   silence for the whole capture       -> "no speech detected", no upload.
 *
 * The level meter is a genuine RMS of the captured samples, and the silence
 * detection uses the same RMS, so the ring is a measurement rather than an
 * animation.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ULTRON_API_URL, saraFetch } from '@/lib/api';
import { rms, toWhisperWav } from '@/lib/wav';

const API = `${ULTRON_API_URL}/ultron`;

/** Never capture longer than this, whatever else happens. */
const MAX_CAPTURE_S = 20;

/**
 * How long to wait for `getUserMedia` before giving up.
 *
 * The promise does not always settle: a permission prompt the user simply
 * never answers, or a browser with no audio device at all, can leave it pending
 * indefinitely - observed in a headless preview, where the button sat on
 * "ASKING..." forever. A microphone button that cannot be escaped is a dead
 * control, so the wait is bounded and the failure is stated plainly.
 */
const PERMISSION_TIMEOUT_MS = 12_000;
/** Stop this long after speech stops, once speech has actually started. */
const SILENCE_STOP_MS = 1400;
/** Ignore silence for this long at the start, so the button does not fire on a click. */
const SPEECH_ONSET_MS = 700;
/** RMS below this is room noise. Calibrated from the first ~300 ms of capture. */
const NOISE_FLOOR_RATIO = 3.2;

/**
 * The last AudioContext sample rate, cached because the context is torn down
 * before the samples are encoded and `sampleRate` cannot be read afterwards.
 * The browser usually gives 44.1 or 48 kHz; both resample to 16 kHz.
 */
let lastSampleRate = 44_100;

type Phase = 'idle' | 'requesting' | 'recording' | 'uploading' | 'error';

export interface MicButtonProps {
  onTranscript: (text: string) => void;
  /** Use the backend host's microphone instead. Only offered as a fallback. */
  onFallback: () => void;
  fallbackAvailable: boolean;
  fallbackReason: string;
  disabled?: boolean;
  color: string;
}

export default function MicButton({
  onTranscript,
  onFallback,
  fallbackAvailable,
  fallbackReason,
  disabled,
  color,
}: MicButtonProps) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState('');
  const [level, setLevel] = useState(0);
  const [seconds, setSeconds] = useState(0);

  const streamRef = useRef<MediaStream | null>(null);
  const contextRef = useRef<AudioContext | null>(null);
  const chunksRef = useRef<Float32Array[]>([]);
  const noiseRef = useRef<number[]>([]);
  const startedAtRef = useRef(0);
  const speechAtRef = useRef<number | null>(null);
  const silentSinceRef = useRef<number | null>(null);
  const stopRef = useRef<(() => void) | null>(null);
  const guardRef = useRef<number | null>(null);

  const release = useCallback(() => {
    stopRef.current = null;
    // The wall-clock cap is cleared here rather than only in finish(), because
    // release() also runs on unmount.
    if (guardRef.current !== null) {
      window.clearTimeout(guardRef.current);
      guardRef.current = null;
    }
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    void contextRef.current?.close().catch(() => undefined);
    contextRef.current = null;
  }, []);

  // A component unmount mid-capture would otherwise leave the mic indicator on.
  useEffect(() => release, [release]);

  const finish = useCallback(
    async (auto: boolean) => {
      const chunks = chunksRef.current;
      chunksRef.current = [];
      release();

      if (chunks.length === 0) {
        setPhase('idle');
        setError(auto ? 'No speech detected. Try again, a little closer to the mic.' : '');
        return;
      }

      // Flatten the capture into one buffer.
      let total = 0;
      for (const chunk of chunks) total += chunk.length;
      const samples = new Float32Array(total);
      let offset = 0;
      for (const chunk of chunks) {
        samples.set(chunk, offset);
        offset += chunk.length;
      }

      setPhase('uploading');
      setLevel(0);
      setSeconds(0);
      setError('');

      try {
        const wav = toWhisperWav(samples, lastSampleRate);
        const res = await saraFetch(`${API}/voice/transcribe`, {
          method: 'POST',
          headers: { 'Content-Type': 'audio/wav' },
          body: wav,
        });
        const body = await res.json().catch(() => null);
        if (!res.ok) {
          const detail = body?.detail;
          setPhase('error');
          setError(
            typeof detail === 'string'
              ? detail
              : typeof detail?.error === 'string'
                ? detail.error
                : `Transcription failed (HTTP ${res.status}).`,
          );
          return;
        }
        const text = String(body?.text ?? '').trim();
        if (!text) {
          setPhase('idle');
          setError('No speech was recognised in that clip.');
          return;
        }
        setPhase('idle');
        onTranscript(text);
      } catch {
        setPhase('error');
        setError('Could not reach the transcription endpoint.');
      }
    },
    [onTranscript, release],
  );

  const start = useCallback(async () => {
    if (phase === 'recording' || phase === 'requesting' || phase === 'uploading') return;
    setError('');

    const media = navigator.mediaDevices;
    if (!media?.getUserMedia) {
      setPhase('error');
      setError(
        'This browser will not expose a microphone to a page over an insecure ' +
          'connection. Use https:// or localhost, or use the server-side mic.',
      );
      return;
    }

    setPhase('requesting');
    let stream: MediaStream;
    try {
      stream = await withTimeout(
        media.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        }),
        PERMISSION_TIMEOUT_MS,
        'The microphone request did not complete. If a permission prompt is ' +
          'waiting, answer it - or use the server mic below.',
      );
    } catch (err: unknown) {
      setPhase('error');
      setError(describeMicError(err));
      return;
    }

    const context = new AudioContext();
    if (context.state === 'suspended') await context.resume();
    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(4096, 1, 1);

    streamRef.current = stream;
    contextRef.current = context;
    chunksRef.current = [];
    noiseRef.current = [];
    speechAtRef.current = null;
    silentSinceRef.current = null;
    startedAtRef.current = performance.now();
    lastSampleRate = context.sampleRate;
    setSeconds(0);
    setPhase('recording');

    processor.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      chunksRef.current.push(new Float32Array(input));

      const amplitude = rms(input);
      setLevel(Math.min(1, amplitude * 6));

      const now = performance.now();
      const elapsed = now - startedAtRef.current;
      setSeconds(Math.min(MAX_CAPTURE_S, elapsed / 1000));

      // Calibrate the room for the first ~300 ms, so a noisy desk does not read
      // as continuous speech.
      if (elapsed < 300) {
        noiseRef.current.push(amplitude);
        return;
      }
      const noise =
        noiseRef.current.reduce((a, b) => a + b, 0) / Math.max(1, noiseRef.current.length);
      const threshold = Math.max(0.012, noise * NOISE_FLOOR_RATIO);

      if (amplitude > threshold) {
        if (speechAtRef.current === null && elapsed > SPEECH_ONSET_MS) {
          speechAtRef.current = now;
        }
        silentSinceRef.current = null;
      } else if (speechAtRef.current !== null) {
        if (silentSinceRef.current === null) silentSinceRef.current = now;
        else if (now - silentSinceRef.current > SILENCE_STOP_MS) {
          stopRef.current?.();
          void finish(true);
        }
      }

      if (elapsed > MAX_CAPTURE_S * 1000) {
        stopRef.current?.();
        void finish(true);
      }
    };

    // Hard wall-clock cap, armed BEFORE the audio graph is built.
    //
    // Two reasons it cannot live inside the audio callback, both learned by
    // watching this button hang: (1) a device that is present but never
    // delivers a buffer - muted, virtual, or a headless browser's null source -
    // means `onaudioprocess` never fires at all; and (2) anything that throws
    // while connecting the graph skips a callback-scoped timer entirely. A
    // microphone control that can hang forever is worse than one that gives up.
    guardRef.current = window.setTimeout(() => {
      guardRef.current = null;
      stopRef.current?.();
      void finish(true);
    }, MAX_CAPTURE_S * 1000);

    // A ScriptProcessor only runs while it is connected. Routing it to the
    // destination would echo the user's own voice back at them through the
    // speakers, so it goes to a zero-gain node instead.
    let silent: GainNode | null = null;
    try {
      silent = context.createGain();
      silent.gain.value = 0;
      source.connect(processor);
      processor.connect(silent);
      silent.connect(context.destination);
    } catch (err: unknown) {
      // Without this the button stays "recording" forever with no error and no
      // way out, because the throw skips everything after it.
      release();
      setPhase('error');
      setError(
        `Could not start the audio graph: ${
          err instanceof Error ? err.message : String(err)
        }`,
      );
      return;
    }

    const gainNode = silent;
    stopRef.current = () => {
      processor.onaudioprocess = null;
      try {
        source.disconnect();
        processor.disconnect();
        gainNode.disconnect();
      } catch {
        // already torn down
      }
    };
  }, [finish, phase, release]);

  const cancel = useCallback(() => {
    stopRef.current?.();
    chunksRef.current = [];
    release();
    setPhase('idle');
    setLevel(0);
    setSeconds(0);
    setError('Capture cancelled.');
  }, [release]);

  const recording = phase === 'recording' || phase === 'requesting';
  const busy = recording || phase === 'uploading';
  const label =
    phase === 'requesting'
      ? 'ASKING…'
      : phase === 'uploading'
        ? 'TRANSCRIBING…'
        : recording
          ? `LISTENING ${seconds.toFixed(1)}s`
          : '🎤 SPEAK';

  return (
    <div style={{ display: 'grid', gap: 8, justifyItems: 'center' }}>
      <button
        type="button"
        onClick={recording ? cancel : () => void start()}
        disabled={disabled || phase === 'uploading'}
        title={
          disabled
            ? 'Unavailable'
            : recording
              ? 'Click to cancel this capture'
              : 'Record an objective with your microphone'
        }
        aria-pressed={recording}
        style={{
          display: 'grid',
          placeItems: 'center',
          width: 74,
          height: 74,
          borderRadius: '50%',
          cursor: disabled || phase === 'uploading' ? 'not-allowed' : 'pointer',
          border: `1px solid ${recording ? color : 'rgba(0,240,255,0.35)'}`,
          background: recording
            ? `color-mix(in srgb, ${color} 22%, rgba(3,8,15,0.9))`
            : 'rgba(3,8,15,0.9)',
          color: recording ? color : '#8ce9ff',
          boxShadow: recording
            ? `0 0 ${14 + level * 26}px ${color}, inset 0 0 ${8 + level * 14}px ${color}`
            : 'none',
          transition: 'box-shadow 90ms linear, background 160ms var(--sara-ease)',
          fontFamily: 'var(--sara-font-mono)',
          fontSize: 10,
          letterSpacing: '0.14em',
          textAlign: 'center',
          lineHeight: 1.4,
          padding: 4,
        }}
      >
        <span aria-hidden="true" style={{ fontSize: 17 }}>
          {recording ? '◉' : '🎤'}
        </span>
      </button>

      <div
        style={{
          fontFamily: 'var(--sara-font-mono)',
          fontSize: 9,
          letterSpacing: '0.14em',
          color: recording ? color : 'var(--sara-faint)',
          textTransform: 'uppercase',
        }}
      >
        {label}
      </div>

      {error && (
        <p
          className="hud-tag"
          style={{ margin: 0, textTransform: 'none', color: 'var(--sara-warn)', maxWidth: 260, textAlign: 'center' }}
        >
          {error}
        </p>
      )}

      {fallbackAvailable && (
        <button
          type="button"
          className="ultron-btn ultron-btn--ghost"
          style={{ padding: '4px 9px', fontSize: 9.5 }}
          onClick={onFallback}
          disabled={busy}
          title={fallbackReason}
        >
          USE SERVER MIC
        </button>
      )}
    </div>
  );
}

/** The AudioContext sample rate is not readable after teardown, so it is cached. */
/**
 * Reject if `promise` has not settled within `ms`.
 *
 * The timer is always cleared, including on success, so a fast permission grant
 * does not leave a pending timer holding the event loop open.
 */
function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      const error = new Error(message);
      error.name = 'TimeoutError';
      reject(error);
    }, ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err: unknown) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

function describeMicError(err: unknown): string {
  const name = (err as { name?: string })?.name ?? '';
  if (name === 'TimeoutError') {
    return (
      (err as { message?: string })?.message ??
      'The microphone request did not complete in time.'
    );
  }
  switch (name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
      return 'Microphone permission was denied. Allow it in the browser’s site settings, then try again.';
    case 'NotFoundError':
    case 'DevicesNotFoundError':
      return 'No microphone was found on this machine.';
    case 'NotReadableError':
    case 'TrackStartError':
      return 'The microphone is in use by another application. Close it and try again.';
    case 'SecurityError':
      return 'The browser blocked microphone access. Use https:// or localhost.';
    default:
      return `Could not open the microphone: ${err instanceof Error ? err.message : String(err)}`;
  }
}
