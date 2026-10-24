'use client';

/**
 * React binding for the Ultron holographic core.
 *
 * The scene is created exactly once per mount and then driven through the
 * `UltronCoreApi`: prop changes never recreate the WebGL context. That matters
 * because this component re-renders on every streamed backend event, and
 * rebuilding a 15k-particle scene per event would be both a leak and a
 * stutter.
 *
 * Rendering failures (no WebGL context, blocklisted driver, exhausted GPU
 * memory) are handled by the scene module, which reports them in place and
 * hands back an inert API - so a machine without a working GPU still gets a
 * fully functional command center rather than a crashed page.
 */

import React, { useEffect, useRef } from 'react';
import {
  createUltronCore,
  type UltronCoreApi,
  type UltronCoreMode,
} from '@/lib/ultronCore';

export interface UltronCoreCanvasProps {
  mode: UltronCoreMode;
  /** Real completion of the current plan, 0..1. */
  progress: number;
  /** Increments on every real tool result - drives the shockwave. */
  eventTick?: number;
  /** Increments on every real failed step - shifts the core amber. */
  failureTick?: number;
  className?: string;
}

export default function UltronCoreCanvas({
  mode,
  progress,
  eventTick = 0,
  failureTick = 0,
  className,
}: UltronCoreCanvasProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  const apiRef = useRef<UltronCoreApi | null>(null);
  const seenEvents = useRef(eventTick);
  const seenFailures = useRef(failureTick);

  useEffect(() => {
    const node = mountRef.current;
    if (!node) return undefined;

    const api = createUltronCore(node);
    apiRef.current = api;

    return () => {
      api.dispose();
      apiRef.current = null;
    };
  }, []);

  useEffect(() => {
    apiRef.current?.setState({ mode, progress });
  }, [mode, progress]);

  useEffect(() => {
    if (eventTick === seenEvents.current) return;
    seenEvents.current = eventTick;
    apiRef.current?.pulse(1);
  }, [eventTick]);

  useEffect(() => {
    if (failureTick === seenFailures.current) return;
    seenFailures.current = failureTick;
    apiRef.current?.flag('fail');
  }, [failureTick]);

  // The wrapper positions itself absolutely so it fills whichever positioned
  // stage hosts it. This is deliberately inline rather than a stylesheet
  // dependency: a `position: relative` wrapper containing only absolutely
  // positioned children collapses to zero height, and a zero-height container
  // produced a 1px canvas and an invisible core.
  return (
    <div className={className} style={{ position: 'absolute', inset: 0 }}>
      <div ref={mountRef} style={{ position: 'absolute', inset: 0 }} />
    </div>
  );
}
