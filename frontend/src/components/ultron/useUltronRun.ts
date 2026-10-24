'use client';

/**
 * Ultron run controller.
 *
 * Owns one supervised run: start -> stream -> approve/deny -> halt, plus the
 * derived state the command center renders (lifecycle phase, real progress,
 * the current tool). Everything reported here comes from the backend stream;
 * nothing is inferred optimistically.
 *
 * Split out of the page so the command center's layout and the run's behaviour
 * can change independently, and so a second surface (the voice page, a future
 * dashboard) can drive the same controller without copying it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ULTRON_API_URL, saraFetch } from '@/lib/api';
import type { UltronCoreMode } from '@/lib/ultronCore';

const API = `${ULTRON_API_URL}/ultron`;

export interface PlanStepPayload {
  tool: string;
  args?: Record<string, unknown>;
  rationale?: string;
}

export type StepStatus = 'running' | 'ok' | 'failed';

export interface ToolStep {
  callId: string;
  name: string;
  args?: Record<string, unknown>;
  status: StepStatus;
  output?: string;
  seconds?: number;
  attempts?: number;
  source?: string;
}

export interface Approval {
  callId: string;
  name: string;
  prompt: string;
}

/** Every event shape the Ultron SSE stream can emit. */
interface StreamEvent {
  type: string;
  message?: string;
  tools?: string[];
  native_tools?: string[];
  desktop_tools?: string[];
  version?: string;
  steps?: PlanStepPayload[];
  source?: string;
  dropped?: string[];
  attempt?: number;
  call_id?: string;
  name?: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  output?: string;
  seconds?: number;
  attempts?: number;
  prompt?: string;
  phase?: string;
  status?: string;
  summary?: string;
  elapsed?: number;
}

/** Backend lifecycle phases, in the order the orchestrator emits them. */
export const PHASE_ORDER = [
  'received',
  'understanding',
  'planning',
  'executing',
  'verifying',
] as const;

export const PHASE_LABEL: Record<string, string> = {
  received: 'RECEIVED',
  understanding: 'UNDERSTANDING',
  planning: 'PLANNING',
  executing: 'EXECUTING',
  verifying: 'VERIFYING',
  completed: 'VERIFIED',
  completed_with_issues: 'ISSUES',
  failed: 'FAILED',
  stopped: 'HALTED',
};

export interface UltronRunController {
  goal: string;
  setGoal: (value: string) => void;
  mode: 'autonomous' | 'assisted';
  setMode: (value: 'autonomous' | 'assisted') => void;
  taskId: string | null;
  status: string;
  phase: string;
  plan: PlanStepPayload[];
  dropped: string[];
  logs: string[];
  steps: ToolStep[];
  summary: string;
  approval: Approval | null;
  busy: boolean;
  replans: number;
  toolCount: number;
  /** Real completed-steps / planned-steps ratio, 0 when there is no plan. */
  progress: number;
  /** Core visual mode, derived from the real phase - never from a timer. */
  coreMode: UltronCoreMode;
  eventTick: number;
  failureTick: number;
  run: (customGoal?: string) => Promise<void>;
  approve: (action: 'APPROVE' | 'DENY') => Promise<void>;
  halt: () => Promise<void>;
}

