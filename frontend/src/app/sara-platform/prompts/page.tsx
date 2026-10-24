'use client';

import React, { useState } from 'react';

type Tab = 'business' | 'behavior' | 'escalation';

const TABS: { id: Tab; label: string }[] = [
  { id: 'business', label: 'Business Context' },
  { id: 'behavior', label: 'Response Behavior' },
  { id: 'escalation', label: 'Escalation Rules' },
];

export default function PromptsRulesPage() {
  const [activeTab, setActiveTab] = useState<Tab>('business');
  const [isSaving, setIsSaving] = useState(false);

  const handleSave = () => {
    setIsSaving(true);
    setTimeout(() => {
      setIsSaving(false);
      alert('Rules successfully synchronized with SARA Engine.');
    }, 800);
  };

  return (
    <div className="space-y-6">
      <div className="sp-page-head">
        <div>
          <h2 className="sp-page-title">Prompts &amp; Rules Configuration</h2>
          <p className="sp-page-sub">
            Manage SARA&apos;s business logic, tone, and operational workflows.
          </p>
        </div>
        <button
          onClick={handleSave}
          disabled={isSaving}
          className="sara-btn sara-btn-primary sara-focus-ring"
        >
          {isSaving ? 'Deploying…' : 'Publish Changes'}
        </button>
      </div>

      <div className="sp-tabs" role="tablist" aria-label="Rules sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={activeTab === t.id}
            onClick={() => setActiveTab(t.id)}
            className={`sara-focus-ring sp-tab ${activeTab === t.id ? 'sp-tab-on' : ''}`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="sp-card" role="tabpanel">
        {activeTab === 'business' && (
          <div className="space-y-5">
            <label className="block">
              <span className="sp-field-label">Hotel / Business Name</span>
              <input
                type="text"
                defaultValue="SARA Grand Hotel"
                className="sara-input"
              />
            </label>
            <label className="block">
              <span className="sp-field-label">Core Offerings &amp; Guidelines</span>
              <textarea
                rows={6}
                defaultValue="Check-in is at 3 PM. Check-out is at 11 AM. We offer Deluxe and Standard rooms. Pets are not allowed."
                className="sara-textarea"
              />
            </label>
          </div>
        )}

        {activeTab === 'behavior' && (
          <label className="block">
            <span className="sp-field-label">Agent Persona</span>
            <textarea
              rows={4}
              defaultValue="You are a polite, professional, and concise booking assistant. Always confirm dates before asking for personal details."
              className="sara-textarea"
            />
          </label>
        )}

        {activeTab === 'escalation' && (
          <label className="block">
            <span className="sp-field-label">Human Handoff Trigger</span>
            <textarea
              rows={3}
              defaultValue="If a user asks about refunds, cancellations, or uses aggressive language, immediately transfer the chat to human staff."
              className="sara-textarea"
            />
          </label>
        )}
      </div>
    </div>
  );
}
