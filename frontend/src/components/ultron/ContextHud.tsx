'use client';

/**
 * The context HUD.
 *
 * Deliberately small. The instruction was "do not clutter the interface, the
 * hologram stays the centerpiece" - so this is a single quiet strip, not a
 * dashboard. Five readouts and three switches, and nothing animates unless the
 * thing it describes is genuinely happening.
 *
 *   CURRENT FOCUS   application + what is open in it
 *   CURRENT TASK    what Ultron was asked to do
 *   LAST ACTION     what happened, and whether it was VERIFIED
 *   VOICE           the always-on microphone state
 *   VISION          the screen-understanding state
 *
 * Every value comes from `GET /ultron/awareness`, which returns `blocked` when
 * screen access is off. That is rendered as "SCREEN ACCESS OFF" rather than as
 * the last known window, because showing stale context as if it were live is
 * the one thing this panel must never do.
 *
 * "LAST ACTION" renders three different ways on purpose - verified, failed, and
 * attempted-but-unverified - so a green tick can never sit next to something
 * that was never checked.
 */

import { useCallback, useEffect, useState } from 'react';
import { ULTRON_API_URL, saraFetch } from '@/lib/api';

const API = `${ULTRON_API_URL}/ultron`;

/** Fast enough that an alt-tab is visible, slow enough to be free. */
const POLL_MS = 2000;

interface ContextSnapshot {
  revision: number;
  t: number;
  age_s: number;
  context: {
    category: string;
    app_name: string;
    item: string | null;
    url: string | null;
    site: string | null;
    project: string | null;
    folder: string | null;
    confidence: number;
    unavailable: string[];
  };
  adapter: string;
  raw_title: string;
  task: string;
  last_action: string;
  last_action_ok: boolean | null;
  source: string;
}

interface AwarenessPayload {
  current: ContextSnapshot | null;
  previous: ContextSnapshot | null;
  changes: number;
  blocked: string | null;
  observer: { running: boolean; polls: number; changes: number; url_reads: number };
  vision: {
    tier: string;
    summary: string;
    changed: boolean;
    element_count: number;
    screenshot_taken: boolean;
  } | null;
  vision_stats: { calls: number; screenshots: number; screenshot_allowed: boolean };
  always_on: {
    state: string;
    running: boolean;
    permission: boolean;
    utterances: number;
    handled: number;
    timeouts: number;
    errors: number;
    consecutive_errors: number;
    barge_ins: number;
    last_error: string;
  };
  permissions: {
    gates: Record<string, { allowed: boolean; reason: string }>;
  };
}

export function useAwareness(nonce = 0): { data: AwarenessPayload | null; error: string } {
  const [data, setData] = useState<AwarenessPayload | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetch(`${API}/awareness`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = (await res.json()) as AwarenessPayload;
        if (!cancelled) {
          setData(body);
          setError('');
        }
      } catch (err: unknown) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
    // `nonce` is the manual-refresh handle: bumping it restarts the poll so a
    // permission change shows up immediately rather than at the next tick.
  }, [nonce]);

  return { data, error };
}