export function useUltronRun(): UltronRunController {
  const [goal, setGoal] = useState('');
  const [mode, setMode] = useState<'autonomous' | 'assisted'>('autonomous');
  const [taskId, setTaskId] = useState<string | null>(null);
  const [status, setStatus] = useState('idle');
  const [phase, setPhase] = useState('received');
  const [plan, setPlan] = useState<PlanStepPayload[]>([]);
  const [dropped, setDropped] = useState<string[]>([]);
  const [logs, setLogs] = useState<string[]>([]);
  const [steps, setSteps] = useState<ToolStep[]>([]);
  const [summary, setSummary] = useState('');
  const [approval, setApproval] = useState<Approval | null>(null);
  const [busy, setBusy] = useState(false);
  const [replans, setReplans] = useState(0);
  const [toolCount, setToolCount] = useState(0);
  const [eventTick, setEventTick] = useState(0);
  const [failureTick, setFailureTick] = useState(0);

  const streamRef = useRef<EventSource | null>(null);
  const taskRef = useRef<string | null>(null);

  /** Close the stream exactly once; safe to call from anywhere. */
  const closeStream = useCallback(() => {
    streamRef.current?.close();
    streamRef.current = null;
  }, []);

  // One stream per mounted controller; a navigation away must not leave an
  // EventSource reconnecting in the background.
  useEffect(() => {
    return () => closeStream();
  }, [closeStream]);

  const handleEvent = useCallback((ev: StreamEvent) => {
    switch (ev.type) {
      case 'meta':
        setToolCount(ev.tools?.length ?? 0);
        break;
      case 'phase':
        // The orchestrator only emits a phase it actually reached.
        if (ev.phase) setPhase(ev.phase);
        break;
      case 'plan':
        setPlan(ev.steps ?? []);
        setDropped(ev.dropped ?? []);
        break;
      case 'replan':
        setReplans(ev.attempt ?? 0);
        break;
      case 'log':
        setLogs((prev) => [...prev, ev.message ?? '']);
        break;
      case 'tool_call':
        setSteps((prev) => [
          ...prev,
          {
            callId: ev.call_id || `${prev.length}`,
            name: ev.name || 'tool',
            args: ev.args,
            status: 'running',
          },
        ]);
        break;
      case 'tool_result':
        setSteps((prev) =>
          prev.map((step) =>
            step.callId === (ev.call_id || '')
              ? {
                  ...step,
                  status: ev.ok ? 'ok' : 'failed',
                  output: ev.output,
                  seconds: ev.seconds,
                  attempts: ev.attempts,
                }
              : step,
          ),
        );
        setEventTick((n) => n + 1);
        if (!ev.ok) setFailureTick((n) => n + 1);
        break;
      case 'approval_request':
        setApproval({
          callId: ev.call_id || '',
          name: ev.name || 'action',
          prompt: ev.prompt || 'Authorization required',
        });
        break;
      case 'done':
        setStatus(ev.status ?? 'completed');
        setBusy(false);
        setApproval(null);
        closeStream();
        setSummary(ev.summary || '');
        break;
      default:
        break;
    }
  }, [closeStream]);

  const openStream = useCallback(
    (id: string) => {
      closeStream();
      const stream = new EventSource(`${API}/events?task_id=${encodeURIComponent(id)}`);
      streamRef.current = stream;
      stream.onmessage = (message) => {
        try {
          handleEvent(JSON.parse(message.data) as StreamEvent);
        } catch {
          // A malformed frame or a keepalive comment is not a reason to drop a
          // running task; the stream stays open.
        }
      };
      stream.onerror = () => {
        if (streamRef.current?.readyState === EventSource.CLOSED) {
          streamRef.current = null;
          setBusy(false);
        }
      };
    },
    [closeStream, handleEvent],
  );

  const run = useCallback(
    async (customGoal?: string) => {
      const target = (customGoal ?? goal).trim();
      if (!target) return;
      if (customGoal) setGoal(customGoal);

      // Reset every derived field so a stale plan can never be read as the
      // current run's plan.
      setPlan([]);
      setDropped([]);
      setLogs([]);
      setSteps([]);
      setSummary('');
      setApproval(null);
      setTaskId(null);
      setReplans(0);
      setPhase('received');
      setStatus('starting');
      setBusy(true);

      try {
        const res = await saraFetch(`${API}/run`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ goal: target, mode }),
        });
        if (!res.ok) {
          let detail = `Failed to start (HTTP ${res.status})`;
          try {
            const body = await res.json();
            if (typeof body?.detail === 'string') detail = body.detail;
          } catch {
            // keep the HTTP status as the explanation
          }
          throw new Error(detail);
        }
        const data = await res.json();
        taskRef.current = data.task_id;
        setTaskId(data.task_id);
        setStatus('running');
        openStream(data.task_id);
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        setLogs((prev) => [...prev, `[ERROR] ${message}`]);
        setStatus('failed');
        setBusy(false);
      }
    },
    [goal, mode, openStream],
  );

  const approve = useCallback(
    async (action: 'APPROVE' | 'DENY') => {
      const id = taskRef.current;
      if (!id) return;
      setApproval(null);
      try {
        const res = await saraFetch(`${API}/approve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: id, call_id: approval?.callId, action }),
        });
        if (!res.ok) {
          setLogs((prev) => [...prev, `[ERROR] Approval was not accepted (HTTP ${res.status}).`]);
        }
      } catch {
        setLogs((prev) => [...prev, '[ERROR] Could not reach the approval endpoint.']);
      }
    },
    [approval],
  );

  const halt = useCallback(async () => {
    const id = taskRef.current;
    if (!id) return;
    try {
      await saraFetch(`${API}/stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: id }),
      });
    } catch {
      setLogs((prev) => [...prev, '[ERROR] Could not reach the stop endpoint.']);
    }
  }, []);

  /* Derived, real progress: finished plan steps over planned steps. */
  const progress = useMemo(() => {
    if (!plan.length) return 0;
    const finished = steps.filter((s) => s.status !== 'running').length;
    return Math.max(0, Math.min(1, finished / plan.length));
  }, [plan.length, steps]);

  /* The core's visual mode is a pure function of real backend state. */
  const coreMode = useMemo<UltronCoreMode>(() => {
    if (status === 'failed') return 'error';
    if (busy) {
      if (PHASE_ORDER.includes(phase as (typeof PHASE_ORDER)[number])) {
        return phase as UltronCoreMode;
      }
      return 'executing';
    }
    return 'idle';
  }, [busy, phase, status]);

  return {
    goal,
    setGoal,
    mode,
    setMode,
    taskId,
    status,
    phase,
    plan,
    dropped,
    logs,
    steps,
    summary,
    approval,
    busy,
    replans,
    toolCount,
    progress,
    coreMode,
    eventTick,
    failureTick,
    run,
    approve,
    halt,
  };
}
