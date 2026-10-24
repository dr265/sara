'use client';

import React from 'react';

const CARDS = [
  {
    title: 'Knowledge Base Rules',
    body: 'Upload standard PDF or CSV files. The engine automatically chunks, vectorizes, and indexes the data for SARA to use during live chats.',
  },
  {
    title: 'Google Sheets Sync',
    body: 'Ensure pop-ups are allowed in your browser to complete the OAuth authorization. SARA will automatically create a new sheet upon your first sync.',
  },
  {
    title: 'Telegram Webhook',
    body: 'The booking agent polls Telegram directly. Incoming payloads are parsed, enriched against the knowledge base, and answered automatically.',
  },
  {
    title: 'Escalation Policy',
    body: 'Conversations that fail confidence thresholds are flagged for human handoff and surfaced in the Live Conversations view.',
  },
];

export default function HelpPage() {
  return (
    <div className="space-y-6">
      <div className="sp-page-head">
        <div>
          <h2 className="sp-page-title">Platform Documentation</h2>
          <p className="sp-page-sub">
            Operating notes for every connected subsystem.
          </p>
        </div>
      </div>

      <div className="sp-cols-2 sp-grid">
        {CARDS.map((c) => (
          <article key={c.title} className="sp-card">
            <h3 className="sp-card-title">{c.title}</h3>
            <p className="sp-card-note">{c.body}</p>
          </article>
        ))}
      </div>
    </div>
  );
}