/** The always-on microphone switch. Separate from the one-shot SPEAK button. */
function MicSwitch({ data, onChanged }: { data: AwarenessPayload | null; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const granted = data?.permissions.gates.microphone?.allowed ?? false;
  const running = data?.always_on.running ?? false;

  const call = useCallback(
    async (path: string, body: unknown) => {
      setBusy(true);
      setNote('');
      try {
        const res = await saraFetch(`${API}${path}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const payload = await res.json().catch(() => null);
        if (!res.ok) {
          setNote(
            typeof payload?.detail === 'string'
              ? payload.detail
              : `Request failed (HTTP ${res.status}).`,
          );
          return;
        }
        // A refusal is a 200 with `started: false` and a reason - the server
        // saying "I will not turn the microphone on, and here is why". Treating
        // it as a plain success would leave the switch apparently inert.
        if (payload && (payload.started === false || payload.barge_in === false)) {
          setNote(payload.reason || 'The request was declined; see the backend log.');
        }
        onChanged();
      } catch {
        setNote('Could not reach the backend.');
      } finally {
        setBusy(false);
      }
    },
    [onChanged],
  );

  const label = running ? '● MIC ON' : granted ? 'MIC READY' : 'MIC OFF';

  return (
    <div style={{ display: 'grid', gap: 5, justifyItems: 'start' }}>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        <button
          type="button"
          className="ultron-btn ultron-btn--ghost"
          style={{
            padding: '5px 9px',
            fontSize: 9.5,
            opacity: busy ? 0.5 : 1,
            borderColor: running ? '#00f0ff' : undefined,
            color: running ? '#00f0ff' : undefined,
          }}
          disabled={busy || !granted}
          title={
            granted
              ? running
                ? 'Turn the microphone OFF'
                : 'Start the always-on loop. It stays on until you turn it off.'
              : 'Grant the microphone permission first.'
          }
          onClick={() =>
            void call(
              running ? '/voice/always-on/stop' : '/voice/always-on',
              running ? { reason: 'operator' } : {},
            )
          }
        >
          {label}
        </button>
        {!granted && (
          <button
            type="button"
            className="ultron-btn ultron-btn--ghost"
            style={{ padding: '5px 9px', fontSize: 9.5 }}
            disabled={busy}
            onClick={() =>
              void call('/permissions/microphone', {
                allowed: true,
                reason: 'enabled from the context HUD',
              })
            }
          >
            GRANT
          </button>
        )}
      </div>
      {note && (
        <span className="hud-tag" style={{ color: 'var(--sara-warn)', textTransform: 'none' }}>
          {note}
        </span>
      )}
    </div>
  );
}

function Readout({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div className="hud-tag">{label}</div>
      <div
        style={{
          fontFamily: 'var(--sara-font-mono)',
          fontSize: 11,
          color: tone ?? 'var(--sara-ink)',
          marginTop: 2,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
        title={value}
      >
        {value}
      </div>
    </div>
  );
}

export default function ContextHud() {
  const [nonce, setNonce] = useState(0);
  const refresh = useCallback(() => setNonce((n) => n + 1), []);
  const { data, error } = useAwareness(nonce);

  const current = data?.current ?? null;
  const screenOn = data?.permissions.gates.screen?.allowed ?? false;
  const controlOn = data?.permissions.gates.control?.allowed ?? false;
  const vision = data?.vision ?? null;
  const alwaysOn = data?.always_on;

  // Three very different truth states, rendered three different ways.
  const lastAction = current?.last_action
    ? current.last_action_ok === true
      ? `${current.last_action} ✓`
      : current.last_action_ok === false
        ? `${current.last_action} ✗`
        : `${current.last_action} (unverified)`
    : '—';
  const lastActionTone =
    current?.last_action_ok === true
      ? '#22d3a5'
      : current?.last_action_ok === false
        ? '#ff2d6f'
        : current?.last_action
          ? '#ffaa00'
          : undefined;

  const focusLine = !screenOn
    ? 'SCREEN ACCESS OFF'
    : data?.blocked
      ? `BLOCKED — ${data.blocked}`
      : current
        ? [current.context.app_name, current.context.item ?? current.context.site]
            .filter(Boolean)
            .join(' · ') || current.raw_title
        : 'NO WINDOW OBSERVED';

  const visionLine = !screenOn
    ? 'OFF'
    : vision
      ? `${vision.tier.toUpperCase()} · ${vision.element_count} controls`
      : 'IDLE';

  return (
    <section className="hud-plate" aria-label="Context awareness">
      <header className="hud-plate__head">
        <h2>CONTEXT</h2>
        <span className="hud-tag">
          {data
            ? `${data.observer.running ? 'WATCHING' : 'PAUSED'} · ${data.changes} CHANGES` +
              (data.observer.url_reads ? ` · ${data.observer.url_reads} URLS` : '')
            : 'PROBING…'}
        </span>
      </header>

      <div className="hud-plate__body">
        {error && <p className="ultron-error">Awareness unavailable: {error}</p>}

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
            gap: 12,
          }}
        >
          <Readout
            label="CURRENT FOCUS"
            value={focusLine}
            tone={screenOn && !data?.blocked ? '#00f0ff' : '#ffaa00'}
          />
          <Readout label="CURRENT TASK" value={current?.task || '—'} />
          <Readout label="LAST ACTION" value={lastAction} tone={lastActionTone} />
          <Readout
            label="VOICE"
            value={
              !alwaysOn
                ? '—'
                : alwaysOn.running
                  ? `● LISTENING · ${alwaysOn.utterances} heard`
                  : // A loop that stopped on repeated errors is not "ready" and
                    // must never render as though it simply went quiet: the user
                    // needs to know the microphone is dead and why.
                    alwaysOn.state === 'error'
                      ? `ERROR · ${alwaysOn.last_error.slice(0, 40) || 'no microphone'}`
                      : alwaysOn.permission
                        ? `READY · ${alwaysOn.timeouts} quiet cycles`
                        : 'OFF — PERMISSION NOT GRANTED'
            }
            tone={
              alwaysOn?.state === 'error'
                ? '#ff2d6f'
                : alwaysOn?.running
                  ? '#22d3a5'
                  : alwaysOn?.permission
                    ? '#8ce9ff'
                    : '#ffaa00'
            }
          />
          <Readout
            label="VISION"
            value={visionLine}
            tone={vision?.screenshot_taken ? '#ffaa00' : screenOn ? '#00f0ff' : '#ffaa00'}
          />
        </div>

        {/* One line of detail, only when there is something to say. */}
        {current && current.context.unavailable.length > 0 && screenOn && (
          <p
            className="hud-tag"
            style={{ margin: '10px 0 0', textTransform: 'none' }}
          >
            Not visible for this window: {current.context.unavailable.join(', ')}
            {current.context.confidence < 0.5 ? ' · low confidence' : ''}
          </p>
        )}
        {vision?.summary && (
          <p className="hud-tag" style={{ margin: '6px 0 0', textTransform: 'none' }}>
            {vision.summary}
          </p>
        )}

        {/* The three independent switches. Each is usable on its own. */}
        <div
          style={{
            display: 'flex',
            gap: 14,
            flexWrap: 'wrap',
            alignItems: 'center',
            marginTop: 12,
            paddingTop: 10,
            borderTop: '1px solid rgba(0,240,255,0.12)',
          }}
        >
          <MicSwitch data={data} onChanged={refresh} />
          <Switch
            label="SCREEN / CONTEXT"
            on={screenOn}
            busy={false}
            onToggle={(allowed) =>
              void saraFetch(`${API}/permissions/screen`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ allowed, reason: 'context HUD' }),
              }).then(refresh)
            }
          />
          <Switch
            label="DESKTOP CONTROL"
            on={controlOn}
            busy={false}
            onToggle={(allowed) =>
              void saraFetch(`${API}/permissions/control`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ allowed, reason: 'context HUD' }),
              }).then(refresh)
            }
          />
          {alwaysOn?.barge_ins ? (
            <span className="hud-tag">{alwaysOn.barge_ins} barge-in(s)</span>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function Switch({
  label,
  on,
  busy,
  onToggle,
}: {
  label: string;
  on: boolean;
  busy: boolean;
  onToggle: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      className="ultron-btn ultron-btn--ghost"
      aria-pressed={on}
      style={{
        padding: '5px 9px',
        fontSize: 9.5,
        opacity: busy ? 0.5 : 1,
        borderColor: on ? '#22d3a5' : undefined,
        color: on ? '#22d3a5' : undefined,
      }}
      disabled={busy}
      onClick={() => onToggle(!on)}
      title={on ? 'Revoke this capability' : 'Grant this capability'}
    >
      {on ? '●' : '○'} {label}
    </button>
  );
}
