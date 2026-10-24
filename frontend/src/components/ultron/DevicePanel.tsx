'use client';

/**
 * Connected devices + phone pairing.
 *
 * This panel drives a real flow, end to end:
 *
 *   CONNECT DEVICE -> POST /ultron/pairing   (mints a 120s single-use token
 *                                              + a QR rendered by the backend)
 *                 -> the QR encodes /ultron/pair/claim?t=<token>
 *                 -> the device POSTs that token to /ultron/pairing/claim
 *                 -> the backend returns a scoped device token
 *                 -> the device opens WS /ultron/link?token=... and heartbeats
 *
 * The countdown is real (it is derived from the server's expiry, corrected by
 * the browser clock only to keep the second ticking between polls) and it does
 * not reset on re-render. When it reaches zero the panel says the token
 * expired and offers a new one, because pretending an expired token is still
 * live would be the worst possible thing for a pairing screen to do.
 */

import { useEffect, useMemo, useState } from 'react';
import { ULTRON_API_URL, saraFetch } from '@/lib/api';

const API = `${ULTRON_API_URL}/ultron`;

const POLL_MS = 5000;

interface DeviceView {
  device_id: string;
  kind: string;
  label: string;
  platform: string;
  scopes: string[];
  state: 'online' | 'stale' | 'offline' | 'revoked';
  last_seen_age_s: number;
  revoked: boolean;
  revoked_reason: string;
  client: Record<string, string>;
}

interface DevicePayload {
  devices: DeviceView[];
  summary: { total: number; pending_offers: number; by_state: Record<string, number> };
}

interface Offer {
  token: string;
  pair_code: string;
  kind: string;
  label: string;
  payload: string;
  expires_at: number;
  expires_in_s: number;
  qr: { available: boolean; data_url: string | null; error: string | null };
  note: string;
}

/**
 * Whether a machine is currently able to run actions.
 *
 * Derived from the device list rather than fetched from /ultron/execution. That
 * endpoint is API-key protected, so relying on it made the status line depend on
 * a secret being baked into the client - and it silently disappeared whenever
 * that was missing. A `pc_agent` that is online is the same fact, and the device
 * list is already fetched.
 */
function connectedPcAgents(devices: DeviceView[]): DeviceView[] {
  return devices.filter(
    (d) => d.kind === 'pc_agent' && d.state === 'online' && !d.revoked,
  );
}

const STATE_COLOR: Record<DeviceView['state'], string> = {
  online: '#22d3a5',
  stale: '#ffaa00',
  offline: '#5b6b7c',
  revoked: '#ff2d6f',
};

const KIND_GLYPH: Record<string, string> = {
  phone: '▯',
  tablet: '▭',
  desktop: '▤',
  pc_agent: '⌨',
};

/** What each pairing actually grants, so the button labels are not guesses. */
const KIND_BLURB: Record<string, string> = {
  phone: 'Voice + task submit',
  tablet: 'Voice + task submit',
  desktop: 'View tasks only',
  pc_agent: 'Can control THIS PC',
};

