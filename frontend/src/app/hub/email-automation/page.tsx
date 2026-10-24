'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { Apple } from "lucide-react";

const AURA_STYLES = `
  .liquid-glass {
    background: rgba(255,255,255,0.01);
    background-blend-mode: luminosity;
    backdrop-filter: blur(4px);
    border: none;
    box-shadow: inset 0 1px 1px rgba(255,255,255,0.1);
    position: relative; overflow: hidden;
  }
  .liquid-glass::before {
    content: ''; position: absolute; inset: 0; border-radius: inherit;
    padding: 1.4px;
    background: linear-gradient(180deg,
      rgba(255,255,255,0.45) 0%, rgba(255,255,255,0.15) 20%,
      rgba(255,255,255,0) 40%, rgba(255,255,255,0) 60%,
      rgba(255,255,255,0.15) 80%, rgba(255,255,255,0.45) 100%);
    -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude;
    pointer-events: none;
  }
  @keyframes shiny {
    0% { background-position: -200% center; }
    100% { background-position: 200% center; }
  }
  .animate-shiny {
    animation: shiny 6s linear infinite;
  }
`;

function AuraBackground() {
  return (
    <>
      <style>{AURA_STYLES}</style>
      <svg className="hidden">
        <filter id="c3-noise">
          <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" stitchTiles="stitch" />
          <feColorMatrix type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 0.35 0" />
          <feComposite in2="SourceGraphic" operator="in" result="noise" />
          <feBlend in="SourceGraphic" in2="noise" mode="multiply" />
        </filter>
      </svg>
      <div className="fixed inset-0 z-0 pointer-events-none">
        <video autoPlay loop muted playsInline
          className="w-full h-full object-cover pointer-events-none opacity-50"
          src="https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260508_064122_c4750c0e-7476-4b44-94a2-a85a65c63bf2.mp4" />
      </div>
      <div className="hidden md:block pointer-events-none fixed inset-y-0 left-1/2 -translate-x-[calc(50%+36rem)] w-px bg-white/10 z-[5]" />
      <div className="hidden md:block pointer-events-none fixed inset-y-0 left-1/2 translate-x-[calc(-50%+36rem)] w-px bg-white/10 z-[5]" />
    </>
  );
}

function AppleButton({ label, onClick, className }: any) {
  return (
    <button onClick={onClick} className={`group inline-flex items-center justify-center gap-2 rounded-full bg-white text-black font-medium text-sm px-5 py-3 transition-all hover:bg-white/90 active:scale-[0.98] ${className || ''}`}>
      <Apple className="w-4 h-4" />
      {label}
      <svg className="w-4 h-4 transition-transform group-hover:translate-x-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" /></svg>
    </button>
  );
}

import { SARA_API_URL, saraFetch } from '@/lib/api';

/* ============================================================================
   Types
   ========================================================================== */

interface Health {
  status: string;
  agent: string;
  llm: boolean;
  smtp: boolean;
  smtp_sender: boolean;
  smtp_password: boolean;
  smtp_from?: string;
  gmail_api: boolean;
  gmail_connected?: boolean;
  gmail_email?: string;
  sheets_service_account: boolean;
  running_campaigns: string[];
}

const canSendLive = (health: Health | null, provider: string | undefined) =>
  provider === 'gmail'
    ? !!health?.gmail_api && !!health?.gmail_connected
    : !!health?.smtp;

interface Campaign {
  id: string;
  name: string;
  status: string;
  context: string;
  instructions: string;
  subject: string;
  tone: string;
  objective: string;
  cta: string;
  signature: string;
  provider: string;
  dry_run: boolean;
  test_email: string | null;
  max_emails: number;
  delay_seconds: number;
  max_retries: number;
  sheet_id: string | null;
  sheet_range: string;
  sent_count: number;
  failed_count: number;
  counts: Record<string, number>;
  live: boolean;
}

interface ClientRow {
  id: string;
  name: string;
  email: string;
  company: string;
  source: string;
  status: string;
  attempts: number;
  error: string;
  message_id: string;
  sent_at: string | null;
}

interface ContextFile {
  id: string;
  filename: string;
  chars: number;
  created_at: string;
}

interface AttachmentRow {
  id: string;
  filename: string;
  mime: string;
  size: number;
  created_at: string;
}

interface SenderStatus {
  smtp: {
    configured: boolean;
    from_email: string;
    from_name: string;
    host: string;
    port: number;
    ssl: boolean;
    source: 'saved' | 'env' | null;
  };
  gmail: { available: boolean; connected: boolean; email: string };
  app_passwords_url: string;
}

type Toast = { kind: 'ok' | 'err' | 'info'; text: string } | null;

const API = `${SARA_API_URL}/email-agent`;

const fmtBytes = (n: number) =>
  n >= 1024 * 1024 ? `${(n / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;

/* "Connect with Google": full browser redirect to Google's own consent page.
   SARA never handles the password; Google returns a one-time code to
   /auth/callback which stores the token server-side. `state` carries the page
   to return to after consent. */
const googleConnectUrl = (next: string) => {
  const clientId = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID;
  const redirectUri =
    process.env.NEXT_PUBLIC_GMAIL_REDIRECT_URI ||
    (typeof window !== 'undefined' ? `${window.location.origin}/auth/callback` : '');
  const scope = encodeURIComponent('openid email https://www.googleapis.com/auth/gmail.send');
  return `https://accounts.google.com/o/oauth2/v2/auth?client_id=${clientId}&redirect_uri=${redirectUri}&response_type=code&scope=${scope}&access_type=offline&prompt=consent&state=${encodeURIComponent(next)}`;
};

/* ============================================================================
   Small UI atoms
   ========================================================================== */

