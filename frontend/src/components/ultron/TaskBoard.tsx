'use client';

/**
 * Task board - every task the supervisor is holding, and what it is doing.
 *
 * Backed by `GET /ultron/tasks`, which is authenticated because a task carries
 * its goal text. Progress is the backend's own ratio of finished plan steps to
 * planned steps, so a task with no plan reads 0% rather than being animated
 * towards 100% - the bar can never outrun the work.
 */

import { useEffect, useState } from 'react';
import { ULTRON_API_URL, saraFetch } from '@/lib/api';

const API = `${ULTRON_API_URL}/ultron`;

/** Fast enough that a step boundary is visible while you watch it happen. */
const POLL_MS = 2000;

export interface TaskRow {
  task_id: string;
  goal: string;
  status: string;
  phase: string;
  terminal: boolean;
  mode: string;
  current_tool: string;
  steps_done: number;
  steps_total: number;
  progress: number;
  replans: number;
  created: number;
  elapsed_s: number;
}

interface TaskPayload {
  tasks: TaskRow[];
  summary: { total: number; active: number; finished: number };
}

const PHASE_TONE: Record<string, string> = {
  received: '#7d8fa3',
  understanding: '#a78bfa',
  planning: '#a78bfa',
  executing: '#ffaa00',
  verifying: '#22d3a5',
  completed: '#22d3a5',
  completed_with_issues: '#ffaa00',
  failed: '#ff2d6f',
  stopped: '#ff9730',
};

export function useTasks(): { payload: TaskPayload | null; error: string } {
  const [payload, setPayload] = useState<TaskPayload | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await saraFetch(`${API}/tasks`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = (await res.json()) as TaskPayload;
        if (!cancelled) {
          setPayload(body);
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

  return { payload, error };
}

export default function TaskBoard({
  highlightTaskId,
}: {
  highlightTaskId: string | null;
}) {
  const { payload, error } = useTasks();
  const tasks = payload?.tasks ?? [];

  return (
    <section className="hud-plate" aria-label="Task progress">
      <header className="hud-plate__head">
        <h2>TASK PROGRESS</h2>
        <span className="hud-tag">
          {payload
            ? `${payload.summary.active} ACTIVE · ${payload.summary.finished} FINISHED`
            : 'LOADING…'}
        </span>
      </header>
      <div className="hud-plate__body">
        {error && <p className="ultron-error">Task list unavailable: {error}</p>}
        {!payload && !error && <p className="ultron-mono-dim">Reading the session store…</p>}
        {payload && tasks.length === 0 && (
          <p className="ultron-mono-dim">
            No tasks yet. Submit an objective and it will appear here the moment the
            backend creates it.
          </p>
        )}

        <div className="hud-tasks">
          {tasks.map((task) => {
            const tone = PHASE_TONE[task.phase] ?? '#7d8fa3';
            const percent = Math.round(task.progress * 100);
            return (
              <article
                key={task.task_id}
                className={`hud-task${task.terminal ? '' : ' hud-task--active'}`}
                style={
                  task.task_id === highlightTaskId
                    ? { borderColor: 'var(--hud-cyan)' }
                    : undefined
                }
              >
                <div className="hud-task__goal">{task.goal || '(no goal recorded)'}</div>
                <div
                  style={{
                    fontFamily: 'var(--sara-font-mono)',
                    fontSize: 10,
                    letterSpacing: '0.12em',
                    color: tone,
                    textAlign: 'right',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {task.phase.toUpperCase()}
                </div>
                <div
                  style={{
                    gridColumn: '1 / -1',
                    fontFamily: 'var(--sara-font-mono)',
                    fontSize: 9.5,
                    letterSpacing: '0.1em',
                    color: 'var(--sara-faint)',
                  }}
                >
                  {task.steps_done}/{task.steps_total || '—'} STEPS · {percent}% ·{' '}
                  {task.elapsed_s}s
                  {task.current_tool ? ` · ${task.current_tool}` : ''}
                  {task.replans ? ` · REPLANNED ${task.replans}×` : ''}
                  {task.mode === 'assisted' ? ' · ASSISTED' : ''}
                </div>
                <div className="hud-task__bar">
                  <span
                    style={{ width: `${percent}%`, background: tone }}
                    aria-hidden="true"
                  />
                </div>
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}
