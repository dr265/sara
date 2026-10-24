'use client';

/**
 * Ultron memory panel.
 *
 * Shows what is actually on disk - the experiences the orchestrator recorded
 * and the learned rules it injects into every future plan - and performs real
 * mutations against the same store. Persistence is verifiable: reload this page
 * and the rules, their active state and their ids are the same, because they
 * live in ``$FULLAI_HOME/memory/sara_ultron_agent``, not in component state.
 *
 * The Add-Rule flow reports exactly what the backend did. Adding a rule that
 * already exists returns ``changed: false``, and that is what the panel says,
 * instead of a blanket "saved".
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ULTRON_API_URL, saraFetch } from '@/lib/api';
import './console.css';

const API = `${ULTRON_API_URL}/ultron`;

interface RuleRecord {
  id: string;
  rule: string;
  category?: string;
  origin?: string;
  active?: boolean;
  created_at?: number;
}

interface Experience {
  goal: string;
  success: boolean;
  errors?: string;
  timestamp?: number;
  steps: number;
}

interface MemoryResponse {
  available?: boolean;
  store?: string;
  experiences?: number;
  rules?: number;
  success_rate?: number | null;
  recent?: Experience[];
  rule_records?: RuleRecord[];
}

export default function MemoryPanel() {
  const [memory, setMemory] = useState<MemoryResponse | null>(null);
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [draft, setDraft] = useState('');
  const [working, setWorking] = useState(false);
  const mounted = useRef(true);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API}/memory`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as MemoryResponse;
      if (!mounted.current) return;
      setMemory(data);
      setError('');
    } catch (err: unknown) {
      if (!mounted.current) return;
      setError(
        `Could not read the memory store: ${
          err instanceof Error ? err.message : String(err)
        }`,
      );
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const kickoff = async () => {
      await load();
    };
    void kickoff();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  const mutate = useCallback(
    async (body: Record<string, unknown>) => {
      setWorking(true);
      setNote('');
      try {
        const res = await saraFetch(`${API}/memory/rules`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json().catch(() => null);
        if (!res.ok) {
          const detail =
            typeof data?.detail === 'string'
              ? data.detail
              : `The backend refused the change (HTTP ${res.status}).`;
          setNote(detail);
          return;
        }
        setMemory((prev) =>
          prev ? { ...prev, rules: (data.rules ?? []).length, rule_records: data.rules } : prev,
        );
        if (typeof data?.detail === 'string' && data.detail) setNote(data.detail);
        else if (data?.changed === false) setNote('Nothing changed.');
        else setNote('');
        if (data?.changed) await load();
      } catch (err: unknown) {
        setNote(
          `Could not reach the memory endpoint: ${
            err instanceof Error ? err.message : String(err)
          }`,
        );
      } finally {
        if (mounted.current) setWorking(false);
      }
    },
    [load],
  );

  /** Delete one stored experience. Timestamp-keyed, so it cannot miss. */
  const forget = useCallback(
    async (exp: Experience) => {
      if (exp.timestamp == null) return;
      setWorking(true);
      setNote('');
      try {
        const res = await saraFetch(`${API}/memory/forget`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ timestamp: exp.timestamp, goal: exp.goal }),
        });
        if (!res.ok) {
          const detail = (await res.json().catch(() => null))?.detail;
          setNote(
            typeof detail === 'string'
              ? detail
              : `The run could not be removed (HTTP ${res.status}).`,
          );
          return;
        }
        await load();
      } catch (err: unknown) {
        setNote(
          `Could not reach the memory endpoint: ${
            err instanceof Error ? err.message : String(err)
          }`,
        );
      } finally {
        if (mounted.current) setWorking(false);
      }
    },
    [load],
  );

  const addRule = useCallback(async () => {
    const rule = draft.trim();
    if (!rule) return;
    await mutate({ action: 'add', rule });
    setDraft('');
  }, [draft, mutate]);

  const successRate =
    typeof memory?.success_rate === 'number'
      ? `${Math.round(memory.success_rate * 100)}%`
      : '—';

  return (
    <section className="ultron-panel">
      <header className="ultron-panel__head">
        <h2>MEMORY</h2>
        <div className="ultron-panel__tools">
          <span className="ultron-mono-dim">
            {memory?.experiences ?? 0} runs · {memory?.rules ?? 0} rules · {successRate} success
          </span>
          <button
            type="button"
            className="ultron-btn ultron-btn--ghost"
            onClick={() => void load()}
          >
            RELOAD
          </button>
        </div>
      </header>

      {error && <p className="ultron-warn">{error}</p>}
      {note && <p className="ultron-mono-dim ultron-footnote">{note}</p>}

      <div className="ultron-memory">
        <div>
          <h3 className="ultron-subhead">LEARNED RULES</h3>
          <div className="ultron-inline">
            <input
              className="ultron-input"
              value={draft}
              maxLength={400}
              placeholder="Teach a standing rule (e.g. always reply in Hindi)"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void addRule();
                }
              }}
            />
            <button
              type="button"
              className="ultron-btn"
              disabled={working || !draft.trim()}
              onClick={() => void addRule()}
            >
              ADD
            </button>
          </div>

          {(memory?.rule_records ?? []).length === 0 ? (
            <p className="ultron-mono-dim">
              No rules stored yet. Ultron also writes rules itself when a run
              fails for a specific, non-transient reason.
            </p>
          ) : (
            <ul className="ultron-rules">
              {(memory?.rule_records ?? []).map((rule) => (
                <li key={rule.id} className="ultron-rule">
                  <span
                    className="ultron-rule__state"
                    style={{ color: rule.active ? '#22d3a5' : '#5b6b7f' }}
                  >
                    {rule.active ? 'ACTIVE' : 'OFF'}
                  </span>
                  <span className="ultron-rule__body">{rule.rule}</span>
                  <span className="ultron-mono-dim">{rule.category}</span>
                  <button
                    type="button"
                    className="ultron-btn ultron-btn--ghost"
                    disabled={working}
                    onClick={() => void mutate({ action: 'toggle', rule_id: rule.id })}
                  >
                    {rule.active ? 'DISABLE' : 'ENABLE'}
                  </button>
                  <button
                    type="button"
                    className="ultron-btn ultron-btn--danger"
                    disabled={working}
                    onClick={() => void mutate({ action: 'delete', rule_id: rule.id })}
                  >
                    DELETE
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <h3 className="ultron-subhead">RECENT RUNS</h3>
          {(memory?.recent ?? []).length === 0 ? (
            <p className="ultron-mono-dim">Nothing recorded yet.</p>
          ) : (
            <ul className="ultron-runs">
              {(memory?.recent ?? []).map((exp, index) => (
                <li key={`${exp.timestamp ?? index}-${index}`} className="ultron-run">
                  <span
                    className="ultron-rule__state"
                    style={{ color: exp.success ? '#22d3a5' : '#ffaa00' }}
                  >
                    {exp.success ? 'OK' : 'FAIL'}
                  </span>
                  <span className="ultron-run__goal">{exp.goal}</span>
                  <span className="ultron-mono-dim">
                    {exp.steps} step{exp.steps === 1 ? '' : 's'}
                  </span>
                  <button
                    type="button"
                    className="ultron-btn ultron-btn--danger"
                    disabled={working || exp.timestamp == null}
                    title="Delete this stored experience"
                    onClick={() => void forget(exp)}
                  >
                    FORGET
                  </button>
                  {!exp.success && exp.errors && (
                    <span className="ultron-run__error">{exp.errors}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {memory?.store && (
        <p className="ultron-mono-dim ultron-footnote">
          Persisted at <code>{memory.store}</code> — surviving a backend restart is
          the point of this panel, so rules are read back from disk, not from
          component state.
        </p>
      )}
    </section>
  );
}
