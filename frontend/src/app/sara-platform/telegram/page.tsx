'use client';

import React, { useState } from 'react';
import { LEGACY_API_URL } from '@/lib/api';

export default function TelegramConnectionPage() {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [resultTone, setResultTone] = useState<'ok' | 'err'>('ok');

  const handleTest = async () => {
    setTesting(true);
    setResult(null);
    try {
      const res = await fetch(`${LEGACY_API_URL}/telegram/status`);
      if (!res.ok) throw new Error(`status ${res.status}`);
      const data = await res.json();
      setResult(
        data.running
          ? 'Connection healthy — agent is running and listening.'
          : 'Connection reachable — agent is currently paused.',
      );
      setResultTone(data.running ? 'ok' : 'err');
    } catch (err) {
      setResult(
        `Unable to reach the Telegram endpoint: ${(err as Error).message}. Is the backend running on port 8000?`,
      );
      setResultTone('err');
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="sp-page-head">
        <div>
          <h2 className="sp-page-title">Telegram Bot Connection</h2>
          <p className="sp-page-sub">
            Manage your active Telegram Assistant integration and webhook health.
          </p>
        </div>
        <span className="sara-status sara-status-ok sara-pulse">
          <span className="sara-status-dot" aria-hidden="true" />
          Active &amp; Listening
        </span>
      </div>

      <section className="sp-card">
        <div className="sp-cols-2 sp-grid">
          <div className="sp-kv">
            <p className="sp-metric-label">Polling Strategy</p>
            <p className="sp-kv-value is-accent">Direct Adapter (Bypass)</p>
          </div>
          <div className="sp-kv">
            <p className="sp-metric-label">Human Handoff Lock</p>
            <p className="sp-kv-value is-ok">Overridden / Disabled</p>
          </div>
        </div>

        <div className="sp-actions">
          <button
            onClick={handleTest}
            disabled={testing}
            className="sara-btn sara-btn-primary sara-focus-ring"
          >
            {testing ? 'Testing…' : 'Test Connection'}
          </button>
          <button className="sara-btn sara-btn-danger sara-focus-ring">
            Disconnect Provider
          </button>
        </div>

        {result && (
          <p
            className={`sp-test-result ${resultTone === 'ok' ? 'is-ok' : 'is-err'}`}
            role="status"
          >
            {result}
          </p>
        )}
      </section>
    </div>
  );
}
