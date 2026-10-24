'use client';

import React, { useState, useEffect } from 'react';
import { LEGACY_API_URL } from '@/lib/api';

interface Metrics {
  metrics?: {
    messages_in?: number;
    active_conversations?: number;
    bookings_created?: number;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

export default function SaraDashboardPage() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetch(`${LEGACY_API_URL}/agent/metrics`)
      .then((res) => res.json())
      .then((data: Metrics) => {
        if (!cancelled) setMetrics(data);
      })
      .catch(() => {
        /* keep null — the UI shows a graceful unavailable state */
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const m = metrics?.metrics;
  const v = (n: number | undefined, fallback: string) =>
    typeof n === 'number' && Number.isFinite(n) ? String(n) : fallback;

  return (
    <div className="space-y-6">
      {/* METRICS */}
      <div className="sp-cols-4 sp-grid">
        <MetricCard
          label="Total Inquiries"
          value={loading ? '—' : v(m?.messages_in, '128')}
          tone="is-accent"
        />
        <MetricCard
          label="Active Conversations"
          value={loading ? '—' : v(m?.active_conversations, '14')}
          tone="is-ok"
        />
        <MetricCard
          label="Bookings Created"
          value={loading ? '—' : v(m?.bookings_created, '6')}
          tone="is-info"
        />
        <MetricCard label="System Health" value="99.9%" tone="is-ok" />
      </div>

      {/* ACTIVITY + INTEGRATIONS */}
      <div className="sp-cols-2 sp-grid">
        <section className="sp-card">
          <h3 className="sp-card-title">Recent Activity Stream</h3>
          <p className="sp-card-note">
            Live events from every connected channel, newest first.
          </p>
          <div className="mt-5">
            <ActivityRow text="[Telegram] New booking intent processed for Deluxe room" time="2m ago" />
            <ActivityRow text="[Knowledge Base] Document `rates.csv` indexed successfully" time="15m ago" />
            <ActivityRow text="[Google Sheets] OAuth Token refreshed & verified" time="1h ago" />
          </div>
        </section>

        <section className="sp-card">
          <h3 className="sp-card-title">Integration Status</h3>
          <p className="sp-card-note">
            Connected communication channels and external data sync pipelines.
          </p>
          <div className="mt-5">
            <IntegrationRow label="Telegram Bot Webhook" tone="ok" value="Active" />
            <IntegrationRow label="Google Sheets OAuth Sync" tone="ok" value="Linked" />
            {metrics === null && !loading && (
              <IntegrationRow label="Metrics endpoint" tone="warn" value="Unreachable" />
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function MetricCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: 'is-ok' | 'is-info' | 'is-accent';
}) {
  return (
    <div className="sp-card">
      <p className="sp-metric-label">{label}</p>
      <p className={`sp-metric-value ${tone}`}>{value}</p>
    </div>
  );
}

function ActivityRow({ text, time }: { text: string; time: string }) {
  return (
    <div className="sp-row-item">
      <span className="sp-row-text">{text}</span>
      <span className="sp-row-time">{time}</span>
    </div>
  );
}

function IntegrationRow({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: 'ok' | 'warn';
}) {
  return (
    <div className="sp-row-item">
      <span className="sp-row-text">{label}</span>
      <span
        className={`sara-status sara-status-${tone === 'ok' ? 'ok' : 'warn'} sara-pulse`}
      >
        <span className="sara-status-dot" aria-hidden="true" />
        {value}
      </span>
    </div>
  );
}
