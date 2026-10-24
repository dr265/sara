'use client';

/**
 * Execution timeline: the real lifecycle, the plan, and what each step did.
 *
 * The phase strip is not a decorative progress bar. The backend emits a
 * ``phase`` event only from the code that reached that phase, so a phase is
 * lit here only when the orchestrator actually got there, and a run that
 * stopped during EXECUTING shows exactly that - it does not jump to the end.
 */

import React from 'react';
import {
  PHASE_LABEL,
  PHASE_ORDER,
  type PlanStepPayload,
  type ToolStep,
} from './useUltronRun';
import './console.css';

const PHASE_TONE: Record<string, string> = {
  received: '#5b6b7f',
  understanding: '#7dd3fc',
  planning: '#ffaa00',
  executing: '#00f0ff',
  verifying: '#a78bfa',
};

export function PhaseStrip({ phase, failed }: { phase: string; failed: boolean }) {
  const terminal = phase === 'completed' || phase === 'completed_with_issues'
    || phase === 'failed' || phase === 'stopped';
  const currentIndex = PHASE_ORDER.indexOf(phase as (typeof PHASE_ORDER)[number]);

  return (
    <ol className="ultron-phases" aria-label="Task lifecycle">
      {PHASE_ORDER.map((name, index) => {
        const reached = terminal || (currentIndex >= 0 && index <= currentIndex);
        const isCurrent = !terminal && index === currentIndex;
        const tone = failed && name === 'executing' ? '#ff5470' : PHASE_TONE[name];
        return (
          <li
            key={name}
            className={`ultron-phase${isCurrent ? ' ultron-phase--current' : ''}`}
            style={{
              color: reached ? tone : '#3b4757',
              borderColor: reached ? `${tone}55` : '#1a2432',
              background: isCurrent ? `${tone}18` : 'transparent',
            }}
          >
            <span className="ultron-phase__mark">{reached ? '◆' : '◇'}</span>
            {PHASE_LABEL[name]}
          </li>
        );
      })}
      {terminal && (
        <li
          className="ultron-phase ultron-phase--terminal"
          style={{
            color: phase === 'completed' ? '#22d3a5'
              : phase === 'completed_with_issues' ? '#ffaa00'
              : phase === 'stopped' ? '#f97316' : '#ff5470',
            borderColor:
              phase === 'completed' ? '#22d3a555'
                : phase === 'completed_with_issues' ? '#ffaa0055'
                : '#ff547055',
          }}
        >
          {PHASE_LABEL[phase]}
        </li>
      )}
    </ol>
  );
}

export function PlanList({
  plan,
  dropped,
  steps,
}: {
  plan: PlanStepPayload[];
  dropped: string[];
  steps: ToolStep[];
}) {
  return (
    <section className="ultron-panel">
      <header className="ultron-panel__head">
        <h2>EXECUTION PLAN</h2>
        {plan.length > 0 && (
          <span className="ultron-mono-dim">
            {steps.filter((s) => s.status !== 'running').length}/{plan.length} steps executed
          </span>
        )}
      </header>

      {plan.length === 0 ? (
        <p className="ultron-mono-dim">
          No plan yet. Submit an objective to see how Ultron decomposes it.
        </p>
      ) : (
        <ol className="ultron-plan">
          {plan.map((step, index) => {
            const executed = steps[index];
            const tone =
              executed?.status === 'ok' ? '#22d3a5'
                : executed?.status === 'failed' ? '#ff5470'
                : executed?.status === 'running' ? '#00f0ff'
                : '#5b6b7f';
            return (
              <li key={`${step.tool}-${index}`} className="ultron-plan__step">
                <span className="ultron-plan__index" style={{ color: tone }}>
                  {String(index + 1).padStart(2, '0')}
                </span>
                <div>
                  <div>
                    <strong style={{ color: executed ? tone : '#cbd5e1' }}>{step.tool}</strong>
                    {step.args && Object.keys(step.args).length > 0 && (
                      <code className="ultron-mono-dim ultron-plan__args">
                        {Object.entries(step.args)
                          .slice(0, 3)
                          .map(([key, value]) => `${key}=${String(value).slice(0, 40)}`)
                          .join(', ')}
                      </code>
                    )}
                  </div>
                  {step.rationale && (
                    <div className="ultron-plan__rationale">{step.rationale}</div>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}

      {dropped.length > 0 && (
        <p className="ultron-warn">
          Dropped unknown tool reference(s): {dropped.join(', ')} — the planner
          named tools that do not exist, so they were never executed.
        </p>
      )}
    </section>
  );
}

export function StepTimeline({ steps }: { steps: ToolStep[] }) {
  if (steps.length === 0) return null;
  return (
    <section className="ultron-panel">
      <header className="ultron-panel__head">
        <h2>STEP RESULTS</h2>
        <span className="ultron-mono-dim">
          {steps.filter((s) => s.status === 'ok').length} ok ·{' '}
          {steps.filter((s) => s.status === 'failed').length} failed
        </span>
      </header>
      <div className="ultron-steps">
        {steps.map((step) => {
          const tone =
            step.status === 'ok' ? '#22d3a5'
              : step.status === 'failed' ? '#ff5470'
              : '#00f0ff';
          return (
            <div key={step.callId} className="ultron-step">
              <div className="ultron-step__head">
                <span style={{ color: tone }}>
                  {step.status === 'ok' ? '✔' : step.status === 'failed' ? '✖' : '▶'}
                </span>
                <strong>{step.name}</strong>
                {step.seconds != null && (
                  <span className="ultron-mono-dim">{step.seconds}s</span>
                )}
                {step.attempts != null && step.attempts > 1 && (
                  <span className="ultron-mono-dim">{step.attempts} attempts</span>
                )}
              </div>
              {step.output && <pre className="ultron-step__out">{step.output.slice(0, 600)}</pre>}
            </div>
          );
        })}
      </div>
    </section>
  );
}
