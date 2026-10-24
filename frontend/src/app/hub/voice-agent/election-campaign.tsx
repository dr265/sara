'use client';

/**
 * Dynamic Election Campaign Voice Agent — configuration + calling console.
 *
 * Everything talks to the REAL backend (/voice-agent/voice/campaign/*):
 *   GET    /voice/campaign/profiles              the caller's campaign agents
 *   POST   /voice/campaign/profiles              create one
 *   GET    /voice/campaign/profiles/{id}         the saved configuration
 *   PUT    /voice/campaign/profiles/{id}         save (draft) / publish
 *   POST   /voice/campaign/profiles/{id}/publish publish the draft
 *   GET    /voice/campaign/profiles/{id}/preview the exact spoken lines
 *   POST   /voice/campaign/profiles/{id}/simulate one test turn (no call)
 *   GET    /voice/campaign/contacts              the caller's contacts
 *   POST   /voice/campaign/contacts              add a contact
 *   GET    /voice/campaign/calls                 the caller's campaign calls
 *   POST   /voice/campaign/calls                 start one call
 *
 * Requests go through saraFetch so the Clerk session token and the shared
 * SARA API key are attached — the backend scopes every row to the caller.
 * Saving a configuration never dials anybody.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { VOICE_AGENT_API_URL, saraFetch } from '@/lib/api';

const CAMPAIGN_API = `${VOICE_AGENT_API_URL}/voice-agent/voice/campaign`;

type Faq = { question: string; answer: string };

type CampaignFields = {
  agent_name: string;
  representative_name: string;
  organization_name: string;
  campaign_name: string;
  location: string;
  ward: string;
  position: string;
  language: string;
  voice: string;
  timezone: string;
  greeting: string;
  identity_disclosure: string;
  purpose: string;
  introduction: string;
  approved_message: string;
  faqs: Faq[];
  busy_message: string;
  decline_message: string;
  human_handoff_message: string;
  do_not_call_message: string;
  closing_message: string;
  unknown_answer: string;
  contact_details: string;
  handoff_destination: string;
  calling_schedule: string;
  calling_days: string;
  follow_up_enabled: boolean;
};

type CampaignProfile = {
  id: string;
  name: string;
  status: string;
  version: number;
  draft: CampaignFields;
  profile: CampaignFields;
  published_profile: CampaignFields;
  updated_at: string | null;
  published_at: string | null;
};

type Contact = {
  id: string;
  name: string | null;
  phone: string;
  eligible: boolean;
  reason: string;
  consent_status: string | null;
  call_count: number;
  last_call_at: string | null;
};

type CampaignCall = {
  id: string;
  customer_name: string | null;
  phone: string | null;
  state: string;
  direction: string;
  objective: string | null;
  provider_call_id: string | null;
  created_at: string | null;
  summary: string | null;
};

type Preview = {
  published: Record<string, string>;
  published_faqs: Faq[];
  draft: Record<string, string>;
  draft_faqs: Faq[];
  version: number;
};

type LanguageOption = { code: string; name: string; native_name: string };

const EMPTY_FIELDS: CampaignFields = {
  agent_name: '',
  representative_name: '',
  organization_name: '',
  campaign_name: '',
  location: '',
  ward: '',
  position: '',
  language: 'en-IN',
  voice: '',
  timezone: 'Asia/Kolkata',
  greeting: '',
  identity_disclosure: '',
  purpose: '',
  introduction: '',
  approved_message: '',
  faqs: [],
  busy_message: '',
  decline_message: '',
  human_handoff_message: '',
  do_not_call_message: '',
  closing_message: '',
  unknown_answer: '',
  contact_details: '',
  handoff_destination: '',
  calling_schedule: '09:00-19:00',
  calling_days: 'mon,tue,wed,thu,fri,sat,sun',
  follow_up_enabled: true,
};

const TEXT_FIELDS: { key: keyof CampaignFields; label: string; hint?: string; long?: boolean }[] = [
  { key: 'greeting', label: 'Greeting', hint: 'The first thing the caller hears. Use [representative_name], [location], [position] to fill in the saved values.', long: true },
  { key: 'identity_disclosure', label: 'Identity disclosure', hint: 'Who is calling and on whose behalf.', long: true },
  { key: 'purpose', label: 'Purpose of the call', long: true },
  { key: 'introduction', label: 'Main introduction', long: true },
  { key: 'approved_message', label: 'Approved campaign message', hint: 'Only verified information belongs here.', long: true },
  { key: 'busy_message', label: 'Busy recipient', long: true },
  { key: 'decline_message', label: 'Recipient declines', long: true },
  { key: 'human_handoff_message', label: 'Human hand-off', long: true },
  { key: 'do_not_call_message', label: 'Do-not-call request', long: true },
  { key: 'closing_message', label: 'Closing message', long: true },
  { key: 'unknown_answer', label: 'Unknown question', hint: 'Used when a question is not covered by the approved information.', long: true },
];

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await saraFetch(`${CAMPAIGN_API}${path}`, {
    cache: 'no-store',
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* keep the status text */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