const STATUS_STYLES: Record<string, string> = {
  Pending: 'bg-slate-500/10 text-white/80 border-slate-500/30',
  Sending: 'bg-amber-500/10 text-amber-300 border-amber-500/30 animate-pulse',
  Sent: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30',
  Failed: 'bg-rose-500/10 text-rose-300 border-rose-500/30',
  Skipped: 'bg-slate-700/20 text-white/60 border-slate-600/30',
};

function StatusPill({ status }: { status: string }) {
  return (
    <span className={`px-2 py-0.5 rounded-full text-[10px] font-mono border ${STATUS_STYLES[status] ?? STATUS_STYLES.Pending}`}>
      {status.toUpperCase()}
    </span>
  );
}

function Chip({ label, ok, hint }: { label: string; ok: boolean; hint?: string }) {
  return (
    <div
      className="group relative flex items-center gap-2 px-3 py-1.5 rounded-xl bg-black/40 border border-white/10"
      title={hint}
    >
      <span className={`h-2 w-2 rounded-full ${ok ? 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]' : 'bg-rose-500'}`} />
      <span className="text-[11px] font-mono text-white/80">{label}</span>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-[10px] font-mono uppercase tracking-widest text-white/40 mb-1.5">{label}</span>
      {children}
    </label>
  );
}

const inputCls =
  'w-full rounded-xl bg-black/50 border border-white/10 px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-white/40 focus:ring-1 focus:ring-white/20 transition-all';

/* ============================================================================
   3D canvas wrapper
   ========================================================================== */

function EmailSceneCanvas() {
  const mountRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let disposed = false;
    let api: { dispose(): void } | null = null;
    (async () => {
      const { createEmailScene } = await import('@/lib/emailScene');
      if (disposed || !mountRef.current) return;
      api = createEmailScene(mountRef.current);
    })();
    return () => {
      disposed = true;
      api?.dispose();
    };
  }, []);

  return <div ref={mountRef} className="absolute inset-0 z-0 pointer-events-auto" />;
}

/* ============================================================================
   Main page
   ========================================================================== */

