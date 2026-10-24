'use client';

import React, { useState, useEffect } from 'react';
import { LEGACY_API_URL } from '@/lib/api';

export default function SheetsIntegrationPage() {
  const [isGoogleConnected, setIsGoogleConnected] = useState(false);
  const [isSyncing, setIsSyncing] = useState(false);
  const [token, setToken] = useState<string | null>(null);

  useEffect(() => {
    setToken(localStorage.getItem('sara_tg_token'));
  }, []);

  const connectGoogle = async () => {
    if (!token) return;
    try {
      const res = await fetch(`${LEGACY_API_URL}/agent/integrations/sheets/connect`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: 'My Google Workspace' }),
      });
      const data = await res.json();

      if (data.authorize_url) {
        const width = 600, height = 700;
        const left = window.innerWidth / 2 - width / 2;
        const top = window.innerHeight / 2 - height / 2;
        const authWindow = window.open(
          data.authorize_url,
          'Google Auth',
          `width=${width},height=${height},top=${top},left=${left}`,
        );

        const timer = setInterval(async () => {
          if (authWindow?.closed) {
            clearInterval(timer);
            // Verify the OAuth really completed instead of assuming it did
            try {
              const check = await fetch(`${LEGACY_API_URL}/agent/integrations/sheets/history`, {
                headers: { Authorization: `Bearer ${token}` },
              });
              if (check.ok) setIsGoogleConnected(true);
              else alert('Google connection did not complete. Please try again.');
            } catch (error) {
              alert('Google connection did not complete. Please try again.');
            }
          }
        }, 1000);
      }
    } catch (error) {
      console.error('Auth failed', error);
    }
  };

  const triggerSync = async () => {
    if (!token) return;
    setIsSyncing(true);
    try {
      const res = await fetch(`${LEGACY_API_URL}/agent/integrations/sheets/sync`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target: 'bookings',
          spreadsheet_id: 'AUTO_CREATE',
          direction: 'outbound',
        }),
      });
      if (res.ok) alert('Sync Complete! Data pushed to Google Sheets.');
    } finally {
      setIsSyncing(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="sp-page-head">
        <div>
          <h2 className="sp-page-title">Google Workspace Sync</h2>
          <p className="sp-page-sub">
            Automatically export captured leads and draft bookings to live spreadsheets.
          </p>
        </div>
        <span
          className={`sara-status ${isGoogleConnected ? 'sara-status-ok' : 'sara-status-warn'} ${
            isGoogleConnected ? 'sara-pulse' : ''
          }`}
        >
          <span className="sara-status-dot" aria-hidden="true" />
          {isGoogleConnected ? 'Connected' : 'Requires Authentication'}
        </span>
      </div>

      <section className="sp-card">
        {!isGoogleConnected ? (
          <button
            onClick={connectGoogle}
            className="sara-btn sara-btn-primary sara-focus-ring"
          >
            Authorize Google Account
          </button>
        ) : (
          <div className="sp-row-item">
            <span className="sp-row-text">
              Target: <strong>SARA Booking Assistant Leads</strong>
            </span>
            <button
              onClick={triggerSync}
              disabled={isSyncing}
              className="sara-btn sara-btn-primary sara-focus-ring"
            >
              {isSyncing ? 'Synchronizing…' : 'Force Manual Sync'}
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
