'use client';

import React from 'react';

interface ChatRow {
  id: string;
  guest: string;
  channel: string;
  status: string;
  lastMessage: string;
}

const CHATS: ChatRow[] = [
  { id: 'TG-991', guest: 'John Doe', channel: 'Telegram', status: 'Automated', lastMessage: 'I need a room for tomorrow.' },
  { id: 'WA-442', guest: '+1 555-0192', channel: 'WhatsApp', status: 'Handoff Pending', lastMessage: 'Can I bring my dog?' },
];

export default function ConversationsPage() {
  return (
    <div className="space-y-6">
      <div className="sp-page-head">
        <div>
          <h2 className="sp-page-title">Live Conversations</h2>
          <p className="sp-page-sub">
            Monitor active guest inquiries across Telegram and WhatsApp.
          </p>
        </div>
      </div>

      <section className="sp-card">
        <div className="sp-table-wrap">
          <table className="sp-table">
            <thead>
              <tr>
                <th>Session ID</th>
                <th>Guest</th>
                <th>Channel</th>
                <th>Status</th>
                <th>Last Message</th>
              </tr>
            </thead>
            <tbody>
              {CHATS.map((c) => (
                <tr key={c.id}>
                  <td className="sara-mono">{c.id}</td>
                  <td>{c.guest}</td>
                  <td>{c.channel}</td>
                  <td>
                    <span
                      className={`sara-status ${c.status === 'Automated' ? 'sara-status-ok' : 'sara-status-warn'}`}
                    >
                      <span className="sara-status-dot" aria-hidden="true" />
                      {c.status}
                    </span>
                  </td>
                  <td>{c.lastMessage}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
