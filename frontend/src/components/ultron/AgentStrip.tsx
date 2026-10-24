'use client';

/**
 * Agent strip - the whole fleet, as it actually answers.
 *
 * Each chip is one entry from `GET /ultron/agents`, whose status comes from a
 * real HTTP health probe run by the backend (see core/fleet.py). There is no
 * client-side guess here and no optimistic "connecting" state: a chip is grey
 * with `NO PROBE` when SARA exposes no liveness route for that agent, which is
 * the honest answer and not a failure of this component.
 */

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { ULTRON_API_URL } from '@/lib/api';

const API = `${ULTRON_API_URL}/ultron`;

/** Fleet probes are 15 HTTP calls server-side; poll slowly enough to be cheap. */
const POLL_MS = 5000;

interface AgentEntry {
  id: string;
  name: string;
  category?: string;
  route?: string | null;
  probe?: string | null;
  status: 'online' | 'degraded' | 'unreachable' | 'no_probe';
  http_status?: number | null;
  latency_ms?: number | null;
  detail: string;
  live?: { active_runs: number; total_sessions: number };
}

interface FleetPayload {
  agents: AgentEntry[];
  summary?: {
    total: number;
    online: number;
    degraded: number;
    unreachable: number;
    no_probe: number;
    checked_at: number;
  };
  error?: string;
}

const STATUS_COLOR: Record<AgentEntry['status'], string> = {
  online: '#22d3a5',
  degraded: '#ffaa00',
  unreachable: '#ff2d6f',
  no_probe: '#5b6b7c',
};

export function useFleet(): { fleet: FleetPayload | null; error: string } {
  const [fleet, setFleet] = useState<FleetPayload | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetch(`${API}/agents`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = (await res.json()) as FleetPayload;
        if (!cancelled) {
          setFleet(body);
          setError('');
        }
      } catch (err: unknown) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  return { fleet, error };
}

export default function AgentStrip() {
  const { fleet, error } = useFleet();

  const summary = fleet?.summary;
  const agents = fleet?.agents ?? [];

  return (
    <section className="hud-plate" aria-label="Agent fleet">
      <header className="hud-plate__head">
        <h2>ACTIVE AGENTS</h2>
        <span className="hud-tag">
          {summary
            ? `${summary.online}/${summary.total} ONLINE` +
              (summary.degraded ? ` · ${summary.degraded} DEGRADED` : '') +
              (summary.unreachable ? ` · ${summary.unreachable} UNREACHABLE` : '')
            : 'PROBING…'}
        </span>
      </header>
      <div className="hud-plate__body">
        {error && <p className="ultron-error">Fleet probe failed: {error}</p>}
        {!fleet && !error && <p className="ultron-mono-dim">Probing every agent…</p>}
        {fleet && agents.length === 0 && (
          <p className="ultron-mono-dim">The backend returned an empty fleet.</p>
        )}

        <div className="hud-agents">
          {agents.map((agent) => {
            const color = STATUS_COLOR[agent.status] ?? STATUS_COLOR.no_probe;
            const chip = (
              <>
                <span className="hud-agent__dot" style={{ color }} />
                <span style={{ minWidth: 0 }}>
                  <span className="hud-agent__name">{agent.name}</span>
                  <span className="hud-agent__meta" style={{ display: 'block' }}>
                    {agent.status === 'no_probe'
                      ? 'NO PROBE'
                      : `${agent.http_status ?? '—'} · ${agent.latency_ms ?? '—'}ms`}
                    {agent.live?.active_runs ? ` · ${agent.live.active_runs} RUN` : ''}
                  </span>
                </span>
              </>
            );

            return agent.route ? (
              <Link
                key={agent.id}
                href={agent.route}
                className="hud-agent"
                title={`${agent.detail}\n${agent.probe ?? 'no probe route'}`}
                style={{ color }}
              >
                {chip}
              </Link>
            ) : (
              <div
                key={agent.id}
                className="hud-agent"
                title={agent.detail}
                style={{ color }}
              >
                {chip}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
