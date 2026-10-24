'use client';

/**
 * Agent Control Center.
 *
 * Every status shown here came from a real HTTP probe issued by the backend
 * (``GET /ultron/agents``) against that agent's own health route. There is no
 * optimistic default and no timer-driven "online": an agent that did not
 * answer is rendered as unreachable, with the HTTP status and the latency that
 * were actually observed, and an agent with no health route says so.
 *
 * Controls are limited to what the backend genuinely supports:
 *   - INSPECT re-probes the fleet for real (the only write-free operation).
 *   - OPEN navigates to that agent's own page, so its real controls are used
 *     in its own context rather than reimplemented here against a guessed API.
 * SARA has no uniform per-agent start/stop/restart endpoint, so none is
 * offered; inventing one would be the exact fake control this avoids.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ULTRON_API_URL } from '@/lib/api';
import './console.css';

const API = `${ULTRON_API_URL}/ultron`;

/** Refresh cadence while the panel is visible. Cheap: the backend probes in
 *  parallel with a 2 s ceiling, and polling pauses with the tab. */
const REFRESH_MS = 20000;

export interface FleetAgent {
  id: string;
  name: string;
  category?: string | null;
  route?: string | null;
  probe?: string | null;
  status: 'online' | 'degraded' | 'unreachable' | 'no_probe';
  http_status?: number | null;
  latency_ms?: number | null;
  detail?: string;
  live?: {
    active_runs: number;
    total_sessions: number;
    current: {
      task_id?: string;
      goal: string;
      status?: string;
      phase?: string;
      current_tool?: string;
      steps_done: number;
      plan_steps: number;
    } | null;
  };
}

export interface FleetSummary {
  total: number;
  probed: number;
  online: number;
  degraded: number;
  unreachable: number;
  no_probe: number;
  checked_at: number;
  base_url: string;
}

interface FleetResponse {
  agents: FleetAgent[];
  unprobed_agents?: FleetAgent[];
  summary: FleetSummary;
}

const STATUS_STYLE: Record<string, { color: string; label: string }> = {
  online: { color: '#22d3a5', label: 'ONLINE' },
  degraded: { color: '#ffaa00', label: 'DEGRADED' },
  unreachable: { color: '#ff5470', label: 'UNREACHABLE' },
  no_probe: { color: '#5b6b7f', label: 'NO PROBE' },
};

export default function FleetPanel() {
  const [fleet, setFleet] = useState<FleetResponse | null>(null);
  const [error, setError] = useState('');
  const [checking, setChecking] = useState(false);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`${API}/agents`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as FleetResponse;
      if (!mounted.current) return;
      setFleet(data);
      setError('');
    } catch (err: unknown) {
      if (!mounted.current) return;
      setError(
        `Fleet probe failed: ${err instanceof Error ? err.message : String(err)}. ` +
          'Statuses below are from the last successful probe.',
      );
    }
  }, []);

  /** INSPECT is the operator asking, so the busy state belongs to the click. */
  const inspect = useCallback(async () => {
    setChecking(true);
    try {
      await refresh();
    } finally {
      if (mounted.current) setChecking(false);
    }
  }, [refresh]);

  useEffect(() => {
    mounted.current = true;
    const kickoff = async () => {
      await refresh();
    };
    void kickoff();
    const timer = window.setInterval(() => {
      // Do not spend probes on a tab nobody is looking at.
      if (!document.hidden) void refresh();
    }, REFRESH_MS);
    return () => {
      mounted.current = false;
      window.clearInterval(timer);
    };
  }, [refresh]);

  const summary = fleet?.summary;
  const agents = [...(fleet?.agents ?? []), ...(fleet?.unprobed_agents ?? [])];

  return (
    <section className="ultron-panel">
      <header className="ultron-panel__head">
        <h2>AGENT CONTROL CENTER</h2>
        <div className="ultron-panel__tools">
          {summary && (
            <span className="ultron-mono-dim">
              {summary.online} online · {summary.degraded} degraded ·{' '}
              {summary.unreachable} unreachable
              {summary.no_probe > 0 ? ` · ${summary.no_probe} no probe` : ''}
            </span>
          )}
          <button
            type="button"
            className="ultron-btn ultron-btn--ghost"
            onClick={() => void inspect()}
            disabled={checking}
          >
            {checking ? 'PROBING…' : 'INSPECT'}
          </button>
        </div>
      </header>

      {error && <p className="ultron-warn">{error}</p>}
      {!fleet && !error && <p className="ultron-mono-dim">Probing fleet…</p>}

      <div className="ultron-fleet">
        {agents.map((agent) => {
          const style = STATUS_STYLE[agent.status] ?? STATUS_STYLE.no_probe;
          const current = agent.live?.current;
          return (
            <article key={agent.id} className="ultron-fleet__row">
              <span
                className="ultron-fleet__dot"
                style={{ background: style.color, boxShadow: `0 0 8px ${style.color}` }}
                aria-hidden
              />
              <div className="ultron-fleet__body">
                <div className="ultron-fleet__title">
                  <strong>{agent.name}</strong>
                  <span className="ultron-mono-dim">{agent.category}</span>
                  <span style={{ color: style.color }} className="ultron-fleet__status">
                    {style.label}
                  </span>
                </div>
                <div className="ultron-mono-dim ultron-fleet__meta">
                  {agent.probe ? (
                    <>
                      <code>{agent.probe}</code>
                      {agent.http_status != null && <> · HTTP {agent.http_status}</>}
                      {agent.latency_ms != null && <> · {agent.latency_ms} ms</>}
                    </>
                  ) : (
                    <span>no health route</span>
                  )}
                  {current && (
                    <>
                      {' '}
                      · <strong style={{ color: '#7dd3fc' }}>{current.phase}</strong>
                      {current.current_tool && <> → {current.current_tool}</>}
                    </>
                  )}
                </div>
                {agent.detail && (
                  <div className="ultron-fleet__detail">{agent.detail}</div>
                )}
              </div>
              <div className="ultron-fleet__actions">
                {agent.route && (
                  <Link className="ultron-btn ultron-btn--ghost" href={agent.route}>
                    OPEN
                  </Link>
                )}
              </div>
            </article>
          );
        })}
      </div>

      {summary && (
        <p className="ultron-mono-dim ultron-footnote">
          Probed in-process against {summary.base_url} ·{' '}
          {new Date(summary.checked_at * 1000).toLocaleTimeString()} · SARA exposes no
          uniform per-agent start/stop, so this panel reports and inspects only.
        </p>
      )}
    </section>
  );
}
