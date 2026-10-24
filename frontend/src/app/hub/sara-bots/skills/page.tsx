"use client";

import React, { useCallback, useEffect, useState } from "react";
import { SARA_API_URL, saraFetch } from "@/lib/api";

type Skill = {
  id: string;
  name: string;
  description: string;
  instructions: string;
  required_tools: string[];
  version: string;
  enabled: boolean;
};

;

const SKILLS_CSS = `
.skills-page { max-width: 1000px; margin: 0 auto; padding: 24px; }
.skills-header { margin-bottom: 28px; }
.skills-title { font-size: 26px; font-weight: 800; letter-spacing: -0.02em; margin: 0 0 6px; }
.skills-sub { color: #94a3b8; font-size: 13px; margin: 0 0 20px; max-width: 640px; }
.back-link { color: #67e8f9; text-decoration: none; font-size: 13px; display: inline-block; margin-bottom: 20px; }
.back-link:hover { text-decoration: underline; }
.notice { color: #34d399; margin-bottom: 16px; font-size: 13px; }
.error { color: #f87171; margin-bottom: 16px; white-space: pre-wrap; font-size: 12.5px; }
.section { margin-bottom: 32px; }
.section-title { font-size: 18px; font-weight: 700; margin: 0 0 14px; }
.form-grid { display: grid; gap: 12px; }
.form-field { display: flex; flex-direction: column; gap: 4px; }
.form-label { font-size: 11.5px; color: #64748b; margin-bottom: 4px; }
.input { width: 100%; padding: 10px 14px; border-radius: 10px; border: 1px solid rgba(148,163,184,0.2); background: rgba(15,23,42,0.8); color: #e2e8f0; font-size: 13.5px; box-sizing: border-box; }
.input:focus { outline: none; border-color: #67e8f9; box-shadow: 0 0 0 3px rgba(103,232,249,0.15); }
.textarea { min-height: 90px; resize: vertical; }
.btn-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 4px; }
.btn { padding: 9px 16px; border-radius: 10px; font-weight: 700; cursor: pointer; transition: all .15s; border: none; font-size: 12.5px; }
.btn-primary { background: linear-gradient(180deg,#22d3ee,#0891b2); color: #06121a; }
.btn-primary:hover { filter: brightness(1.1); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-ghost { background: transparent; border: 1px solid rgba(148,163,184,0.25); color: #cbd5e1; }
.btn-ghost:hover { background: rgba(148,163,184,0.08); }
.btn-danger { background: rgba(248,113,113,0.15); border: 1px solid rgba(248,113,113,0.35); color: #f87171; }
.btn-danger:hover { background: rgba(248,113,113,0.25); }
.skills-list { display: flex; flex-direction: column; gap: 10px; }
.skill-card { border: 1px solid rgba(148,163,184,0.16); border-radius: 14px; padding: 16px; background: rgba(2,6,23,0.5); }
.skill-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 6px; }
.skill-name { font-size: 15px; font-weight: 700; }
.skill-meta { font-size: 11.5px; color: #94a3b8; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.skill-desc { color: #cbd5e1; font-size: 13px; margin: 6px 0; }
.skill-tools { font-size: 11px; color: #64748b; margin: 6px 0; }
.skill-actions { display: flex; gap: 8px; margin-top: 10px; }
.tag { font-size: 10.5px; padding: 2px 8px; border-radius: 999px; background: rgba(148,163,184,0.1); color: #94a3b8; border: 1px solid rgba(148,163,184,0.15); }
.tag-on { background: rgba(52,211,153,0.15); color: #34d399; border-color: rgba(52,211,153,0.3); }
.loading { color: #94a3b8; text-align: center; padding: 40px; }
.empty { color: #64748b; text-align: center; padding: 40px; }
`;

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({
    name: "", description: "", instructions: "", required_tools: "", version: "1.0.0",
  });

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await saraFetch(`${SARA_API_URL}/api/sara-bots/skills`);
      if (!r.ok) throw new Error(await r.text());
      const j = await r.json();
      setSkills(j.skills ?? []);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const create = async () => {
    setCreating(true);
    setError("");
    try {
      const tools = form.required_tools.split(",").map((s) => s.trim()).filter(Boolean);
      const r = await saraFetch(`${SARA_API_URL}/api/sara-bots/skills`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: form.name,
          description: form.description,
          instructions: form.instructions,
          required_tools: tools,
          version: form.version,
        }),
      });
      if (!r.ok) throw new Error(await r.text());
      setNotice("Skill created.");
      setForm({ name: "", description: "", instructions: "", required_tools: "", version: "1.0.0" });
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setCreating(false);
    }
  };

  const remove = async (id: string) => {
    if (!confirm("Delete this skill?")) return;
    try {
      const r = await saraFetch(`${SARA_API_URL}/api/sara-bots/skills/${id}`, { method: "DELETE" });
      if (!r.ok) throw new Error(await r.text());
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const toggle = async (sk: Skill) => {
    try {
      const r = await saraFetch(`${SARA_API_URL}/api/sara-bots/skills/${sk.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !sk.enabled }),
      });
      if (!r.ok) throw new Error(await r.text());
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <main style={{ maxWidth: 1000, margin: "0 auto", padding: 24 }}>
      <style jsx>{SKILLS_CSS}</style>

      <div className="skills-page">
        <header className="skills-header">
          <a href="/hub/sara-bots" className="back-link">&larr; Back to My Bots</a>
          <h1 className="skills-title">Skills Library</h1>
          <p className="skills-sub">
            Reusable SKILL.md-style instruction packages. Attach them to your bots in
            Edit configuration. Skills add instructions, never tools.
          </p>
        </header>

        {notice && <div className="notice">{notice}</div>}
        {error && <div className="error">{error}</div>}

        <section className="section">
          <h2 className="section-title">Create a skill</h2>
          <div className="form-grid">
            <div className="form-field">
              <label className="form-label">Name</label>
              <input className="input" placeholder="Name (min 2 chars)" value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </div>
            <div className="form-field">
              <label className="form-label">Description</label>
              <input className="input" placeholder="Description" value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })} />
            </div>
            <div className="form-field">
              <label className="form-label">Instructions (the SKILL body)</label>
              <textarea className="input textarea" rows={6} placeholder="Instructions..." value={form.instructions}
                onChange={(e) => setForm({ ...form, instructions: e.target.value })} />
            </div>
            <div className="form-field">
              <label className="form-label">Required tools (comma-separated ids)</label>
              <input className="input" placeholder="e.g. action.web_search, hub.email-automation" value={form.required_tools}
                onChange={(e) => setForm({ ...form, required_tools: e.target.value })} />
            </div>
            <div className="form-field">
              <label className="form-label">Version</label>
              <input className="input" placeholder="Version" value={form.version}
                onChange={(e) => setForm({ ...form, version: e.target.value })} />
            </div>
            <div className="btn-row">
              <button className="btn btn-primary" onClick={create} disabled={creating}>
                {creating ? "Creating..." : "Create skill"}
              </button>
            </div>
          </div>
        </section>

        <section className="section">
          <h2 className="section-title">Your skills ({skills.length})</h2>
          {loading && <div className="loading">Loading&hellip;</div>}
          {!loading && skills.length === 0 && <div className="empty">No skills yet.</div>}
          <div className="skills-list">
            {skills.map((sk) => (
              <div key={sk.id} className="skill-card">
                <div className="skill-head">
                  <span className="skill-name">{sk.name}</span>
                  <div className="skill-meta">
                    <span className={`tag ${sk.enabled ? "tag-on" : ""}`}>
                      {sk.enabled ? "enabled" : "disabled"}
                    </span>
                    <span>v{sk.version}</span>
                    <span>{sk.id}</span>
                  </div>
                </div>
                <div className="skill-desc">{sk.description}</div>
                <div className="skill-tools">
                  tools: {sk.required_tools.join(", ") || "none"}
                </div>
                <div className="skill-actions">
                  <button className="btn btn-ghost" onClick={() => toggle(sk)}>
                    {sk.enabled ? "Disable" : "Enable"}
                  </button>
                  <button className="btn btn-danger" onClick={() => remove(sk.id)}>Delete</button>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </main>
  );
}