export default function DevicePanel({ onDevicesChanged }: { onDevicesChanged?: () => void }) {
  const [payload, setPayload] = useState<DevicePayload | null>(null);
  const [error, setError] = useState('');
  const [offer, setOffer] = useState<Offer | null>(null);
  const [offerError, setOfferError] = useState('');
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);
  // Bumping this re-runs the poll immediately, which is how a revoke shows up
  // in the list without waiting out the interval.
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
                const res = await saraFetch(`${API}/devices`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = (await res.json()) as DevicePayload;
        if (!cancelled) {
          setPayload(body);
          setError('');
        }
      } catch (err: unknown) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
      // PC-control status is polled alongside the device list so the panel can
      // say "this PC is connected" instead of only offering to connect it.
    };
    void poll();
    const timer = window.setInterval(() => void poll(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [reloadToken]);

  // One shared 1 Hz tick. Without it the countdown would only move every poll,
  // and a token expiring is exactly the moment the UI must be accurate.
  useEffect(() => {
    if (!offer) return undefined;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [offer]);

  const remaining = useMemo(() => {
    if (!offer) return 0;
    return Math.max(0, Math.round(offer.expires_at - now));
  }, [offer, now]);

  const expired = Boolean(offer) && remaining === 0;

  const mint = async (kind: string) => {
    setBusy(true);
    setOfferError('');
    try {
      const res = await saraFetch(`${API}/pairing`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, label: `${kind} via command center` }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        setOfferError(
          typeof body?.detail === 'string'
            ? body.detail
            : `Pairing failed (HTTP ${res.status}).`,
        );
        return;
      }
      setOffer(body as Offer);
      setNow(Date.now() / 1000);
    } catch {
      setOfferError('Could not reach the pairing endpoint.');
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (deviceId: string) => {
    setBusy(true);
    try {
      const res = await saraFetch(`${API}/devices/${deviceId}/revoke`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: 'revoked from the command center' }),
      });
      if (!res.ok) setOfferError(`Revoke failed (HTTP ${res.status}).`);
      setReloadToken((n) => n + 1);
      onDevicesChanged?.();
    } finally {
      setBusy(false);
    }
  };

  const devices = payload?.devices ?? [];
  const online = payload?.summary.by_state.online ?? 0;
  const pcAgents = connectedPcAgents(devices);

  return (
    <section className="hud-plate" aria-label="Connected devices">
      <header className="hud-plate__head">
        <h2>CONNECTED DEVICES</h2>
        <span className="hud-tag">
          {payload ? `${online} ONLINE · ${payload.summary.total} PAIRED` : 'LOADING…'}
        </span>
      </header>

      <div className="hud-plate__body">
        {error && <p className="ultron-error">Device registry unavailable: {error}</p>}
        {offerError && <p className="ultron-warn">{offerError}</p>}

        <div className="ultron-row" style={{ marginBottom: 10 }}>
          <button
            type="button"
            className="ultron-btn ultron-btn--primary"
            disabled={busy}
            onClick={() => void mint('phone')}
          >
            ⌸ CONNECT PHONE
          </button>
          <button
            type="button"
            className="ultron-btn ultron-btn--ghost"
            disabled={busy}
            onClick={() => void mint('pc_agent')}
            title={KIND_BLURB.pc_agent}
          >
            ⌨ CONNECT THIS PC
          </button>
          <button
            type="button"
            className="ultron-btn ultron-btn--ghost"
            disabled={busy}
            onClick={() => void mint('desktop')}
            title={KIND_BLURB.desktop}
          >
            ▤ CONNECT VIEWER
          </button>
          {offer && !expired && (
            <button
              type="button"
              className="ultron-btn ultron-btn--ghost"
              onClick={() => setOffer(null)}
            >
              HIDE CODE
            </button>
          )}
        </div>

        <p
          className="hud-tag"
          style={{
            margin: '0 0 12px',
            textTransform: 'none',
            color: pcAgents.length ? '#22d3a5' : '#ffaa00',
          }}
        >
          {pcAgents.length
            ? `PC CONTROL ACTIVE — ${pcAgents
                .map((a) => a.label)
                .join(', ')} can run actions on this machine.`
            : 'PC CONTROL OFF — no agent is connected. Run sara_desktop/local_agent.py on this machine and it pairs itself automatically; nothing to copy.'}
        </p>

        {offer && (
          <div className="hud-pairing" style={{ marginBottom: 12 }}>
            <div className="hud-qr">
              {offer.qr.available && offer.qr.data_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={offer.qr.data_url} alt="Pairing QR code" />
              ) : (
                <div
                  style={{
                    display: 'grid',
                    placeItems: 'center',
                    height: '100%',
                    color: '#b00',
                    fontSize: 10,
                    textAlign: 'center',
                    padding: 6,
                  }}
                >
                  {offer.qr.error ?? 'QR unavailable'}
                </div>
              )}
            </div>
            <div style={{ minWidth: 0 }}>
              <div className="hud-tag">PAIRING TOKEN — SINGLE USE</div>
              <div
                style={{
                  fontFamily: 'var(--sara-font-mono)',
                  fontSize: 15,
                  color: expired ? '#ff2d6f' : '#00f0ff',
                  marginTop: 4,
                }}
              >
                {expired ? 'EXPIRED' : `EXPIRES IN ${remaining}s`}
              </div>
              <p className="hud-tag" style={{ margin: '6px 0 0', textTransform: 'none' }}>
                Scan with the device camera, then POST this token to{' '}
                <code>/ultron/pairing/claim</code>. The token is stored hashed on
                the server and cannot be shown again.
              </p>
              <code className="hud-code">{offer.pair_code}</code>
              <code className="hud-code">{offer.payload}</code>
            </div>
          </div>
        )}

        {devices.length === 0 && !error && (
          <p className="ultron-mono-dim">
            Nothing paired yet. A paired device appears here within one poll of it
            claiming a token, and decays to STALE if it stops heartbeating.
          </p>
        )}

        <div className="hud-devices">
          {devices.map((device) => (
            <article
              key={device.device_id}
              className={`hud-device${device.state === 'online' ? ' hud-device--online' : ''}${
                device.revoked ? ' hud-device--revoked' : ''
              }`}
              style={{ color: STATE_COLOR[device.state] }}
            >
              <span className="hud-device__glyph" aria-hidden="true">
                {KIND_GLYPH[device.kind] ?? '◆'}
              </span>
              <span className="hud-device__label">
                <span style={{ color: 'var(--sara-ink)' }}>{device.label}</span>
                <span className="hud-tag">{device.kind.toUpperCase()}</span>
              </span>
              <span
                className="hud-tag"
                style={{ color: STATE_COLOR[device.state], letterSpacing: '0.14em' }}
              >
                {device.state.toUpperCase()}
              </span>
              <span className="hud-device__meta">
                {device.platform && `${device.platform} · `}
                {device.scopes.length} scopes · seen {device.last_seen_age_s}s ago
                {device.client?.app ? ` · ${device.client.app}` : ''}
                {device.revoked ? ` · ${device.revoked_reason}` : ''}
              </span>
              {!device.revoked && (
                <button
                  type="button"
                  className="ultron-btn ultron-btn--danger"
                  style={{ gridColumn: '2 / -1', justifySelf: 'end', padding: '4px 9px' }}
                  disabled={busy}
                  onClick={() => void revoke(device.device_id)}
                >
                  REVOKE
                </button>
              )}
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