export default function EmailAutomationPage() {
  /* ---------------- state ---------------- */
  const [health, setHealth] = useState<Health | null>(null);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [view, setView] = useState<'dashboard' | 'workspace'>('dashboard');
  const [workspaceTab, setWorkspaceTab] = useState<'memory' | 'ingestion' | 'queue'>('queue');
  const [active, setActive] = useState<Campaign | null>(null);
  const [clients, setClients] = useState<ClientRow[]>([]);
  const [contextFiles, setContextFiles] = useState<ContextFile[]>([]);
  const [attachments, setAttachments] = useState<AttachmentRow[]>([]);
  const [statusFilter, setStatusFilter] = useState('');
  const [toast, setToast] = useState<Toast>(null);

  // sender connection ("Connect your email")
  const [sender, setSender] = useState<SenderStatus | null>(null);
  const [smtpForm, setSmtpForm] = useState({ from_email: '', from_name: '', app_password: '', host: '', port: '' });
  const [showAdvanced, setShowAdvanced] = useState(false);

  // create form
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({
    name: 'Q4 Outreach', subject: '', objective: '', cta: '', signature: '',
    tone: 'professional', provider: 'smtp', test_email: '', delay_seconds: 2,
  });

  // memory editor
  const [contextDraft, setContextDraft] = useState('');
  const [instructionsDraft, setInstructionsDraft] = useState('');

  // sheets
  const [sheetId, setSheetId] = useState('');
  const [sheetRange, setSheetRange] = useState('Sheet1!A:Z');

  // preview / test
  const [preview, setPreview] = useState<{
    subject: string;
    body: string;
    to: string;
    attachments?: { filename: string; size: number }[];
  } | null>(null);
  const [testEmail, setTestEmail] = useState('');
  const [busy, setBusy] = useState<string>('');

  const notify = useCallback((kind: 'ok' | 'err' | 'info', text: string) => {
    setToast({ kind, text });
    setTimeout(() => setToast(null), 4200);
  }, []);

  /* ---------------- data loading ---------------- */

  const loadHealth = useCallback(async () => {
    try {
      const res = await saraFetch(`${API}/health`);
      if (res.ok) setHealth(await res.json());
    } catch { /* backend offline */ }
  }, []);

  const loadCampaigns = useCallback(async () => {
    try {
      const res = await saraFetch(`${API}/campaigns`);
      if (res.ok) {
        const data: Campaign[] = await res.json();
        setCampaigns(data);
        setActive((prev) => (prev ? data.find((c) => c.id === prev.id) ?? null : data[0] ?? null));
      }
    } catch { /* backend offline */ }
  }, []);

  const loadClients = useCallback(async (campaignId: string, filter: string) => {
    try {
      const res = await saraFetch(`${API}/campaigns/${campaignId}/clients${filter ? `?status=${filter}` : ''}`);
      if (res.ok) setClients(await res.json());
    } catch { /* backend offline */ }
  }, []);

  const loadContextFiles = useCallback(async (campaignId: string) => {
    try {
      const res = await saraFetch(`${API}/campaigns/${campaignId}/context/files`);
      if (res.ok) setContextFiles(await res.json());
    } catch { /* backend offline */ }
  }, []);

  const loadAttachments = useCallback(async (campaignId: string) => {
    try {
      const res = await saraFetch(`${API}/campaigns/${campaignId}/attachments`);
      if (res.ok) setAttachments(await res.json());
    } catch { /* backend offline */ }
  }, []);

  const loadSender = useCallback(async () => {
    try {
      const res = await saraFetch(`${API}/sender`);
      if (res.ok) {
        const data: SenderStatus = await res.json();
        setSender(data);
        // Prefill the form with whichever sender is already connected, but only
        // when the user has not typed their own value yet.
        setSmtpForm((f) => (f.from_email ? f : { ...f, from_email: data.smtp?.from_email || '' }));
      }
    } catch { /* backend offline */ }
  }, []);

  useEffect(() => {
    loadHealth();
    loadCampaigns();
    loadSender();
  }, [loadHealth, loadCampaigns, loadSender]);

  useEffect(() => {
    if (!active) return;
    setContextDraft(active.context || '');
    setInstructionsDraft(active.instructions || '');
    setSheetId(active.sheet_id || '');
    setSheetRange(active.sheet_range || 'Sheet1!A:Z');
    loadClients(active.id, statusFilter);
    loadContextFiles(active.id);
    loadAttachments(active.id);
  }, [active?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // poll while a campaign is live
  useEffect(() => {
    if (!active?.live) return;
    const t = setInterval(() => {
      loadCampaigns();
      if (active) loadClients(active.id, statusFilter);
      loadHealth();
    }, 4000);
    return () => clearInterval(t);
  }, [active?.live, active?.id, statusFilter, loadCampaigns, loadClients, loadHealth]);

  /* ---------------- actions ---------------- */

  async function createCampaign(e: React.FormEvent) {
    e.preventDefault();
    setBusy('create');
    try {
      const res = await saraFetch(`${API}/campaigns`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: form.name || 'Campaign',
          subject: form.subject,
          objective: form.objective,
          cta: form.cta,
          signature: form.signature,
          tone: form.tone,
          provider: form.provider,
          test_email: form.test_email || null,
          delay_seconds: Number(form.delay_seconds) || 2,
          dry_run: true,
        }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? 'Create failed');
      const { id } = await res.json();
      notify('ok', 'Campaign created in safe dry-run mode');
      setShowCreate(false);
      await loadCampaigns();
      const fresh = await saraFetch(`${API}/campaigns/${id}`);
      if (fresh.ok) setActive(await fresh.json());
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function saveMemory() {
    if (!active) return;
    setBusy('memory');
    try {
      await saraFetch(`${API}/campaigns/${active.id}/context`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: contextDraft }),
      });
      await saraFetch(`${API}/campaigns/${active.id}/instructions`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: instructionsDraft }),
      });
      notify('ok', 'Campaign memory saved');
      loadCampaigns();
    } catch (err) {
      notify('err', String(err));
    } finally {
      setBusy('');
    }
  }

  async function uploadClientsFile(file: File) {
    if (!active) return;
    setBusy('upload');
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await saraFetch(`${API}/campaigns/${active.id}/upload`, { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Upload failed');
      if (data.added === 0) {
        notify('info', data.diagnosis ?? 'No valid emails found in the file');
      } else {
        notify('ok', `Imported ${data.added} clients (${data.duplicates} duplicates skipped)`);
      }
      loadClients(active.id, statusFilter);
      loadCampaigns();
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function uploadContextFile(file: File) {
    if (!active) return;
    setBusy('ctx');
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await saraFetch(`${API}/campaigns/${active.id}/context/upload`, { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Upload failed');
      notify('ok', `Memory file "${data.filename}" stored (${data.chars} chars)`);
      loadContextFiles(active.id);
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function uploadAttachment(file: File) {
    if (!active) return;
    setBusy('attach');
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await saraFetch(`${API}/campaigns/${active.id}/attachments`, { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Attachment upload failed');
      notify('ok', `"${data.filename}" will be attached to every email`);
      loadAttachments(active.id);
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function removeAttachment(id: string, filename: string) {
    if (!active) return;
    setBusy(`att-${id}`);
    try {
      const res = await saraFetch(`${API}/campaigns/${active.id}/attachments/${id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? 'Delete failed');
      notify('info', `Removed "${filename}"`);
      loadAttachments(active.id);
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function connectSenderSmtp(e: React.FormEvent) {
    e.preventDefault();
    setBusy('sender');
    try {
      const res = await saraFetch(`${API}/sender/smtp`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          from_email: smtpForm.from_email.trim(),
          from_name: smtpForm.from_name.trim(),
          password: smtpForm.app_password,
          host: smtpForm.host.trim(),
          port: Number(smtpForm.port) || 0,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Connection failed');
      notify('ok', `Sender connected & verified: ${data.from_email}`);
      setSmtpForm((f) => ({ ...f, app_password: '' }));
      loadSender();
      loadHealth();
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function disconnectSender() {
    if (!confirm('Disconnect this sender? Emails cannot send until you connect again.')) return;
    setBusy('sender');
    try {
      const res = await saraFetch(`${API}/sender/smtp/disconnect`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Disconnect failed');
      notify('info', data.env_fallback
        ? 'Disconnected — falling back to backend/.env credentials'
        : 'Sender disconnected');
      loadSender();
      loadHealth();
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function switchProvider(provider: string) {
    if (!active) return;
    setBusy('provider');
    try {
      const res = await saraFetch(`${API}/campaigns/${active.id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Could not switch provider');
      notify('ok', provider === 'gmail'
        ? 'Campaign will send via the connected Google account (Gmail API)'
        : 'Campaign will send via SMTP');
      loadCampaigns();
      loadHealth();
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function connectSheet(e: React.FormEvent) {
    e.preventDefault();
    if (!active || !sheetId.trim()) return;
    setBusy('sheet');
    try {
      const res = await saraFetch(`${API}/campaigns/${active.id}/sheet`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ spreadsheet_id: sheetId.trim(), range_name: sheetRange || 'Sheet1!A:Z' }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Sheet connection failed');
      notify('ok', `Sheet connected — ${data.added} clients imported, ${data.duplicates} duplicates`);
      loadClients(active.id, statusFilter);
      loadCampaigns();
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function doPreview(client: ClientRow) {
    if (!active) return;
    setBusy(`preview-${client.id}`);
    try {
      const res = await saraFetch(`${API}/campaigns/${active.id}/preview`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_id: client.id }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Preview failed');
      setPreview(data);
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function doTestSend() {
    if (!active) return;
    setBusy('test');
    try {
      const res = await saraFetch(`${API}/campaigns/${active.id}/test-send`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: testEmail || active.test_email || null }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Test send failed');
      const attCount = (data.attachments ?? []).length;
      const attNote = attCount > 0 ? ` with ${attCount} attachment${attCount > 1 ? 's' : ''}` : '';
      notify(data.mode === 'dry-run' ? 'info' : 'ok', data.mode === 'dry-run'
        ? `DRY-RUN — nothing was sent. Preview generated for ${data.to}`
        : `Test email DELIVERED to ${data.to}${attNote}`);
      setPreview({ subject: data.subject ?? 'Test', body: data.body, to: data.to, attachments: data.attachments });
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function goLive() {
    if (!active) return;
    // A leftover test redirect is the #1 reason "every email comes to me" —
    // offer to clear it right at the moment of switching to live.
    const clearRedirect = !!active.test_email && confirm(
      `⚠ TEST INBOX REDIRECT is active: ${active.test_email}\n\n` +
      'OK  = remove it and deliver to each client\'s own email address\n' +
      'Cancel = keep redirecting every email to the test inbox'
    );
    if (active.test_email && !clearRedirect && !confirm(
      `Switch "${active.name}" to LIVE sending?\n\n` +
      `⚠ Every email will still be redirected to the test inbox ${active.test_email}\n` +
      `Provider: ${active.provider === 'gmail' ? 'Gmail API' : 'SMTP'} · Delay: ${active.delay_seconds}s between sends\n\n` +
      'Continue?'
    )) return;
    setBusy('live');
    try {
      const res = await saraFetch(`${API}/campaigns/${active.id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(clearRedirect ? { dry_run: false, test_email: null } : { dry_run: false }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Could not switch to live');
      notify('ok', clearRedirect
        ? 'Campaign is LIVE — test redirect removed, emails go to each client'
        : 'Campaign is now LIVE — real emails will be delivered');
      loadCampaigns();
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function runAction(action: 'start' | 'pause' | 'resume' | 'retry-failed' | 'sheet/sync') {
    if (!active) return;
    setBusy(action);
    try {
      const res = await saraFetch(`${API}/campaigns/${active.id}/${action}`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? `${action} failed`);
      notify('ok', data.status ?? 'done');
      loadCampaigns();
      loadClients(active.id, statusFilter);
      loadHealth();
    } catch (err) {
      notify('err', String(err instanceof Error ? err.message : err));
    } finally {
      setBusy('');
    }
  }

  async function validateNow() {
    if (!active) return;
    setBusy('validate');
    try {
      const res = await saraFetch(`${API}/campaigns/${active.id}/validate`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? 'Validation failed');
      notify(data.invalid + data.duplicates === 0 ? 'ok' : 'info',
        `${data.valid} valid / ${data.invalid} invalid / ${data.duplicates} duplicates`);
    } catch (err) {
      notify('err', String(err));
    } finally {
      setBusy('');
    }
  }

  /* ---------------- derived ---------------- */

  const counts = active?.counts ?? {};
  const total = useMemo(() => Object.values(counts).reduce((a, b) => a + b, 0), [counts]);
  const progress = total > 0 ? Math.round(((counts.Sent ?? 0) / total) * 100) : 0;

  /* ================= render ================= */

  return (
    <main className="relative min-h-screen w-full bg-[#0c0c0c] text-white" style={{ fontFamily: "'Inter', system-ui, sans-serif" }}>
      <AuraBackground />
      
      {/* Aura Top Bar */}
      <div className="relative z-20 w-full h-10 bg-black/40 backdrop-blur-md border-b border-white/10 flex items-center justify-between px-6 text-xs text-white/70">
        <div className="flex items-center gap-4 font-medium">
          <Apple className="w-3.5 h-3.5 text-white" />
          <span className="font-bold text-white">Aura Core</span>
        </div>
        <div className="flex items-center gap-4">
           <span>{new Date().toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</span>
        </div>
      </div>

      <div className="relative z-10 mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 py-10">
        
        {/* HERO SECTION */}
        <div className="text-center mb-16 flex flex-col items-center">
           <h1 className="text-4xl md:text-6xl font-semibold tracking-tight leading-[0.9]">
             Your dispatch.<br/>
             <span className="animate-shiny bg-clip-text text-transparent inline-block mt-2 pb-2" style={{ backgroundImage: "linear-gradient(to right, #091020 0%, #0B2551 12.5%, #A4F4FD 32.5%, #00d2ff 50%, #0B2551 67.5%, #091020 87.5%, #091020 100%)", backgroundSize: "200% auto", filter: "url(#c3-noise)" }}>
               Revitalized
             </span>
           </h1>
           <p className="mt-6 text-white/60 max-w-xl text-base leading-[1.5]">
             The premier inbox automation platform for the current era. It leverages powerful AI to orchestrate, dispatch, and monitor your email campaigns with absolute clarity.
           </p>
        </div>

        {/* INBOX MOCKUP INTERFACE (FULLY EXPANDABLE) */}
        <section className="relative rounded-2xl border border-white/10 bg-[#0e1014]/90 backdrop-blur-2xl shadow-2xl flex flex-col mb-32 overflow-hidden">
          {/* Mac window title bar */}
          <div className="h-10 bg-black/50 border-b border-white/10 flex items-center px-4 shrink-0 relative">
             <div className="flex gap-2">
                <div className="w-3 h-3 rounded-full bg-[#ff5f57]" />
                <div className="w-3 h-3 rounded-full bg-[#febc2e]" />
                <div className="w-3 h-3 rounded-full bg-[#28c840]" />
             </div>
             <div className="absolute inset-0 flex items-center justify-center text-xs text-white/50 font-medium pointer-events-none">
                Aura — Dispatch Console
             </div>
          </div>

          <div className="flex-1 grid grid-cols-12 min-h-[800px]">
             
             {/* LEFT SIDEBAR: Campaigns */}
             <div className="col-span-12 md:col-span-3 border-b md:border-b-0 md:border-r border-white/10 bg-black/30 flex flex-col">
                <div className="p-4 border-b border-white/10 shrink-0">
                   <button 
                     onClick={() => setShowCreate(true)} 
                     className="w-full flex items-center justify-center gap-2 rounded-lg bg-white text-black text-xs font-semibold px-3 py-2.5 transition-colors hover:bg-white/90"
                   >
                     <span className="text-[14px]">✦</span> Compose Campaign
                   </button>
                </div>
                
                <div className="flex-1 p-3 space-y-1">
                   <div className="text-[10px] font-mono text-white/30 uppercase tracking-widest px-3 mb-2 mt-2">Campaigns</div>
                   {campaigns.map(c => (
                     <button
                       key={c.id}
                       onClick={() => setActive(c)}
                       className={`w-full text-left px-3 py-2 rounded-lg text-sm transition-all ${active?.id === c.id ? 'bg-white/10 text-white shadow-sm' : 'text-white/60 hover:bg-white/5 hover:text-white/90'}`}
                     >
                       <div className="truncate font-medium">{c.name}</div>
                     </button>
                   ))}
                </div>

                <div className="p-4 border-t border-white/10 shrink-0 bg-black/40">
                   <div className="text-[10px] font-mono uppercase tracking-widest text-white/40 mb-3">System Health</div>
                   <div className="flex flex-col gap-2">
                     <Chip label="LLM Engine" ok={!!health?.llm} />
                     <Chip label="SMTP/Gmail" ok={canSendLive(health, active?.provider)} />
                     <Chip label="Sheets Sync" ok={!!health?.sheets_service_account} />
                   </div>
                </div>
             </div>

             {/* MIDDLE COLUMN: Dispatch Queue */}
             <div className="col-span-12 md:col-span-4 border-b md:border-b-0 md:border-r border-white/10 bg-[#0e1014]/50 flex flex-col">
                <div className="p-4 border-b border-white/10 shrink-0 space-y-3">
                   <div className="flex items-center gap-1 overflow-x-auto scrollbar-none pb-1">
                      {['All', 'Pending', 'Sending', 'Sent', 'Failed'].map(f => (
                        <button
                          key={f}
                          onClick={() => setStatusFilter(f === 'All' ? '' : f)}
                          className={`px-2.5 py-1 rounded-md text-[11px] whitespace-nowrap transition-colors ${(statusFilter === f || (f === 'All' && !statusFilter)) ? 'bg-white/15 text-white' : 'text-white/50 hover:bg-white/5 hover:text-white/80'}`}
                        >
                          {f}
                        </button>
                      ))}
                   </div>
                </div>

                <div className="flex-1">
                   {clients.length === 0 ? (
                     <div className="p-8 text-center text-sm text-white/30">No clients in queue</div>
                   ) : (
                     clients.map(client => (
                       <div key={client.id} className="p-4 border-b border-white/5 hover:bg-white/5 transition-colors cursor-pointer group">
                         <div className="flex justify-between items-start mb-1">
                           <div className="font-medium text-sm text-white/90 truncate pr-4">{client.name || 'Unnamed'}</div>
                           <div className="shrink-0"><StatusPill status={client.status} /></div>
                         </div>
                         <div className="text-xs text-white/50 truncate mb-1">{client.email}</div>
                         {client.error && <div className="text-[10px] text-rose-400 mt-1 line-clamp-1">{client.error}</div>}
                       </div>
                     ))
                   )}
                </div>
             </div>

             {/* RIGHT COLUMN: Campaign Reader / Controls */}
             <div className="col-span-12 md:col-span-5 flex flex-col bg-[#0e1014]/20">
                {active ? (
                  <>
                    {/* Toolbar */}
                    <div className="h-14 border-b border-white/10 flex items-center justify-between px-4 shrink-0 bg-black/20">
                       <div className="flex items-center gap-2">
                          <button onClick={() => active.live ? runAction('pause') : runAction('start')} className={`px-3 py-1.5 rounded-md text-xs font-medium flex items-center gap-2 transition-colors ${active.live ? 'bg-amber-500/20 text-amber-300 hover:bg-amber-500/30' : 'bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30'}`}>
                            {active.live ? '⏸ Pause Dispatch' : '▶ Start Dispatch'}
                          </button>
                          <button onClick={validateNow} className="px-3 py-1.5 rounded-md text-xs font-medium bg-white/10 text-white hover:bg-white/20 transition-colors">
                            Validate Run
                          </button>
                          <button onClick={() => runAction('retry-failed')} className="px-3 py-1.5 rounded-md text-xs font-medium bg-white/5 text-white/70 hover:bg-white/20 hover:text-white transition-colors">
                            ⟲ Retry Failed
                          </button>
                       </div>
                       <div className="text-xs text-white/50 flex items-center gap-2">
                          <span>{progress}%</span>
                          <div className="w-24 h-1.5 bg-white/10 rounded-full overflow-hidden">
                             <div className="h-full bg-white/70 rounded-full transition-all duration-500" style={{ width: `${progress}%` }} />
                          </div>
                       </div>
                    </div>
                    
                    {/* Header */}
                    <div className="p-6 border-b border-white/10 shrink-0">
                       <h2 className="text-2xl font-semibold mb-2">{active.name}</h2>
                       <div className="flex items-center gap-2 text-xs text-white/50">
                          <span className="uppercase tracking-widest text-white/30">{active.provider}</span>
                          <span>•</span>
                          <span>{total} targets queued</span>
                          {active.dry_run && <span className="px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/20 ml-2">DRY RUN</span>}
                       </div>
                    </div>

                    {/* Scrollable Body (Now allows full document scroll) */}
                    <div className="flex-1 p-6 space-y-8">
                       
                       {/* Section: Sender — connect your email */}
                       <div className="liquid-glass rounded-xl p-5 border border-white/5">
                          <div className="flex items-center justify-between gap-3 mb-4">
                            <div className="flex items-center gap-2">
                               <span className="text-[#A4F4FD]">✉</span>
                               <h3 className="text-sm font-semibold">Sender — Connect your email</h3>
                            </div>
                            <select
                              value={active.provider}
                              disabled={active.live || !!busy}
                              onChange={e => switchProvider(e.target.value)}
                              className="rounded-lg bg-black/50 border border-white/10 px-2 py-1 text-xs text-white focus:outline-none disabled:opacity-50"
                              title="How this campaign sends email"
                            >
                              <option value="smtp">SMTP / app password</option>
                              <option value="gmail">Gmail API (Google)</option>
                            </select>
                          </div>

                          {active.provider === 'gmail' ? (
                            <div className="space-y-2">
                              {sender?.gmail.connected ? (
                                <div className="flex items-center justify-between gap-3 rounded-xl bg-emerald-500/10 border border-emerald-500/20 px-4 py-3">
                                  <div className="min-w-0">
                                    <div className="text-xs text-emerald-200 font-medium truncate">
                                      Connected as {sender.gmail.email || 'your Google account'}
                                    </div>
                                    <div className="text-[10px] text-emerald-200/60 mt-0.5">
                                      Sends through the Gmail API — no password stored, revoke anytime in your Google account.
                                    </div>
                                  </div>
                                  <button
                                    onClick={() => { window.location.href = googleConnectUrl('/hub/email-automation'); }}
                                    className="shrink-0 text-[11px] font-medium px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/20 border border-white/20"
                                  >
                                    Reconnect
                                  </button>
                                </div>
                              ) : (
                                <button
                                  onClick={() => { window.location.href = googleConnectUrl('/hub/email-automation'); }}
                                  disabled={!sender?.gmail.available}
                                  className="w-full flex items-center justify-center gap-2 rounded-lg bg-white text-black text-xs font-semibold px-3 py-2.5 hover:bg-white/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                                >
                                  Connect with Google
                                </button>
                              )}
                              <p className="text-[10px] text-white/40 leading-relaxed">
                                Opens Google&apos;s own sign-in page in your browser — choose the account to send from. SARA never sees your password.
                                {sender && !sender.gmail.available && ' (Google OAuth is not configured on the backend yet — use the SMTP option instead.)'}
                              </p>
                            </div>
                          ) : (
                            <div className="space-y-3">
                              {sender?.smtp.configured && (
                                <div className="flex items-center justify-between gap-3 rounded-xl bg-emerald-500/10 border border-emerald-500/20 px-4 py-3">
                                  <div className="min-w-0">
                                    <div className="text-xs text-emerald-200 font-medium truncate">
                                      Connected: {sender.smtp.from_email}
                                    </div>
                                    <div className="text-[10px] text-emerald-200/60 mt-0.5">
                                      {sender.smtp.host}:{sender.smtp.port} · {sender.smtp.source === 'saved' ? 'saved here' : 'from backend/.env'}
                                    </div>
                                  </div>
                                  {sender.smtp.source === 'saved' && (
                                    <button
                                      onClick={disconnectSender}
                                      disabled={!!busy}
                                      className="shrink-0 text-[11px] font-medium px-3 py-1.5 rounded-lg bg-white/10 hover:bg-rose-500/20 text-white/80 border border-white/20 disabled:opacity-50"
                                    >
                                      Disconnect
                                    </button>
                                  )}
                                </div>
                              )}
                              <form onSubmit={connectSenderSmtp} className="space-y-3">
                                <div className="grid grid-cols-2 gap-3">
                                  <Field label="From email">
                                    <input type="email" required value={smtpForm.from_email}
                                      onChange={e => setSmtpForm({ ...smtpForm, from_email: e.target.value })}
                                      placeholder="you@gmail.com" className={inputCls} />
                                  </Field>
                                  <Field label="App password">
                                    <input type="password" required value={smtpForm.app_password}
                                      onChange={e => setSmtpForm({ ...smtpForm, app_password: e.target.value })}
                                      placeholder="abcd efgh ijkl mnop" className={inputCls} />
                                  </Field>
                                </div>
                                <div className="flex items-center justify-between">
                                  <button type="button" onClick={() => setShowAdvanced(v => !v)}
                                    className="text-[10px] font-mono uppercase tracking-widest text-white/40 hover:text-white/70">
                                    {showAdvanced ? '▾' : '▸'} Advanced (name / host / port)
                                  </button>
                                  <a href={sender?.app_passwords_url || 'https://myaccount.google.com/apppasswords'}
                                    target="_blank" rel="noreferrer"
                                    className="text-[11px] text-[#A4F4FD]/80 hover:text-[#A4F4FD] underline underline-offset-2">
                                    Get a Google app password ↗
                                  </a>
                                </div>
                                {showAdvanced && (
                                  <div className="grid grid-cols-3 gap-3">
                                    <Field label="Display name">
                                      <input value={smtpForm.from_name}
                                        onChange={e => setSmtpForm({ ...smtpForm, from_name: e.target.value })}
                                        placeholder="SARA AI" className={inputCls} />
                                    </Field>
                                    <Field label="SMTP host">
                                      <input value={smtpForm.host}
                                        onChange={e => setSmtpForm({ ...smtpForm, host: e.target.value })}
                                        placeholder="auto for @gmail.com" className={inputCls} />
                                    </Field>
                                    <Field label="Port">
                                      <input value={smtpForm.port}
                                        onChange={e => setSmtpForm({ ...smtpForm, port: e.target.value })}
                                        placeholder="auto 587 / 465" className={inputCls} />
                                    </Field>
                                  </div>
                                )}
                                <div className="flex justify-end">
                                  <button type="submit"
                                    disabled={!!busy || !smtpForm.from_email || !smtpForm.app_password}
                                    className="px-4 py-2 rounded-lg bg-white text-black text-xs font-semibold hover:bg-white/90 disabled:opacity-40">
                                    {busy === 'sender' ? 'Verifying…' : 'Save & verify'}
                                  </button>
                                </div>
                                <p className="text-[10px] text-white/40 leading-relaxed">
                                  Gmail: turn on 2-Step Verification → create an App password → paste it here. Any other provider: fill host & port under Advanced. Save &amp; verify signs in once to prove the mailbox works.
                                </p>
                              </form>
                            </div>
                          )}
                       </div>

                       {/* Section: Memory */}
                       <div className="liquid-glass rounded-xl p-5 border border-white/5">
                          <div className="flex items-center gap-2 mb-4">
                             <span className="text-[#A4F4FD]">✦</span>
                             <h3 className="text-sm font-semibold">Campaign Memory</h3>
                          </div>
                          <div className="space-y-4">
                            <Field label="Instructions (Agent Prompt)">
                              <textarea
                                value={instructionsDraft}
                                onChange={e => setInstructionsDraft(e.target.value)}
                                className={inputCls}
                                rows={3}
                                placeholder="e.g. Keep it under 150 words..."
                              />
                            </Field>
                            <Field label="Context (Background Info)">
                              <textarea
                                value={contextDraft}
                                onChange={e => setContextDraft(e.target.value)}
                                className={inputCls}
                                rows={4}
                                placeholder="Paste product details here..."
                              />
                            </Field>

                            {/* CONTEXT PDF UPLOAD RESTORED */}
                            <div className="pt-2">
                               <label className="block border-2 border-dashed border-white/10 rounded-xl p-6 text-center cursor-pointer hover:border-white/30 hover:bg-white/5 transition-all">
                                  <input type="file" className="hidden" onChange={e => e.target.files && uploadContextFile(e.target.files[0])} accept=".txt,.md,.pdf" />
                                  <div className="text-sm font-medium">Memory File (.txt / .md / .pdf)</div>
                                  <div className="text-xs font-medium text-white/40 mt-1">Click or Drop PDF to provide campaign context</div>
                               </label>
                               {contextFiles.length > 0 && (
                                 <div className="mt-3 space-y-2">
                                   {contextFiles.map(cf => (
                                     <div key={cf.id} className="flex justify-between items-center text-xs bg-black/40 px-3 py-2 rounded-lg border border-white/10">
                                        <span className="truncate max-w-[200px] text-white/80">{cf.filename}</span>
                                        <span className="text-white/40">{cf.chars} chars</span>
                                     </div>
                                   ))}
                                 </div>
                               )}
                            </div>

                            <div className="flex justify-end pt-2">
                               <button onClick={saveMemory} className="text-xs font-medium bg-white text-black px-4 py-2 rounded-lg hover:bg-white/90">
                                 Save Memory
                               </button>
                            </div>
                          </div>
                       </div>

                       {/* Section: Attachments */}
                       <div className="liquid-glass rounded-xl p-5 border border-white/5">
                          <div className="flex items-center gap-2 mb-4">
                             <span className="text-[#A4F4FD]">📎</span>
                             <h3 className="text-sm font-semibold">Attachments — sent with every email</h3>
                          </div>
                          <div className="space-y-3">
                            <label className="block border-2 border-dashed border-white/10 rounded-xl p-6 text-center cursor-pointer hover:border-white/30 hover:bg-white/5 transition-all">
                               <input type="file" className="hidden" accept=".pdf,.doc,.docx,.txt,.rtf,.png,.jpg,.jpeg"
                                 onChange={e => { const f = e.target.files?.[0]; if (f) uploadAttachment(f); e.target.value = ''; }} />
                               <div className="text-sm font-medium">Attach PDF / resume / portfolio</div>
                               <div className="text-xs text-white/40 mt-1">Every email this campaign sends carries these files</div>
                            </label>
                            {attachments.length > 0 ? (
                              <div className="space-y-2">
                                {attachments.map(att => (
                                  <div key={att.id} className="flex items-center justify-between gap-3 text-xs bg-black/40 px-3 py-2 rounded-lg border border-white/10">
                                    <div className="min-w-0 flex items-center gap-2">
                                      <span className="truncate text-white/80">{att.filename}</span>
                                      <span className="shrink-0 text-white/40">{fmtBytes(att.size)}</span>
                                    </div>
                                    <button
                                      onClick={() => removeAttachment(att.id, att.filename)}
                                      disabled={busy === `att-${att.id}`}
                                      className="shrink-0 text-white/40 hover:text-rose-400 transition-colors disabled:opacity-40"
                                      title="Remove this attachment"
                                    >
                                      ✕
                                    </button>
                                  </div>
                                ))}
                              </div>
                            ) : (
                              <p className="text-[10px] text-white/30">No attachments yet — only the email text is sent.</p>
                            )}
                            <p className="text-[10px] text-white/30">
                              Max 15 MB per file · 22 MB total per email · uploading the same filename again replaces it.
                            </p>
                          </div>
                       </div>

                       {/* Section: Ingestion */}
                       <div className="liquid-glass rounded-xl p-5 border border-white/5">
                          <h3 className="text-sm font-semibold mb-4">Target Ingestion</h3>
                          <div className="space-y-4">
                            <label className="block border-2 border-dashed border-white/10 rounded-xl p-8 text-center cursor-pointer hover:border-white/30 hover:bg-white/5 transition-all">
                              <input type="file" multiple className="hidden" onChange={e => e.target.files && uploadClientsFile(e.target.files[0])} accept=".csv,.pdf" />
                              <div className="text-sm font-medium">Drop or click to upload Clients (CSV/PDF)</div>
                              <div className="text-xs text-white/40 mt-1">Extracts clients automatically into queue</div>
                            </label>
                            
                            <div className="flex gap-2">
                               <div className="flex-1">
                                 <input type="text" placeholder="Google Sheet ID" value={sheetId} onChange={e => setSheetId(e.target.value)} className={inputCls} />
                               </div>
                               <div className="w-24">
                                 <input type="text" placeholder="Range" value={sheetRange} onChange={e => setSheetRange(e.target.value)} className={inputCls} />
                               </div>
                               <button onClick={connectSheet} className="px-4 py-2 rounded-xl bg-white/10 text-white text-xs font-medium hover:bg-white/20 border border-white/20">
                                 Pull
                               </button>
                            </div>
                          </div>
                       </div>

                       {/* Section: Run Control / Test */}
                       <div className="liquid-glass rounded-xl p-5 border border-white/5">
                          <h3 className="text-sm font-semibold mb-4 text-[#A4F4FD]">Test Send</h3>
                          <div className="space-y-4">
                            <Field label="Send one generated email">
                              <div className="flex gap-2">
                                <input
                                  type="email"
                                  value={testEmail}
                                  onChange={e => setTestEmail(e.target.value)}
                                  placeholder="test@inbox.com"
                                  className={inputCls}
                                />
                                <button
                                  onClick={doTestSend}
                                  disabled={!!busy || !testEmail}
                                  className="px-4 shrink-0 rounded-xl bg-white/10 text-[11px] font-bold text-white hover:bg-white/20 transition-colors border border-white/20 disabled:opacity-50 uppercase tracking-wider"
                                >
                                  SEND
                                </button>
                              </div>
                            </Field>
                          </div>
                       </div>

                    </div>
                  </>
                ) : (
                  <div className="flex-1 flex items-center justify-center text-white/30 text-sm py-32">
                    Select a campaign from the sidebar
                  </div>
                )}
             </div>
          </div>
        </section>
      </div>

      {/* CREATE MODAL */}
      {showCreate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-md">
          <div className="w-full max-w-lg liquid-glass bg-[#0e1014]/90 rounded-2xl border border-white/10 p-6 shadow-2xl">
            <h2 className="text-xl font-semibold mb-6">Compose Campaign</h2>
            <form onSubmit={createCampaign} className="space-y-4">
              <Field label="Name"><input required value={form.name} onChange={e=>setForm({...form,name:e.target.value})} className={inputCls} /></Field>
              <Field label="Subject"><input required value={form.subject} onChange={e=>setForm({...form,subject:e.target.value})} className={inputCls} /></Field>
              <Field label="Objective"><input required value={form.objective} onChange={e=>setForm({...form,objective:e.target.value})} className={inputCls} /></Field>
              <div className="grid grid-cols-2 gap-4">
                 <Field label="Tone">
                   <select value={form.tone} onChange={e=>setForm({...form,tone:e.target.value})} className={inputCls}>
                     <option value="professional">Professional</option>
                     <option value="casual">Casual</option>
                     <option value="urgent">Urgent</option>
                   </select>
                 </Field>
                 <Field label="Provider">
                   <select value={form.provider} onChange={e=>setForm({...form,provider:e.target.value})} className={inputCls}>
                     <option value="smtp">SMTP</option>
                     <option value="gmail">Gmail API</option>
                   </select>
                 </Field>
              </div>
              <div className="flex justify-end gap-3 mt-8">
                <button type="button" onClick={() => setShowCreate(false)} className="px-4 py-2 text-sm text-white/60 hover:text-white">Cancel</button>
                <button type="submit" className="px-5 py-2 rounded-full bg-white text-black text-sm font-semibold hover:bg-white/90">Create</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* PREVIEW MODAL */}
      {preview && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 sm:p-6 bg-black/80 backdrop-blur-sm">
          <div className="w-full max-w-3xl liquid-glass bg-[#0e1014]/90 rounded-2xl border border-white/20 shadow-2xl flex flex-col max-h-[85vh] overflow-hidden">
            <div className="p-4 border-b border-white/10 flex justify-between items-center bg-black/20">
              <div className="text-xs font-mono uppercase tracking-widest text-white/40">Dispatch Preview</div>
              <button onClick={() => setPreview(null)} className="text-white/40 hover:text-white transition-colors">✕</button>
            </div>
            <div className="p-6 border-b border-white/10 bg-black/10">
              <div className="mb-2"><span className="text-white/40 text-xs font-mono w-20 inline-block">TO:</span> <span className="text-sm font-medium">{preview.to}</span></div>
              <div><span className="text-white/40 text-xs font-mono w-20 inline-block">SUBJECT:</span> <span className="text-sm font-medium text-[#A4F4FD]">{preview.subject}</span></div>
              {preview.attachments && preview.attachments.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-2">
                  {preview.attachments.map(a => (
                    <span key={a.filename} className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-white/5 border border-white/10 text-[11px] text-white/70">
                      📎 {a.filename} <span className="text-white/30">{fmtBytes(a.size)}</span>
                    </span>
                  ))}
                </div>
              )}
            </div>
            <div className="p-8 overflow-y-auto flex-1 text-[15px] leading-relaxed whitespace-pre-wrap text-white/90">
              {preview.body}
            </div>
          </div>
        </div>
      )}

      {/* TOAST */}
      {toast && (
        <div className="fixed bottom-6 right-6 z-50 animate-in slide-in-from-bottom-5">
          <div className={`px-5 py-4 rounded-2xl border backdrop-blur-xl shadow-2xl flex items-center gap-4 text-sm font-medium ${
            toast.kind === 'ok' ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-100' :
            toast.kind === 'err' ? 'bg-rose-500/10 border-rose-500/20 text-rose-100' :
            'bg-white/10 border-white/10 text-white'
          }`}>
            {toast.kind === 'ok' && <div className="w-6 h-6 rounded-full bg-emerald-500/20 flex items-center justify-center text-emerald-400">✓</div>}
            {toast.kind === 'err' && <div className="w-6 h-6 rounded-full bg-rose-500/20 flex items-center justify-center text-rose-400">⚠</div>}
            {toast.text}
          </div>
        </div>
      )}
    </main>
  );
}
