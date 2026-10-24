'use client';

import React from 'react';

export default function SettingsPage() {
  return (
    <div className="space-y-6">
      <div className="sp-page-head">
        <div>
          <h2 className="sp-page-title">Workspace Settings</h2>
          <p className="sp-page-sub">
            Manage your SARA AI tenant preferences and global configurations.
          </p>
        </div>
      </div>

      <div className="sp-card space-y-6">
        <label className="block">
          <span className="sp-field-label">Workspace Name</span>
          <input type="text" defaultValue="SARA Ecosystem" className="sara-input" />
        </label>
        <label className="block">
          <span className="sp-field-label">Administrator Email</span>
          <input type="email" defaultValue="admin@sara.ai" className="sara-input" />
        </label>
        <div className="pt-4 border-t border-[var(--sara-line)]">
          <button className="sara-btn sara-btn-primary sara-focus-ring">
            Save Preferences
          </button>
        </div>
      </div>
    </div>
  );
}