function SectionTitle({ children, note }: { children: React.ReactNode; note?: string }) {
  return (
    <div className="mb-3">
      <h4 className="text-[13.5px] font-medium text-[var(--ivory)]">{children}</h4>
      {note && <p className="mt-0.5 text-[12px] leading-relaxed text-[var(--dim)]">{note}</p>}
    </div>
  );
}

export default function ElectionCampaignPanel({
  say,
}: {
  say: (ok: boolean, msg: string) => void;
}) {
  const [profiles, setProfiles] = useState<CampaignProfile[]>([]);
  const [selectedId, setSelectedId] = useState<string>('');
  const [fields, setFields] = useState<CampaignFields>(EMPTY_FIELDS);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [calls, setCalls] = useState<CampaignCall[]>([]);
  const [languages, setLanguages] = useState<LanguageOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [contactName, setContactName] = useState('');
  const [contactPhone, setContactPhone] = useState('');
  const [simMessage, setSimMessage] = useState('');
  const [simReply, setSimReply] = useState('');
  const [authError, setAuthError] = useState('');

  const selected = useMemo(
    () => profiles.find((p) => p.id === selectedId) ?? null,
    [profiles, selectedId],
  );

  const loadProfiles = useCallback(async () => {
    const data = await api<{ profiles: CampaignProfile[] }>('/profiles');
    setProfiles(data.profiles ?? []);
    return data.profiles ?? [];
  }, []);

  const loadContacts = useCallback(async () => {
    const data = await api<{ contacts: Contact[] }>('/contacts');
    setContacts(data.contacts ?? []);
  }, []);

  const loadCalls = useCallback(async () => {
    const data = await api<{ calls: CampaignCall[] }>('/calls');
    setCalls(data.calls ?? []);
  }, []);

  const loadPreview = useCallback(async (profileId: string) => {
    if (!profileId) return;
    const data = await api<Preview>(`/profiles/${profileId}/preview`);
    setPreview(data);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setAuthError('');
      try {
        const list = await loadProfiles();
        if (cancelled) return;
        if (list.length > 0) {
          setSelectedId(list[0].id);
          setFields({ ...EMPTY_FIELDS, ...list[0].draft });
          await loadPreview(list[0].id);
        }
        await Promise.all([loadContacts(), loadCalls()]);
      } catch (err) {
        const message = (err as Error).message;
        if (/401|Unauthorized/i.test(message)) {
          setAuthError(
            'Sign in (or configure the SARA API key) to manage your campaign agent — every campaign profile belongs to one user.',
          );
        } else {
          say(false, `Cannot reach the campaign service: ${message}`);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    fetch(`${VOICE_AGENT_API_URL}/voice-agent/voice/languages`, { cache: 'no-store' })
      .then((r) => r.json())
      .then((d) => setLanguages(d.languages ?? []))
      .catch(() => setLanguages([]));
  }, []);

  const selectProfile = async (id: string) => {
    setSelectedId(id);
    const row = profiles.find((p) => p.id === id);
    if (row) setFields({ ...EMPTY_FIELDS, ...row.draft });
    try {
      await loadPreview(id);
    } catch {
      /* preview is best effort */
    }
  };

  const setField = (key: keyof CampaignFields, value: string | boolean | Faq[]) => {
    setFields((prev) => ({ ...prev, [key]: value }));
  };

  const save = async (publish: boolean) => {
    if (!selectedId) return;
    setBusy(true);
    try {
      const data = await api<{ profile: CampaignProfile; published: boolean }>(
        `/profiles/${selectedId}`,
        {
          method: 'PUT',
          body: JSON.stringify({ profile: fields, publish }),
        },
      );
      setProfiles((prev) => prev.map((p) => (p.id === data.profile.id ? data.profile : p)));
      setFields({ ...EMPTY_FIELDS, ...data.profile.draft });
      await loadPreview(data.profile.id);
      say(
        true,
        publish
          ? `Published version ${data.profile.version}. The next call uses this configuration.`
          : 'Draft saved. Publish to make the agent use it on the next call.',
      );
    } catch (err) {
      say(false, `Save failed: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const publish = async () => {
    if (!selectedId) return;
    setBusy(true);
    try {
      const data = await api<{ profile: CampaignProfile }>(`/profiles/${selectedId}/publish`, {
        method: 'POST',
      });
      setProfiles((prev) => prev.map((p) => (p.id === data.profile.id ? data.profile : p)));
      await loadPreview(data.profile.id);
      say(true, `Published version ${data.profile.version}.`);
    } catch (err) {
      say(false, `Publish failed: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const createProfile = async () => {
    setBusy(true);
    try {
      const data = await api<{ profile: CampaignProfile }>('/profiles', {
        method: 'POST',
        body: JSON.stringify({ name: 'Election Campaign Agent' }),
      });
      setProfiles((prev) => [...prev, data.profile]);
      setSelectedId(data.profile.id);
      setFields({ ...EMPTY_FIELDS, ...data.profile.draft });
      await loadPreview(data.profile.id);
      say(true, 'Campaign agent created from the demo profile. Replace the values and save.');
    } catch (err) {
      say(false, `Create failed: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const simulate = async () => {
    if (!selectedId || !simMessage.trim()) return;
    setBusy(true);
    try {
      const data = await api<{ reply: string }>(`/profiles/${selectedId}/simulate`, {
        method: 'POST',
        body: JSON.stringify({ message: simMessage.trim() }),
      });
      setSimReply(data.reply || '');
      setSimMessage('');
    } catch (err) {
      say(false, `Simulation failed: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const addContact = async () => {
    if (!contactPhone.trim()) return;
    setBusy(true);
    try {
      await api('/contacts', {
        method: 'POST',
        body: JSON.stringify({ name: contactName.trim() || null, phone: contactPhone.trim() }),
      });
      setContactName('');
      setContactPhone('');
      await loadContacts();
      say(true, 'Contact saved. Nothing is dialled until you press Call.');
    } catch (err) {
      say(false, `Contact failed: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const startCall = async (contact: Contact) => {
    if (!selectedId) return;
    setBusy(true);
    try {
      const data = await api<{
        call_id: string;
        state: string;
        provider: string;
        provider_accepted: boolean;
        provider_call_id: string | null;
        provider_reason: string;
        campaign: { representative: string; greeting: string };
      }>('/calls', {
        method: 'POST',
        body: JSON.stringify({ profile_id: selectedId, customer_id: contact.id }),
      });
      await loadCalls();
      say(
        data.provider_accepted,
        data.provider_accepted
          ? `Provider accepted the call to ${contact.name ?? contact.phone}. Status: ${data.state}.`
          : `Provider refused the call: ${data.provider_reason || 'unknown reason'}`,
      );
    } catch (err) {
      say(false, `Call not started: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const inputCls =
    'w-full rounded-xl border border-white/10 bg-[var(--ink)] px-3.5 py-2.5 text-[13.5px] leading-relaxed text-[var(--ivory)] outline-none transition-colors placeholder:text-[var(--dim)] focus:border-[var(--violet)]';
  const labelCls = 'block text-[12.5px]';
  const spanCls = 'mb-1.5 block text-[var(--muted)]';

  if (loading) return <p className="text-[13px] text-[var(--dim)]">Loading campaign agents…</p>;

  if (authError) {
    return (
      <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-[13px] text-amber-200">
        {authError}
      </div>
    );
  }

  if (profiles.length === 0) {
    return (
      <div className="space-y-4">
        <p className="text-[13px] leading-relaxed text-[var(--muted)]">
          No campaign agent yet. Create one from the generic demo profile, replace the values with
          your own, and publish it. Nothing is dialled until you press Call.
        </p>
        <button
          onClick={createProfile}
          disabled={busy}
          className="rounded-full bg-[var(--violet)] px-6 py-2.5 text-[13.5px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {busy ? 'Creating…' : 'Create campaign agent'}
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* ---- agent picker ------------------------------------------------- */}
      <div className="flex flex-wrap items-center gap-3">
        <select
          value={selectedId}
          onChange={(e) => selectProfile(e.target.value)}
          className={`${inputCls} max-w-xs`}
        >
          {profiles.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} — v{p.version} ({p.status.toLowerCase()})
            </option>
          ))}
        </select>
        <button
          onClick={createProfile}
          disabled={busy}
          className="rounded-full border border-white/10 px-4 py-2 text-[12.5px] text-[var(--muted)] transition-colors hover:text-[var(--ivory)] disabled:opacity-50"
        >
          + New agent
        </button>
        {selected && (
          <span className="mono text-[11px] text-[var(--dim)]">
            published v{selected.version} · {selected.published_at ? 'live' : 'never published'}
          </span>
        )}
      </div>

      {/* ---- identity ------------------------------------------------------ */}
      <div>
        <SectionTitle note="These values fill every [placeholder] in the messages below.">
          Candidate & campaign
        </SectionTitle>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {(
            [
              ['agent_name', 'Agent name'],
              ['representative_name', 'Candidate / representative'],
              ['organization_name', 'Organization / party'],
              ['campaign_name', 'Campaign name'],
              ['location', 'Location / city'],
              ['ward', 'Ward / constituency'],
              ['position', 'Position / role'],
              ['contact_details', 'Contact details'],
              ['handoff_destination', 'Human hand-off number'],
              ['calling_schedule', 'Calling window (HH:MM-HH:MM)'],
            ] as [keyof CampaignFields, string][]
          ).map(([key, label]) => (
            <label key={key} className={labelCls}>
              <span className={spanCls}>{label}</span>
              <input
                className={inputCls}
                value={String(fields[key] ?? '')}
                onChange={(e) => setField(key, e.target.value)}
              />
            </label>
          ))}

          <label className={labelCls}>
            <span className={spanCls}>Language</span>
            <select
              className={inputCls}
              value={fields.language}
              onChange={(e) => setField('language', e.target.value)}
            >
              {(languages.length
                ? languages
                : [{ code: 'en-IN', name: 'English', native_name: 'English' }]
              ).map((lang) => (
                <option key={lang.code} value={lang.code}>
                  {lang.name} ({lang.native_name}) — {lang.code}
                </option>
              ))}
            </select>
          </label>

          <label className={labelCls}>
            <span className={spanCls}>Voice (where supported)</span>
            <input
              className={inputCls}
              value={fields.voice}
              placeholder="provider voice id, e.g. priya"
              onChange={(e) => setField('voice', e.target.value)}
            />
          </label>

          <label className={labelCls}>
            <span className={spanCls}>Calling days</span>
            <input
              className={inputCls}
              value={fields.calling_days}
              placeholder="mon,tue,wed,thu,fri,sat,sun"
              onChange={(e) => setField('calling_days', e.target.value)}
            />
          </label>

          <label className={labelCls}>
            <span className={spanCls}>Timezone</span>
            <input
              className={inputCls}
              value={fields.timezone}
              onChange={(e) => setField('timezone', e.target.value)}
            />
          </label>
        </div>
      </div>

      {/* ---- conversation flow -------------------------------------------- */}
      <div>
        <SectionTitle note="Each line is spoken exactly as written here.">
          Conversation flow
        </SectionTitle>
        <div className="space-y-4">
          {TEXT_FIELDS.map((field) => (
            <label key={field.key} className={labelCls}>
              <span className={spanCls}>{field.label}</span>
              <textarea
                className={`${inputCls} min-h-[86px] resize-y`}
                value={String(fields[field.key] ?? '')}
                onChange={(e) => setField(field.key, e.target.value)}
              />
              {field.hint && (
                <span className="mt-1 block text-[11.5px] leading-relaxed text-[var(--dim)]">
                  {field.hint}
                </span>
              )}
            </label>
          ))}
        </div>
      </div>

      {/* ---- FAQs ---------------------------------------------------------- */}
      <div>
        <SectionTitle note="Answers come from this list. Anything else falls back to the safe answer.">
          Frequently asked questions
        </SectionTitle>
        <div className="space-y-3">
          {fields.faqs.map((faq, index) => (
            <div key={index} className="rounded-xl border border-white/10 bg-[var(--ink)] p-3">
              <input
                className={`${inputCls} mb-2`}
                value={faq.question}
                placeholder="Question"
                onChange={(e) => {
                  const next = [...fields.faqs];
                  next[index] = { ...faq, question: e.target.value };
                  setField('faqs', next);
                }}
              />
              <textarea
                className={`${inputCls} min-h-[64px] resize-y`}
                value={faq.answer}
                placeholder="Answer"
                onChange={(e) => {
                  const next = [...fields.faqs];
                  next[index] = { ...faq, answer: e.target.value };
                  setField('faqs', next);
                }}
              />
              <button
                onClick={() => setField('faqs', fields.faqs.filter((_, i) => i !== index))}
                className="mt-2 text-[12px] text-[var(--red)] hover:underline"
              >
                Remove
              </button>
            </div>
          ))}
          <button
            onClick={() => setField('faqs', [...fields.faqs, { question: '', answer: '' }])}
            className="rounded-full border border-white/10 px-4 py-2 text-[12.5px] text-[var(--muted)] transition-colors hover:text-[var(--ivory)]"
          >
            + Add question
          </button>
        </div>
      </div>

      {/* ---- save / publish ------------------------------------------------ */}
      <div className="flex flex-wrap items-center justify-end gap-3 border-t border-white/5 pt-5">
        <button
          onClick={() => save(false)}
          disabled={busy || !selectedId}
          className="rounded-full border border-white/10 px-5 py-2.5 text-[13.5px] text-[var(--muted)] transition-colors hover:text-[var(--ivory)] disabled:opacity-50"
        >
          Save draft
        </button>
        <button
          onClick={() => save(true)}
          disabled={busy || !selectedId}
          className="rounded-full bg-[var(--violet)] px-6 py-2.5 text-[13.5px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {busy ? 'Saving…' : 'Save & publish'}
        </button>
        {selected && selected.status !== 'PUBLISHED' && (
          <button
            onClick={publish}
            disabled={busy}
            className="text-[12.5px] text-[var(--muted)] underline hover:text-[var(--ivory)] disabled:opacity-50"
          >
            Publish saved draft
          </button>
        )}
      </div>

      {/* ---- preview ------------------------------------------------------- */}
      {preview && (
        <div>
          <SectionTitle note="Exactly what the agent will say, with your saved values filled in.">
            Preview (published v{preview.version})
          </SectionTitle>
          <div className="space-y-2 rounded-xl border border-white/10 bg-[var(--ink)] p-4">
            {Object.entries(preview.published).map(([key, text]) => (
              <div key={key}>
                <div className="mono text-[10px] uppercase tracking-wide text-[var(--dim)]">
                  {key.replace(/_/g, ' ')}
                </div>
                <div className="text-[13px] leading-relaxed text-[#e9edef]">{text}</div>
              </div>
            ))}
            {preview.published_faqs.map((faq, index) => (
              <div key={index}>
                <div className="mono text-[10px] uppercase tracking-wide text-[var(--dim)]">faq</div>
                <div className="text-[13px] leading-relaxed text-[#e9edef]">
                  <span className="text-[var(--muted)]">Q: {faq.question}</span>
                  <br />
                  A: {faq.answer}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ---- test a turn ---------------------------------------------------- */}
      <div>
        <SectionTitle note="Runs one turn against the saved agent. No call, no charge.">
          Test a reply
        </SectionTitle>
        <div className="flex flex-wrap gap-2">
          <input
            className={`${inputCls} flex-1 min-w-[220px]`}
            value={simMessage}
            placeholder="What would the caller say?"
            onChange={(e) => setSimMessage(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') simulate();
            }}
          />
          <button
            onClick={simulate}
            disabled={busy || !simMessage.trim()}
            className="rounded-full border border-white/10 px-5 py-2.5 text-[13px] text-[var(--muted)] transition-colors hover:text-[var(--ivory)] disabled:opacity-50"
          >
            Run
          </button>
        </div>
        {simReply && (
          <div className="mt-3 rounded-xl border border-white/10 bg-[var(--ink)] px-4 py-3 text-[13px] leading-relaxed text-[#e9edef]">
            {simReply}
          </div>
        )}
      </div>

      {/* ---- contacts + calling --------------------------------------------- */}
      <div>
        <SectionTitle note="Saving a contact never dials it. A call only starts when you press Call.">
          Contacts
        </SectionTitle>
        <div className="mb-3 flex flex-wrap gap-2">
          <input
            className={`${inputCls} max-w-[220px]`}
            value={contactName}
            placeholder="Name"
            onChange={(e) => setContactName(e.target.value)}
          />
          <input
            className={`${inputCls} max-w-[220px]`}
            value={contactPhone}
            placeholder="+91 90000 00000"
            onChange={(e) => setContactPhone(e.target.value)}
          />
          <button
            onClick={addContact}
            disabled={busy || !contactPhone.trim()}
            className="rounded-full border border-white/10 px-4 py-2.5 text-[13px] text-[var(--muted)] transition-colors hover:text-[var(--ivory)] disabled:opacity-50"
          >
            Add contact
          </button>
        </div>

        {contacts.length === 0 ? (
          <p className="rounded-xl border border-dashed border-white/15 px-5 py-6 text-center text-[13px] text-[var(--dim)]">
            No contacts yet.
          </p>
        ) : (
          <div className="space-y-2">
            {contacts.map((contact) => (
              <div
                key={contact.id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-white/10 bg-[var(--ink)] px-4 py-3"
              >
                <div className="min-w-0">
                  <div className="text-[13.5px] text-[#e9edef]">
                    {contact.name ?? 'Unnamed contact'}
                  </div>
                  <div className="mono mt-0.5 text-[10.5px] text-[var(--dim)]">
                    {contact.phone}
                    {contact.reason ? ` · ${contact.reason}` : ''}
                  </div>
                </div>
                <button
                  onClick={() => startCall(contact)}
                  disabled={busy || !contact.eligible || !selectedId}
                  title={contact.eligible ? 'Call this contact' : contact.reason}
                  className="rounded-full bg-[var(--violet)] px-4 py-2 text-[12.5px] font-semibold text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Call
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ---- campaign calls -------------------------------------------------- */}
      <div>
        <SectionTitle note="Live status comes from the telephony provider's own status callback.">
          Campaign calls
        </SectionTitle>
        {calls.length === 0 ? (
          <p className="rounded-xl border border-dashed border-white/15 px-5 py-6 text-center text-[13px] text-[var(--dim)]">
            No campaign calls yet.
          </p>
        ) : (
          <div className="space-y-2">
            {calls.map((call) => (
              <div
                key={call.id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-white/10 bg-[var(--ink)] px-4 py-3"
              >
                <div className="min-w-0">
                  <div className="text-[13.5px] text-[#e9edef]">
                    {call.customer_name ?? 'Unknown caller'}
                  </div>
                  <div className="mono mt-0.5 text-[10.5px] text-[var(--dim)]">
                    {call.phone ?? '—'} · {call.objective ?? 'campaign outreach'}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  <span className="mono text-[10.5px] text-[var(--dim)]">{call.direction}</span>
                  <span
                    className={`mono rounded-full px-2.5 py-0.5 text-[10.5px] ${
                      call.state === 'COMPLETED'
                        ? 'bg-[var(--green)]/15 text-[var(--green)]'
                        : call.state === 'FAILED' || call.state === 'CANCELLED'
                          ? 'bg-[var(--red)]/15 text-[var(--red)]'
                          : 'bg-[var(--violet)]/15 text-[#c4a7f7]'
                    }`}
                  >
                    {call.state}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
