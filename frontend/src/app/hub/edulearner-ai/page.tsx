"use client";

/**
 * EduLearner AI — Nexus Hub workspace.
 *
 * Premium 3D glassmorphism surface for the adaptive learning engine:
 * layered translucent planes, depth-lit hero with an orbiting agent pipeline,
 * and live panels wired to /api/edulearner (real analysis, planning, marked
 * practice, adaptive loop, readiness, memory, mentor chat, reports).
 *
 * Accessibility: real tablist semantics, focus rings, aria-live status,
 * prefers-reduced-motion respected (animations collapse via globals.css).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { 
  Activity, BookOpen, Brain, CalendarCheck, ChartNoAxesColumn, CheckCircle2,
  ChevronRight, CircleDot, FileText, FileUp, GraduationCap, Loader2,
  MessageSquare, Plus, RefreshCw, Route, Sparkles, Target, Trash2, User, XCircle, Apple
} from "lucide-react";
import { EDULEARNER_API_URL, saraFetch } from "@/lib/api";

const API = `${EDULEARNER_API_URL}/api/edulearner`;

// ----------------------------------------------------------------- types
interface Gap {
  skill: string; current_score: number; target_score: number; gap: number;
  priority: number; classification: string; why: string[];
}
interface Task {
  task_id: string; title: string; skill: string; objective: string;
  task_type: string; difficulty: string; estimated_minutes: number; rationale: string;
}
interface RoadmapWeek {
  week: number; theme: string; assessment: string; project: string;
  objectives: { title: string; description: string }[];
  tasks: Task[];
}
interface Roadmap {
  generated_at?: string; reason?: string; total_weeks?: number;
  total_objectives?: number; deferred_gaps?: string[];
  phases: { phase: number; title: string; expected_outcome: string; weeks: RoadmapWeek[] }[];
}
interface Readiness {
  overall: number; category_scores: Record<string, number>;
  evidence: string[]; blockers: string[]; recommendations: string[];
  confidence: number;
}
interface Question {
  practice_id: string; skill: string; topic: string; question: string;
  difficulty: string; options: string[]; hints: string[]; question_type: string;
  estimated_minutes: number; evaluation_criteria: string[];
}
interface AnswerResult {
  correct: boolean; score: number; explanation: string; misconceptions: string[];
  adaptive?: { decision: string; state: string; skill?: string; changes: string[]; reason: string[] };
  remediation?: { active?: boolean };
}
interface Dashboard {
  profile: { user_id: string; name: string; goal: string; target_role: string; available_hours: number };
  profile_completion: number; skills: { name: string; score: number; level: string }[];
  gaps: Gap[]; roadmap: Roadmap; plan: Task[]; readiness: Readiness | null;
  remediation: Record<string, { active?: boolean; difficulty?: string; prerequisites?: string[] }>;
  memory: Record<string, unknown>; progress: { skill: string; score: number | null; completed: boolean }[];
}
interface AgentInfo { name: string; description: string; }
interface Registry {
  count: number; agents: AgentInfo[]; pipeline: { agent: string; label: string }[];
  llm: string; database: string;
}
interface ProfileProject { id?: string; title: string; stack: string[]; description?: string; }
interface FullProfile {
  user_id: string; name: string; education: string; experience: string; resume_text: string;
  skills: string[]; projects: ProfileProject[]; certifications: string[];
  goal: string; target_role: string; learning_style: string; available_hours: number;
  resume: { id: string; filename: string; uploaded_at: string } | null;
  analysis_summary?: { gaps: number; roadmap_weeks: number };
}

const TABS = [
  { id: "profile", label: "Profile", icon: User },
  { id: "overview", label: "Overview", icon: ChartNoAxesColumn },
  { id: "roadmap", label: "Roadmap", icon: Route },
  { id: "today", label: "Today", icon: CalendarCheck },
  { id: "practice", label: "Practice", icon: BookOpen },
  { id: "readiness", label: "Readiness", icon: Target },
  { id: "mind", label: "Memory", icon: Brain },
  { id: "mentor", label: "Mentor", icon: MessageSquare },
] as const;

type TabId = (typeof TABS)[number]["id"];

const DEMO_PROFILE = {
  name: "Aarav Sharma",
  education: "B.Tech Computer Science, 2nd year",
  skills: ["C++", "Python", "HTML", "CSS", "React", "MongoDB", "Git"],
  projects: [{ title: "Study planner app", stack: ["React", "Python", "MongoDB"] }],
  certifications: ["Data Structures (online course)"],
  goal: "Software Engineer",
  target_role: "Software Engineer",
  learning_style: "hands-on",
  available_hours: 10,
};

// ----------------------------------------------------------------- api
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await saraFetch(`${API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok || !body || body.success === false) {
    const detail = body?.detail ?? body?.message ?? `Request failed (${res.status})`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body.data as T;
}

/** multipart upload against the SARA backend (no JSON content-type). */
async function upload<T>(path: string, file: File): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  const res = await saraFetch(`${API}${path}`, { method: "POST", body: form });
  const body = await res.json().catch(() => null);
  if (!res.ok || !body || body.success === false) {
    const detail = body?.detail ?? body?.message ?? `Upload failed (${res.status})`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body.data as T;
}


// ----------------------------------------------------------------- styles
const STYLES = `
.el-root { color: #e9eef5; }
.el-shell { position: relative; border-radius: 24px; overflow: hidden;
  border: 1px solid rgba(255,255,255,.10);
  background: radial-gradient(120% 140% at 12% 0%, #16203a 0%, #0b0f19 46%, #070a11 100%);
  box-shadow: 0 40px 120px rgba(2,6,18,.55); }
.el-shell::after { content: ""; position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(180deg, rgba(255,255,255,.05), transparent 34%); }

.el-hero { position: relative; padding: 34px 30px 26px; perspective: 1200px; }
.el-orbit { position: absolute; inset: -40% -10% auto auto; width: 620px; height: 620px;
  transform: rotateX(62deg) rotateZ(-14deg); transform-style: preserve-3d; pointer-events: none; }
.el-ring { position: absolute; inset: 0; border-radius: 50%; border: 1px solid rgba(139,183,255,.16); }
.el-ring.r2 { inset: 12%; border-color: rgba(167,139,250,.16); animation: el-spin 24s linear infinite; }
.el-ring.r3 { inset: 26%; border-color: rgba(56,189,248,.14); animation: el-spin 34s linear infinite reverse; }
@keyframes el-spin { to { transform: rotate(360deg); } }
.el-node { position: absolute; top: 50%; left: 50%; width: 8px; height: 8px; border-radius: 999px;
  background: #bfe0ff; box-shadow: 0 0 14px rgba(191,224,255,.9); }

.el-kicker { color: #9dc2ee; font-size: 10px; font-weight: 700; letter-spacing: .3em; text-transform: uppercase; }
.el-title { margin: 10px 0 8px; font-size: clamp(30px, 4.4vw, 52px); line-height: 1;
  font-weight: 750; letter-spacing: -.045em; color: #f4f8ff;
  text-shadow: 0 1px 0 rgba(255,255,255,.22), 0 22px 60px rgba(56,120,255,.28); }
.el-sub { max-width: 640px; color: #9fb0c6; font-size: 14px; line-height: 1.65; }

.el-glass { position: relative; border-radius: 18px; border: 1px solid rgba(255,255,255,.10);
  background: linear-gradient(160deg, rgba(20,28,46,.4), rgba(10,14,24,.3));
  -webkit-backdrop-filter: blur(30px) saturate(150%); backdrop-filter: blur(30px) saturate(150%);
  box-shadow: 0 24px 60px rgba(3,7,16,.5), inset 0 1px rgba(255,255,255,.07); }
.el-glass.lift { transform: translateY(-2px); transition: transform .35s cubic-bezier(.22,1,.36,1), box-shadow .35s; }
.el-glass.lift:hover { transform: translateY(-4px); box-shadow: 0 32px 80px rgba(3,7,16,.6), inset 0 1px rgba(255,255,255,.1); }
.el-pad { padding: 20px 22px; }

.el-tabs { display: flex; flex-wrap: wrap; gap: 8px; }
.el-tab { display: inline-flex; align-items: center; gap: 7px; padding: 8px 14px; border-radius: 12px;
  font-size: 12.5px; font-weight: 600; color: #9fb0c6; cursor: pointer;
  border: 1px solid rgba(255,255,255,.09); background: rgba(15,21,34,.6); transition: all .22s; }
.el-tab:hover { color: #e9eef5; border-color: rgba(255,255,255,.2); }
.el-tab[aria-selected="true"] { color: #06101f; font-weight: 700;
  background: linear-gradient(140deg, #bfe0ff, #8fb8ff 60%, #a78bfa);
  border-color: rgba(255,255,255,.4); box-shadow: 0 10px 30px rgba(120,170,255,.35); }

.el-btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  padding: 10px 16px; border-radius: 13px; font-size: 12.5px; font-weight: 650; cursor: pointer;
  border: 1px solid rgba(255,255,255,.14); background: rgba(20,28,46,.66); color: #dce7f7; transition: all .22s; }
.el-btn:hover:not(:disabled) { border-color: rgba(191,224,255,.5); color: #fff; box-shadow: 0 10px 26px rgba(70,130,255,.22); }
.el-btn.primary { background: linear-gradient(140deg, #cfe4ff, #8fb8ff 55%, #a78bfa); color: #06101f; border-color: transparent; font-weight: 750; }
.el-btn:disabled { opacity: .45; cursor: not-allowed; }
.el-btn.sm { padding: 7px 11px; font-size: 11.5px; border-radius: 10px; }

.el-bar.warn > span { background: linear-gradient(90deg, #f0a98a, #e0705c); }
.el-bar.ok > span { background: linear-gradient(90deg, #7fe0b0, #43b98a); }

.el-chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
  font-size: 11px; border: 1px solid rgba(255,255,255,.12); background: rgba(255,255,255,.05); color: #c3d2e4; }
.el-chip.warn { color: #f6c9b4; border-color: rgba(240,169,138,.4); background: rgba(240,169,138,.08); }
.el-chip.ok { color: #b7ecd2; border-color: rgba(127,224,176,.35); background: rgba(127,224,176,.08); }
.el-chip.info { color: #cbd9ff; border-color: rgba(143,184,255,.35); background: rgba(143,184,255,.08); }

.el-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.el-muted { color: #8fa1b8; font-size: 12.5px; line-height: 1.6; }
.el-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; color: #9fb2c9; }
.el-scroll { max-height: 470px; overflow: auto; }
.el-list { display: grid; gap: 10px; }
.el-item { border-radius: 14px; border: 1px solid rgba(255,255,255,.09);
  background: rgba(14,20,33,.55); padding: 13px 15px; }
.el-item.soft:hover { border-color: rgba(191,224,255,.35); }
.el-banner { display: flex; align-items: flex-start; gap: 9px; padding: 11px 14px; border-radius: 12px; font-size: 12.5px; }
.el-banner.err { color: #f6c2b4; background: rgba(120,44,30,.22); border: 1px solid rgba(240,140,110,.35); }
.el-banner.ok { color: #b7ecd2; background: rgba(24,64,46,.24); border: 1px solid rgba(127,224,176,.32); }
.el-spin { animation: el-rotate 1s linear infinite; }
@keyframes el-rotate { to { transform: rotate(360deg); } }
@media (max-width: 900px) { .el-hero { padding: 26px 18px 20px; } .el-orbit { display: none; } }

.el-input { width: 100%; padding: 11px 14px; border-radius: 13px; font-size: 13px; color: #e9eef5;
  background: rgba(6,10,18,.7); border: 1px solid rgba(255,255,255,.12); }
.el-input::placeholder { color: #6d7f97; }
.el-input:focus-visible, .el-btn:focus-visible, .el-tab:focus-visible {
  outline: 2px solid #bfe0ff; outline-offset: 2px; }

.el-grid { display: grid; gap: 16px; }
.el-stat { display: flex; flex-direction: column; gap: 4px; }
.el-stat .v { font-size: 26px; font-weight: 750; letter-spacing: -.03em; color: #f4f8ff; }
.el-stat .k { font-size: 10.5px; letter-spacing: .18em; text-transform: uppercase; color: #8497ad; }

.el-bar { height: 8px; border-radius: 999px; background: rgba(255,255,255,.08); overflow: hidden; }
.el-bar > span { display: block; height: 100%; border-radius: 999px;
  background: linear-gradient(90deg, #8fb8ff, #a78bfa); transition: width .6s cubic-bezier(.22,1,.36,1); }
`;

function ScoreBar({ value, tone }: { value: number; tone?: "ok" | "warn" }) {
  return (
    <div className={`el-bar${tone ? ` ${tone}` : ""}`} role="presentation">
      <span style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
    </div>
  );
}


// ----------------------------------------------------------------- page

function AuraBackground() {
  return (
    <div className="absolute inset-0 overflow-hidden pointer-events-none" style={{ zIndex: 0 }}>
      <div className="absolute -top-[20%] -left-[10%] w-[70%] h-[70%] rounded-full opacity-20 blur-[120px] mix-blend-screen" style={{ backgroundImage: "radial-gradient(circle at 50% 50%, #3D81E3, transparent)" }} />
      <div className="absolute top-[40%] -right-[10%] w-[60%] h-[60%] rounded-full opacity-10 blur-[120px] mix-blend-screen" style={{ backgroundImage: "radial-gradient(circle at 50% 50%, #8A2BE2, transparent)" }} />
      <div className="absolute -bottom-[20%] left-[20%] w-[80%] h-[80%] rounded-full opacity-15 blur-[150px] mix-blend-screen" style={{ backgroundImage: "radial-gradient(circle at 50% 50%, #A4F4FD, transparent)" }} />
      <div className="absolute inset-0 opacity-[0.03]" style={{ backgroundImage: "url('data:image/svg+xml,%3Csvg viewBox=%220 0 200 200%22 xmlns=%22http://www.w3.org/2000/svg%22%3E%3Cfilter id=%22noiseFilter%22%3E%3CfeTurbulence type=%22fractalNoise%22 baseFrequency=%220.65%22 numOctaves=%223%22 stitchTiles=%22stitch%22/%3E%3C/filter%3E%3Crect width=%22100%25%22 height=%22100%25%22 filter=%22url(%23noiseFilter)%22/%3E%3C/svg%3E')" }} />
    </div>
  );
}

export default function EduLearnerPage() {
  const [userId, setUserId] = useState("demo-learner");
  const [tab, setTab] = useState<TabId>("overview");
  const [loading, setLoading] = useState<string>("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [registry, setRegistry] = useState<Registry | null>(null);
  const [questions, setQuestions] = useState<Question[]>([]);
  const [answers, setAnswers] = useState<Record<string, AnswerResult>>({});
  const [selected, setSelected] = useState<Record<string, number>>({});
  const [chat, setChat] = useState<{ role: string; content: string; actions?: string[]; model?: string }[]>([]);
  const [message, setMessage] = useState("");
  const [report, setReport] = useState<Record<string, unknown> | null>(null);
  const [profile, setProfile] = useState<FullProfile | null>(null);
  const [pform, setPform] = useState({
    name: "", education: "", experience: "", goal: "", target_role: "",
    learning_style: "mixed", available_hours: 7,
  });
  const [newSkill, setNewSkill] = useState("");
  const [newCert, setNewCert] = useState("");
  const [newProject, setNewProject] = useState({ title: "", stack: "", description: "" });
  const chatEnd = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const style = document.createElement("style");
    style.textContent = STYLES;
    document.head.appendChild(style);
    return () => { document.head.removeChild(style); };
  }, []);

  useEffect(() => {
    const saved = window.localStorage.getItem("edulearner:user");
    if (saved) setUserId(saved);
    call<Registry>("/agents").then(setRegistry).catch(() => setRegistry(null));
  }, []);

  useEffect(() => { chatEnd.current?.scrollIntoView({ behavior: "smooth" }); }, [chat]);

  const load = useCallback(async (id: string) => {
    if (!id.trim()) return;
    setLoading("dashboard"); setError("");
    try {
      const data = await call<Dashboard>(`/dashboard/${encodeURIComponent(id)}`);
      setDashboard(data);
      window.localStorage.setItem("edulearner:user", id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load learner");
    } finally { setLoading(""); }
  }, []);

  useEffect(() => { void load(userId); }, [load, userId]);

  const loadProfile = useCallback(async (id: string) => {
    if (!id.trim()) return;
    try {
      const data = await call<FullProfile>(`/profile/${encodeURIComponent(id)}`);
      setProfile(data);
      setPform({
        name: data.name ?? "", education: data.education ?? "",
        experience: data.experience ?? "", goal: data.goal ?? "",
        target_role: data.target_role ?? "",
        learning_style: data.learning_style || "mixed",
        available_hours: data.available_hours ?? 7,
      });
    } catch { setProfile(null); }
  }, []);

  useEffect(() => { void loadProfile(userId); }, [loadProfile, userId]);

  const refreshAll = useCallback(async () => {
    await Promise.all([load(userId), loadProfile(userId)]);
  }, [load, loadProfile, userId]);

  const run = useCallback(async (label: string, task: () => Promise<string | void>) => {
    setLoading(label); setError(""); setNotice("");
    try {
      const result = await task();
      if (result) setNotice(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally { setLoading(""); }
  }, []);

  const onboard = (demo: boolean) => run("onboard", async () => {
    const payload = demo
      ? { user_id: userId, ...DEMO_PROFILE, resume_text: "" }
      : { user_id: userId };
    const data = await call<{ analysis: { gaps: Gap[] } }>("/onboard", {
      method: "POST", body: JSON.stringify(payload),
    });
    await refreshAll();
    return `Analysis complete from evidence — ${data.analysis.gaps.length} gaps prioritised.`;
  });

  const planToday = () => run("plan", async () => {
    await call(`/plan/today?user_id=${encodeURIComponent(userId)}`, { method: "POST" });
    await load(userId);
    return "Today's plan re-generated from the current adaptive state.";
  });

  const actOnTask = (taskId: string, action: "COMPLETE" | "SKIP" | "STUCK") =>
    run(`task:${taskId}`, async () => {
      const data = await call<{ adaptive: { adaptation: { decision: string; skill?: string; changes: string[] } } }>(
        "/plan/task",
        { method: "POST", body: JSON.stringify({ user_id: userId, task_id: taskId, action, time_spent_minutes: 25 }) },
      );
      await load(userId);
      const a = data.adaptive.adaptation;
      return `Adaptive engine: ${a.decision}${a.skill ? ` on ${a.skill}` : ""} — ${a.changes[0] ?? ""}`;
    });

  const generatePractice = (skill?: string, count = 4) => run("practice", async () => {
    const data = await call<{ questions: Question[] }>(
      `/practice/generate?user_id=${encodeURIComponent(userId)}&count=${count}` +
      (skill ? `&skill=${encodeURIComponent(skill)}` : ""),
      { method: "POST" },
    );
    setQuestions(data.questions);
    setAnswers({}); setSelected({});
    return `${data.questions.length} marked questions ready${skill ? ` on ${skill}` : ""}.`;
  });

  const submitAnswer = (question: Question) =>
    run(`answer:${question.practice_id}`, async () => {
      const choice = selected[question.practice_id];
      const data = await call<AnswerResult>("/practice/answer", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId, practice_id: question.practice_id,
          selected_index: choice ?? null, time_spent_minutes: 4,
        }),
      });
      setAnswers((prev) => ({ ...prev, [question.practice_id]: data }));
      await load(userId);
      return data.adaptive
        ? `Adaptive engine: ${data.adaptive.decision}${data.adaptive.skill ? ` on ${data.adaptive.skill}` : ""}`
        : undefined;
    });

  const ask = (text: string) => run("chat", async () => {
    const data = await call<{ answer: string; actions: string[]; evidence: string[]; llm_used: boolean; llm_model: string }>(
      "/chat", { method: "POST", body: JSON.stringify({ user_id: userId, message: text }) },
    );
    setChat((prev) => [
      ...prev,
      { role: "user", content: text },
      { role: "agent", content: data.answer, actions: data.actions, model: data.llm_model },
    ]);
  });

  // ------------------------------------------------------------- profile
  const saveProfile = () => run("profile:save", async () => {
    await call<FullProfile>(`/profile/${encodeURIComponent(userId)}`, {
      method: "PUT", body: JSON.stringify(pform),
    });
    await refreshAll();
    return "Profile saved — gaps, roadmap and plan re-computed from the new evidence.";
  });

  const addSkill = () => run("profile:skill", async () => {
    const skill = newSkill.trim();
    if (!skill) return;
    const data = await call<FullProfile>(`/profile/${encodeURIComponent(userId)}/skills`, {
      method: "POST", body: JSON.stringify({ skill }),
    });
    setNewSkill("");
    await refreshAll();
    return `Skill "${skill}" added — ${data.analysis_summary?.gaps ?? 0} gaps now prioritised.`;
  });

  const removeSkill = (skill: string) => run(`profile:skill:${skill}`, async () => {
    await call(`/profile/${encodeURIComponent(userId)}/skills/${encodeURIComponent(skill)}`, { method: "DELETE" });
    await refreshAll();
    return `Skill "${skill}" removed.`;
  });

  const addProject = () => run("profile:project", async () => {
    const title = newProject.title.trim();
    if (!title) return;
    await call<FullProfile>(`/profile/${encodeURIComponent(userId)}/projects`, {
      method: "POST",
      body: JSON.stringify({
        title,
        stack: newProject.stack.split(",").map((s) => s.trim()).filter(Boolean),
        description: newProject.description.trim(),
      }),
    });
    setNewProject({ title: "", stack: "", description: "" });
    await refreshAll();
    return `Project "${title}" added to the evidence base.`;
  });

  const removeProject = (projectId: string) => run(`profile:project:${projectId}`, async () => {
    await call(`/profile/${encodeURIComponent(userId)}/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
    await refreshAll();
    return "Project removed.";
  });

  const addCertification = () => run("profile:cert", async () => {
    const certification = newCert.trim();
    if (!certification) return;
    await call<FullProfile>(`/profile/${encodeURIComponent(userId)}/certifications`, {
      method: "POST", body: JSON.stringify({ certification }),
    });
    setNewCert("");
    await refreshAll();
    return `Certification added.`;
  });

  const removeCertification = (certification: string) =>
    run(`profile:cert:${certification}`, async () => {
      await call(`/profile/${encodeURIComponent(userId)}/certifications/${encodeURIComponent(certification)}`, { method: "DELETE" });
      await refreshAll();
      return "Certification removed.";
    });

  const doUploadResume = (file: File) => run("profile:resume", async () => {
    const data = await upload<{ resume: { filename: string; characters: number } }>(
      `/profile/${encodeURIComponent(userId)}/resume`, file);
    await refreshAll();
    return `Resume "${data.resume.filename}" parsed — ${data.resume.characters} characters extracted and analysed.`;
  });

  const doDeleteResume = () => run("profile:resume-del", async () => {
    await call(`/profile/${encodeURIComponent(userId)}/resume`, { method: "DELETE" });
    await refreshAll();
    return "Resume removed from the profile.";
  });

  const generateReport = () => run("report", async () => {
    const data = await call<Record<string, unknown>>(
      `/report/generate?user_id=${encodeURIComponent(userId)}`, { method: "POST" });
    setReport(data);
    return "Progress report generated from recorded evidence.";
  });

  const topGap = dashboard?.gaps?.[0];
  const remediationSkills = useMemo(
    () => Object.entries(dashboard?.remediation ?? {}).filter(([, v]) => v?.active).map(([k]) => k),
    [dashboard],
  );
  const busy = loading !== "";

  const readinessRing = (value: number) => {
    const radius = 52;
    const circumference = 2 * Math.PI * radius;
    const filled = (Math.max(0, Math.min(100, value)) / 100) * circumference;
    return (
      <svg width="132" height="132" viewBox="0 0 132 132" role="img"
           aria-label={`Interview readiness ${Math.round(value)} out of 100`}>
        <defs>
          <linearGradient id="el-ring-grad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#8fb8ff" />
            <stop offset="100%" stopColor="#a78bfa" />
          </linearGradient>
        </defs>
        <circle cx="66" cy="66" r={radius} fill="none" stroke="rgba(255,255,255,.07)" strokeWidth="9" />
        <circle
          cx="66" cy="66" r={radius} fill="none" stroke="url(#el-ring-grad)" strokeWidth="9"
          strokeLinecap="round" strokeDasharray={`${filled} ${circumference}`}
          transform="rotate(-90 66 66)"
        />
        <text x="66" y="62" textAnchor="middle" fill="#cfe0f5" fontSize="26" fontWeight="600">
          {Math.round(value)}
        </text>
        <text x="66" y="82" textAnchor="middle" fill="#8497ad" fontSize="10" letterSpacing="2">
          READINESS
        </text>
      </svg>
    );
  };


  return (
    <main className="relative min-h-screen w-full bg-[#0c0c0c] text-white" style={{ fontFamily: "'Inter', system-ui, sans-serif" }}>
      <style>{STYLES}</style>
      <AuraBackground />
      
      {/* MAC-OS STYLE MENU BAR */}
      <div className="relative z-20 w-full h-10 bg-black/40 backdrop-blur-md border-b border-white/10 flex items-center justify-between px-6 text-xs text-white/70">
        <div className="flex items-center gap-4 font-medium">
          <Apple className="w-3.5 h-3.5 text-white" />
          <span className="font-bold text-white">Aura Core</span>
        </div>
        <div className="flex items-center gap-4">
           <span>{new Date().toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</span>
        </div>
      </div>

      <div className="relative z-10 mx-auto max-w-[1400px] px-4 sm:px-6 lg:px-8 py-10">
        
        {/* HERO SECTION */}
        <div className="text-center mb-16 flex flex-col items-center">
           <h1 className="text-4xl md:text-6xl font-semibold tracking-tight leading-[0.9]">
             EduLearner.<br/>
             <span className="animate-shiny bg-clip-text text-transparent inline-block mt-2 pb-2" style={{ backgroundImage: "linear-gradient(to right, #091020 0%, #0B2551 12.5%, #A4F4FD 32.5%, #00d2ff 50%, #0B2551 67.5%, #091020 87.5%, #091020 100%)", backgroundSize: "200% auto", filter: "url(#c3-noise)" }}>
               Intelligence
             </span>
           </h1>
           <p className="mt-6 text-white/60 max-w-xl text-base leading-[1.5]">
             An agentic adaptive-learning engine: it reads evidenced skill gaps, re-plans your roadmap, marks your practice, and records exactly why every change was made — 13 specialised agents in one auditable loop.
           </p>
        </div>

        {/* MOCKUP INTERFACE (FULLY EXPANDABLE) */}
        <section className="relative rounded-2xl border border-white/10 bg-[#0e1014]/90 backdrop-blur-2xl shadow-2xl flex flex-col mb-32 overflow-hidden">
          {/* Mac window title bar */}
          <div className="h-10 bg-black/50 border-b border-white/10 flex items-center px-4 shrink-0 relative">
             <div className="flex gap-2">
                <div className="w-3 h-3 rounded-full bg-[#ff5f57]" />
                <div className="w-3 h-3 rounded-full bg-[#febc2e]" />
                <div className="w-3 h-3 rounded-full bg-[#28c840]" />
             </div>
             <div className="absolute inset-0 flex items-center justify-center text-xs text-white/50 font-medium pointer-events-none">
                Aura — EduLearner AI
             </div>
          </div>

          <div className="flex-1 grid grid-cols-12 min-h-[800px]">
             
             {/* LEFT SIDEBAR: Navigation & Status */}
             <div className="col-span-12 md:col-span-3 border-b md:border-b-0 md:border-r border-white/10 bg-black/30 flex flex-col">
                <div className="p-4 border-b border-white/10 shrink-0 bg-black/40">
                   <div className="text-[10px] font-mono uppercase tracking-widest text-white/40 mb-3">Engine Status</div>
                   <div className="flex flex-col gap-2">
                     <div className="flex items-center gap-2 text-xs text-white/80"><div className={`w-2 h-2 rounded-full ${registry ? 'bg-emerald-400' : 'bg-amber-400 animate-pulse'}`} /> {registry ? "Online" : "Connecting..."}</div>
                     <div className="text-xs text-white/50 font-mono">db: {registry?.database ?? "—"}</div>
                     <div className="text-xs text-white/50 font-mono">llm: {registry?.llm ?? "—"}</div>
                     {!!dashboard?.memory?.current_bottleneck && <div className="text-xs text-white/50 font-mono truncate">top gap: {String(dashboard.memory.current_bottleneck)}</div>}
                   </div>
                </div>
                
                <div className="flex-1 p-3 space-y-1">
                   <div className="text-[10px] font-mono text-white/30 uppercase tracking-widest px-3 mb-2 mt-2">Workspace Modules</div>
                   {TABS.map(({ id, label, icon: Icon }) => (
                     <button
                       key={id}
                       disabled={!dashboard}
                       onClick={() => setTab(id)}
                       className={`w-full flex items-center gap-3 text-left px-3 py-2.5 rounded-lg text-sm transition-all ${tab === id ? 'bg-white/10 text-white shadow-sm font-medium' : 'text-white/50 hover:bg-white/5 hover:text-white/90'} ${!dashboard ? 'opacity-30 cursor-not-allowed' : ''}`}
                     >
                       <Icon size={16} /> <span>{label}</span>
                     </button>
                   ))}
                </div>

                <div className="p-4 border-t border-white/10 shrink-0 bg-black/40 text-xs text-white/50 space-y-2">
                   <div>Learner: <span className="text-white/90">{userId}</span></div>
                   {dashboard && <div>Profile: <span className="text-emerald-400">{dashboard.profile_completion}%</span></div>}
                </div>
             </div>

             {/* RIGHT COLUMN: Toolbar & Panel */}
             <div className="col-span-12 md:col-span-9 flex flex-col bg-[#0e1014]/20">
                {!dashboard ? (
                   <div className="flex-1 flex flex-col items-center justify-center p-8 text-center">
                      <div className="w-16 h-16 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center mb-6">
                         <User size={32} className="text-white/30" />
                      </div>
                      <h2 className="text-xl font-semibold mb-2">Connect a Learner</h2>
                      <p className="text-white/50 max-w-sm mb-8 text-sm">Enter a Learner ID to pull evidence, analyze gaps, and adapt the curriculum.</p>
                      <div className="flex gap-2">
                        <input
                          className="el-input" style={{ width: 200 }} value={userId}
                          onChange={(event) => setUserId(event.target.value)}
                          placeholder="learner id" aria-label="Learner id"
                        />
                        <button className="el-btn primary" onClick={() => onboard(true)} disabled={busy}>
                          {loading === "onboard" ? <Loader2 size={15} className="el-spin" /> : <Sparkles size={15} />}
                          Run analysis
                        </button>
                      </div>
                      {(error || notice) && (
                        <div className="mt-8">
                          {error && <div className="el-banner err" role="alert"><XCircle size={15} /><span>{error}</span></div>}
                          {notice && !error && <div className="el-banner ok" role="status"><CheckCircle2 size={15} /><span>{notice}</span></div>}
                        </div>
                      )}
                   </div>
                ) : (
                   <>
                      {/* Top Action Toolbar */}
                      <div className="h-14 border-b border-white/10 flex items-center justify-between px-6 shrink-0 bg-black/20">
                         <div className="flex items-center gap-2">
                           <button className="el-btn sm" onClick={() => onboard(false)} disabled={busy}>
                             <RefreshCw size={14} /> Re-analyse
                           </button>
                           <button className="el-btn sm" onClick={planToday} disabled={busy}>
                             {loading === "plan" ? <Loader2 size={14} className="el-spin" /> : <CalendarCheck size={14} />} Plan today
                           </button>
                         </div>
                         <button className="el-btn sm" onClick={generateReport} disabled={busy || !dashboard}>
                           {loading === "report" ? <Loader2 size={14} className="el-spin" /> : <Activity size={14} />} Report
                         </button>
                      </div>
                      
                      {/* Notices */}
                      {(error || notice) && (
                        <div className="p-6 pb-0">
                          {error && <div className="el-banner err" role="alert"><XCircle size={15} /><span>{error}</span></div>}
                          {notice && !error && <div className="el-banner ok" role="status"><CheckCircle2 size={15} /><span>{notice}</span></div>}
                        </div>
                      )}

                      {/* Main Tab Panel */}
                      <div className="flex-1 p-6">
      <section
        role="tabpanel"
        id={`el-panel-${tab}`}
        aria-labelledby={`el-tab-${tab}`}
        className="el-grid"
        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(330px, 1fr))" }}
      >
        {tab === "profile" && profile && (
          <>
            {/* Identity + resume */}
            <div className="el-glass lift el-pad">
              <div className="el-row" style={{ marginBottom: 14 }}>
                <span className="k">Learner identity</span>
                <span className="el-chip info">stored in sara.db · edulearner_profiles</span>
              </div>
              <div className="el-list">
                {([
                  ["name", "Full name", "e.g. Aarav Sharma"],
                  ["education", "Education", "e.g. B.Tech Computer Science, 2nd year"],
                  ["experience", "Experience", "internships, jobs, teaching…"],
                  ["goal", "Learning goal", "what you want to achieve"],
                  ["target_role", "Target role", "e.g. Software Engineer"],
                ] as const).map(([field, label, placeholder]) => (
                  <label key={field} style={{ display: "block" }}>
                    <span className="el-muted" style={{ display: "block", marginBottom: 5 }}>{label}</span>
                    <input
                      className="el-input"
                      value={pform[field]}
                      placeholder={placeholder}
                      aria-label={label}
                      onChange={(event) => setPform((prev) => ({ ...prev, [field]: event.target.value }))}
                    />
                  </label>
                ))}
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                  <label style={{ display: "block" }}>
                    <span className="el-muted" style={{ display: "block", marginBottom: 5 }}>Learning style</span>
                    <select
                      className="el-input"
                      value={pform.learning_style}
                      aria-label="Learning style"
                      onChange={(event) => setPform((prev) => ({ ...prev, learning_style: event.target.value }))}
                    >
                      <option value="mixed">mixed</option>
                      <option value="hands-on">hands-on</option>
                      <option value="theory">theory</option>
                      <option value="visual">visual</option>
                    </select>
                  </label>
                  <label style={{ display: "block" }}>
                    <span className="el-muted" style={{ display: "block", marginBottom: 5 }}>Available hours / day</span>
                    <input
                      className="el-input" type="number" min={0} max={80} step={0.5}
                      value={pform.available_hours}
                      aria-label="Available hours per day"
                      onChange={(event) => setPform((prev) => ({ ...prev, available_hours: Number(event.target.value) || 0 }))}
                    />
                  </label>
                </div>
                <div className="el-row" style={{ justifyContent: "flex-start", gap: 8 }}>
                  <button className="el-btn primary" disabled={busy} onClick={saveProfile}>
                    {loading === "profile:save" ? <Loader2 size={15} className="el-spin" /> : <CheckCircle2 size={15} />}
                    Save profile &amp; re-analyse
                  </button>
                </div>
              </div>
            </div>

            {/* Resume upload */}
            <div className="el-glass lift el-pad">
              <div className="el-row" style={{ marginBottom: 14 }}>
                <span className="k">Resume</span>
                {profile.resume ? (
                  <span className="el-chip ok"><FileText size={11} /> {profile.resume.filename}</span>
                ) : (
                  <span className="el-chip">no resume uploaded</span>
                )}
              </div>
              <p className="el-muted">
                Upload a PDF, DOCX or TXT resume — the engine extracts the text and folds it into the
                skill evidence, so gaps and roadmaps reflect your real experience.
              </p>
              <label className="el-btn primary" style={{ marginTop: 12, cursor: "pointer" }}>
                {loading === "profile:resume" ? <Loader2 size={15} className="el-spin" /> : <FileUp size={15} />}
                {profile.resume ? "Replace resume" : "Upload resume (PDF / DOCX / TXT)"}
                <input
                  type="file" accept=".pdf,.docx,.txt" style={{ display: "none" }}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    event.target.value = "";
                    if (file) doUploadResume(file);
                  }}
                />
              </label>
              {profile.resume && (
                <div className="el-row" style={{ marginTop: 10, justifyContent: "flex-start", gap: 8 }}>
                  <span className="el-mono">uploaded {profile.resume.uploaded_at?.slice(0, 10)}</span>
                  <button className="el-btn sm" disabled={busy} onClick={doDeleteResume}>
                    <Trash2 size={13} /> Remove resume
                  </button>
                </div>
              )}
              {profile.resume_text && (
                <details style={{ marginTop: 12 }}>
                  <summary className="el-muted" style={{ cursor: "pointer" }}>Extracted text preview</summary>
                  <p className="el-mono" style={{
                    marginTop: 8, maxHeight: 160, overflow: "auto", whiteSpace: "pre-wrap",
                    background: "rgba(6,10,18,.6)", padding: 12, borderRadius: 10,
                  }}>
                    {profile.resume_text.slice(0, 1200)}{profile.resume_text.length > 1200 ? "…" : ""}
                  </p>
                </details>
              )}
            </div>

            {/* Skills add/remove */}
            <div className="el-glass lift el-pad">
              <div className="el-row" style={{ marginBottom: 14 }}>
                <span className="k">Skills ({profile.skills.length})</span>
              </div>
              <div className="el-list" style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
                {profile.skills.map((skill) => (
                  <span key={skill} className="el-chip info">
                    {skill}
                    <button
                      aria-label={`Remove skill ${skill}`} title={`Remove ${skill}`}
                      style={{ background: "none", border: "none", cursor: "pointer", color: "var(--sara-warn)", padding: 0, display: "inline-flex" }}
                      disabled={busy} onClick={() => removeSkill(skill)}
                    >
                      <XCircle size={13} />
                    </button>
                  </span>
                ))}
                {profile.skills.length === 0 && <p className="el-muted">No skills yet — add what you know.</p>}
              </div>
              <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
                <input
                  className="el-input" value={newSkill} placeholder="Add a skill, e.g. Docker"
                  aria-label="New skill"
                  onChange={(event) => setNewSkill(event.target.value)}
                  onKeyDown={(event) => { if (event.key === "Enter") addSkill(); }}
                />
                <button className="el-btn" disabled={busy || !newSkill.trim()} onClick={addSkill}>
                  {loading === "profile:skill" ? <Loader2 size={14} className="el-spin" /> : <Plus size={14} />}
                  Add
                </button>
              </div>
            </div>

            {/* Projects add/remove */}
            <div className="el-glass lift el-pad">
              <div className="el-row" style={{ marginBottom: 14 }}>
                <span className="k">Projects ({profile.projects.length})</span>
              </div>
              <div className="el-list">
                {profile.projects.map((project) => (
                  <div key={project.id ?? project.title} className="el-item">
                    <div className="el-row">
                      <span style={{ fontSize: 13, fontWeight: 650 }}>{project.title}</span>
                      <button
                        aria-label={`Remove project ${project.title}`} title="Remove project"
                        style={{ background: "none", border: "none", cursor: "pointer", color: "var(--sara-warn)", display: "inline-flex" }}
                        disabled={busy} onClick={() => project.id && removeProject(project.id)}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                    {project.stack?.length > 0 && (
                      <div className="el-list" style={{ flexDirection: "row", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                        {project.stack.map((tech) => <span key={tech} className="el-chip">{tech}</span>)}
                      </div>
                    )}
                    {project.description && (
                      <p className="el-muted" style={{ marginTop: 8 }}>{project.description}</p>
                    )}
                  </div>
                ))}
                {profile.projects.length === 0 && <p className="el-muted">No projects yet — they count as strong skill evidence.</p>}
              </div>
              <div className="el-list" style={{ marginTop: 14 }}>
                <input
                  className="el-input" value={newProject.title} placeholder="Project title, e.g. Study planner app"
                  aria-label="Project title"
                  onChange={(event) => setNewProject((prev) => ({ ...prev, title: event.target.value }))}
                />
                <input
                  className="el-input" value={newProject.stack} placeholder="Stack, comma separated: React, Python, MongoDB"
                  aria-label="Project stack"
                  onChange={(event) => setNewProject((prev) => ({ ...prev, stack: event.target.value }))}
                />
                <textarea
                  className="el-input" rows={2} value={newProject.description}
                  placeholder="One line about what it does (optional)"
                  aria-label="Project description"
                  onChange={(event) => setNewProject((prev) => ({ ...prev, description: event.target.value }))}
                />
                <button className="el-btn" disabled={busy || !newProject.title.trim()} onClick={addProject}>
                  {loading === "profile:project" ? <Loader2 size={14} className="el-spin" /> : <Plus size={14} />}
                  Add project
                </button>
              </div>
            </div>

            {/* Certifications add/remove */}
            <div className="el-glass lift el-pad">
              <div className="el-row" style={{ marginBottom: 14 }}>
                <span className="k">Certifications ({profile.certifications.length})</span>
              </div>
              <div className="el-list" style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
                {profile.certifications.map((certification) => (
                  <span key={certification} className="el-chip ok">
                    {certification}
                    <button
                      aria-label={`Remove certification ${certification}`} title="Remove"
                      style={{ background: "none", border: "none", cursor: "pointer", color: "var(--sara-warn)", padding: 0, display: "inline-flex" }}
                      disabled={busy} onClick={() => removeCertification(certification)}
                    >
                      <XCircle size={13} />
                    </button>
                  </span>
                ))}
                {profile.certifications.length === 0 && <p className="el-muted">No certifications yet.</p>}
              </div>
              <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
                <input
                  className="el-input" value={newCert} placeholder="Add a certification, e.g. AWS Cloud Practitioner"
                  aria-label="New certification"
                  onChange={(event) => setNewCert(event.target.value)}
                  onKeyDown={(event) => { if (event.key === "Enter") addCertification(); }}
                />
                <button className="el-btn" disabled={busy || !newCert.trim()} onClick={addCertification}>
                  {loading === "profile:cert" ? <Loader2 size={14} className="el-spin" /> : <Plus size={14} />}
                  Add
                </button>
              </div>
            </div>
          </>
        )}

        {tab === "profile" && !profile && (
          <div className="el-glass el-pad" style={{ gridColumn: "1 / -1" }}>
            <span className="k">Profile</span>
            <p className="el-muted" style={{ marginTop: 10 }}>
              Could not load the profile for “{userId}”. Save it once from this tab (or run the
              onboarding analysis) to create the database row.
            </p>
          </div>
        )}

        {tab === "overview" && (
          <>
            <div className="el-glass lift el-pad">
              <div className="el-row">
                <div className="el-stat">
                  <span className="k">Interview readiness</span>
                  <span className="v">
                    {Math.round(dashboard.readiness?.overall ?? 0)}
                    <span style={{ fontSize: 14, color: "#8497ad" }}>/100</span>
                  </span>
                  <span className="el-muted">
                    confidence {Math.round((dashboard.readiness?.confidence ?? 0) * 100)}% · {" "}
                    {dashboard.progress.length} records analysed
                  </span>
                </div>
                {readinessRing(dashboard.readiness?.overall ?? 0)}
              </div>
              <div className="el-list" style={{ marginTop: 14 }}>
                {remediationSkills.length > 0 ? remediationSkills.map((skill) => (
                  <span key={skill} className="el-chip warn">
                    <CircleDot size={11} /> remediation active: {skill}
                  </span>
                )) : (
                  <span className="el-chip ok">no remediation active</span>
                )}
              </div>
            </div>

            <div className="el-glass lift el-pad">
              <div className="el-row" style={{ marginBottom: 12 }}>
                <div className="el-stat">
                  <span className="k">Prioritised gaps</span>
                  <span className="v">{dashboard.gaps.filter((gap) => gap.gap > 0).length}</span>
                </div>
                <span className="el-chip info">{dashboard.profile.target_role || "target role unset"}</span>
              </div>
              <div className="el-list el-scroll">
                {dashboard.gaps.slice(0, 6).map((gap) => (
                  <button
                    key={gap.skill}
                    className="el-item soft"
                    style={{ textAlign: "left", cursor: "pointer" }}
                    onClick={() => generatePractice(gap.skill)}
                    title={`Practise ${gap.skill}`}
                  >
                    <div className="el-row">
                      <span style={{ fontSize: 13, fontWeight: 650 }}>{gap.skill}</span>
                      <span className="el-mono">
                        {Math.round(gap.current_score)} → {Math.round(gap.target_score)}
                      </span>
                    </div>
                    <div style={{ marginTop: 8 }}>
                      <ScoreBar
                        value={gap.current_score}
                        tone={gap.classification === "WEAK" || gap.classification === "MISSING" ? "warn" : undefined}
                      />
                    </div>
                    <div className="el-row" style={{ marginTop: 8 }}>
                      <span className="el-mono">priority {gap.priority.toFixed(1)}</span>
                      <span className="el-chip">{gap.classification}</span>
                    </div>
                    {gap.why.length > 0 && (
                      <p className="el-muted" style={{ marginTop: 6 }}>{gap.why[0]}</p>
                    )}
                  </button>
                ))}
              </div>
            </div>

            <div className="el-glass lift el-pad">
              <span className="k">Skill inventory (evidence-weighted)</span>
              <div className="el-list" style={{ marginTop: 12 }}>
                {dashboard.skills.slice(0, 9).map((skill) => (
                  <div key={skill.name}>
                    <div className="el-row">
                      <span style={{ fontSize: 12.5 }}>{skill.name}</span>
                      <span className="el-mono">{Math.round(skill.score)} · {skill.level}</span>
                    </div>
                    <div style={{ marginTop: 6 }}>
                      <ScoreBar value={skill.score} tone={skill.score >= 70 ? "ok" : undefined} />
                    </div>
                  </div>
                ))}
                {dashboard.skills.length === 0 && (
                  <p className="el-muted">No skill evidence yet — run the onboarding analysis.</p>
                )}
              </div>
            </div>
            {report && (
              <div className="el-glass lift el-pad" style={{ gridColumn: "1 / -1" }}>
                <span className="k">Latest progress report</span>
                <div className="el-grid" style={{ marginTop: 12, gridTemplateColumns: "repeat(auto-fit, minmax(240px,1fr))" }}>
                  <div>
                    <p className="el-mono">acquired: {(report.acquired_skills as string[])?.join(", ") || "—"}</p>
                    <p className="el-mono">weaknesses: {(report.persistent_weaknesses as string[])?.join(", ") || "—"}</p>
                    <p className="el-mono">learning hours: {String(report.learning_hours ?? 0)}</p>
                  </div>
                  <div>
                    <p className="el-muted"><strong>Roadmap changes:</strong></p>
                    <ul className="el-muted" style={{ paddingLeft: 18, margin: 0 }}>
                      {((report.roadmap_changes as string[]) ?? []).slice(0, 5).map((item, index) => (
                        <li key={index}>{item}</li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <p className="el-muted"><strong>Next steps:</strong></p>
                    <ul className="el-muted" style={{ paddingLeft: 18, margin: 0 }}>
                      {((report.next_steps as string[]) ?? []).map((item, index) => <li key={index}>{item}</li>)}
                    </ul>
                  </div>
                </div>
              </div>
            )}
          </>
        )}

        {tab === "today" && (
          <div className="el-glass el-pad" style={{ gridColumn: "1 / -1" }}>
            <div className="el-row" style={{ marginBottom: 14 }}>
              <div className="el-stat">
                <span className="k">Today&apos;s plan</span>
                <span className="v">
                  {dashboard.plan.length} tasks ·{" "}
                  {dashboard.plan.reduce((sum, task) => sum + task.estimated_minutes, 0)} min
                </span>
              </div>
              <button className="el-btn" onClick={planToday} disabled={busy}>
                <RefreshCw size={14} /> Re-plan
              </button>
            </div>
            <div className="el-list">
              {dashboard.plan.map((task) => (
                <div key={task.task_id} className="el-item">
                  <div className="el-row" style={{ alignItems: "flex-start" }}>
                    <div>
                      <p style={{ fontSize: 13.5, fontWeight: 650, margin: 0 }}>{task.title}</p>
                      <p className="el-muted" style={{ margin: "6px 0 0" }}>{task.rationale}</p>
                    </div>
                    <div className="el-list" style={{ justifyItems: "flex-end", gap: 6 }}>
                      <span className="el-chip info">{task.difficulty}</span>
                      <span className="el-mono">{task.estimated_minutes} min</span>
                    </div>
                  </div>
                  <div className="el-row" style={{ marginTop: 12, justifyContent: "flex-start", gap: 8 }}>
                    <button className="el-btn sm" disabled={busy} onClick={() => actOnTask(task.task_id, "COMPLETE")}>
                      <CheckCircle2 size={13} /> Complete
                    </button>
                    <button className="el-btn sm" disabled={busy} onClick={() => actOnTask(task.task_id, "STUCK")}>
                      <XCircle size={13} /> I&apos;m stuck
                    </button>
                    <button className="el-btn sm" disabled={busy} onClick={() => actOnTask(task.task_id, "SKIP")}>
                      <ChevronRight size={13} /> Skip
                    </button>
                    <button className="el-btn sm" disabled={busy} onClick={() => generatePractice(task.skill)}>
                      Practise this skill
                    </button>
                  </div>
                </div>
              ))}
              {dashboard.plan.length === 0 && (
                <p className="el-muted">No tasks yet — press <strong>Plan today</strong>.</p>
              )}
            </div>
          </div>
        )}

        {tab === "roadmap" && (
          <div className="el-glass el-pad" style={{ gridColumn: "1 / -1" }}>
            <div className="el-row" style={{ marginBottom: 12 }}>
              <div className="el-stat">
                <span className="k">Adaptive roadmap</span>
                <span className="v">
                  {dashboard.roadmap.total_weeks ?? 0} weeks · {dashboard.roadmap.total_objectives ?? 0} objectives
                </span>
                <span className="el-muted">{dashboard.roadmap.reason}</span>
              </div>
              <button
                className="el-btn"
                disabled={busy}
                onClick={() => run("roadmap", async () => {
                  await call(`/adaptive/analyze?user_id=${encodeURIComponent(userId)}`, { method: "POST" });
                  await load(userId);
                  return "Roadmap re-generated from the latest evidence.";
                })}
              >
                <RefreshCw size={14} /> Re-generate
              </button>
            </div>
            {(dashboard.roadmap.deferred_gaps?.length ?? 0) > 0 && (
              <p className="el-muted" style={{ marginBottom: 12 }}>
                Deferred beyond the horizon: {dashboard.roadmap.deferred_gaps?.join(", ")}
              </p>
            )}
            <div className="el-list el-scroll">
              {(dashboard.roadmap.phases ?? []).map((phase) => (
                <div key={phase.phase} className="el-item">
                  <div className="el-row">
                    <span style={{ fontSize: 13.5, fontWeight: 700 }}>{phase.title}</span>
                    <span className="el-chip info">phase {phase.phase}</span>
                  </div>
                  <p className="el-muted" style={{ margin: "6px 0 10px" }}>{phase.expected_outcome}</p>
                  <div className="el-list">
                    {phase.weeks.map((week) => (
                      <div key={week.week} style={{ borderLeft: "2px solid rgba(143,184,255,.35)", paddingLeft: 12 }}>
                        <div className="el-row">
                          <span style={{ fontSize: 12.5, fontWeight: 650 }}>Week {week.week} · {week.theme}</span>
                          <span className="el-mono">{week.tasks.length} tasks</span>
                        </div>
                        <ul className="el-muted" style={{ paddingLeft: 16, margin: "6px 0 0" }}>
                          {week.objectives.slice(0, 3).map((objective) => (
                            <li key={objective.title}>{objective.title}</li>
                          ))}
                        </ul>
                        <p className="el-mono" style={{ marginTop: 6 }}>
                          assessment: {week.assessment} · project: {week.project}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
              {(dashboard.roadmap.phases ?? []).length === 0 && (
                <p className="el-muted">
                  No roadmap yet — every gap is currently mastered, or the analysis has not run.
                </p>
              )}
            </div>
          </div>
        )}

        {tab === "practice" && (
          <>
            <div className="el-glass el-pad" style={{ gridColumn: "1 / -1" }}>
              <div className="el-row" style={{ flexWrap: "wrap", gap: 10, justifyContent: "flex-start" }}>
                <button className="el-btn primary" disabled={busy}
                        onClick={() => generatePractice(topGap?.skill, 4)}>
                  {loading === "practice" ? <Loader2 size={15} className="el-spin" /> : <BookOpen size={15} />}
                  Generate on top gap{topGap ? ` (${topGap.skill})` : ""}
                </button>
                {dashboard.gaps.slice(0, 4).map((gap) => (
                  <button key={gap.skill} className="el-btn sm" disabled={busy}
                          onClick={() => generatePractice(gap.skill, 3)}>
                    {gap.skill}
                  </button>
                ))}
              </div>
              <p className="el-muted" style={{ marginTop: 10 }}>
                Questions come from the curated item bank and are marked by the engine; every answer
                feeds the adaptive loop (correct answers accelerate, failures trigger remediation).
              </p>
            </div>

            {questions.map((question) => {
              const result = answers[question.practice_id];
              const choice = selected[question.practice_id];
              const isMcq = question.options.length > 0;
              return (
                <div key={question.practice_id} className="el-glass lift el-pad" style={{ gridColumn: "1 / -1" }}>
                  <div className="el-row" style={{ alignItems: "flex-start" }}>
                    <div>
                      <span className="el-chip info">{question.skill}</span>{" "}
                      <span className="el-chip">{question.difficulty}</span>{" "}
                      <span className="el-mono">{question.topic}</span>
                    </div>
                    <span className="el-mono">{question.estimated_minutes} min</span>
                  </div>
                  <p style={{ margin: "12px 0", fontSize: 14, lineHeight: 1.6 }}>{question.question}</p>

                  {isMcq ? (
                    <div className="el-list" role="radiogroup" aria-label={`Options for ${question.topic}`}>
                      {question.options.map((option, index) => (
                        <button
                          key={option}
                          role="radio"
                          aria-checked={choice === index}
                          className="el-item soft"
                          style={{
                            textAlign: "left", cursor: "pointer",
                            borderColor: choice === index ? "rgba(191,224,255,.6)" : undefined,
                            background: choice === index ? "rgba(143,184,255,.12)" : undefined,
                          }}
                          onClick={() => setSelected((prev) => ({ ...prev, [question.practice_id]: index }))}
                        >
                          <span className="el-mono">{String.fromCharCode(65 + index)}.</span> {option}
                        </button>
                      ))}
                    </div>
                  ) : (
                    <p className="el-muted">
                      Open question — answer in your own workspace and record the attempt.
                      Evaluation criteria: {question.evaluation_criteria.join(", ")}
                    </p>
                  )}

                  {question.hints.length > 0 && !result && (
                    <p className="el-muted" style={{ marginTop: 10 }}>Hint: {question.hints[0]}</p>
                  )}

                  {result ? (
                    <div className={`el-banner ${result.correct ? "ok" : "err"}`} style={{ marginTop: 12 }}
                         role="status" aria-live="polite">
                      {result.correct ? <CheckCircle2 size={15} /> : <XCircle size={15} />}
                      <div>
                        <strong>{result.correct ? "Correct" : "Incorrect"}</strong> · score {Math.round(result.score)}/100
                        {result.explanation && <p style={{ margin: "6px 0 0" }}>{result.explanation}</p>}
                        {!result.correct && result.misconceptions.length > 0 && (
                          <p style={{ margin: "6px 0 0" }}>Watch out: {result.misconceptions[0]}</p>
                        )}
                        {!result.correct && result.remediation?.active && (
                          <p style={{ margin: "6px 0 0" }}>
                            Remediation is now active on this skill — foundations are re-planned first.
                          </p>
                        )}
                      </div>
                    </div>
                  ) : (
                    <button className="el-btn primary" style={{ marginTop: 12 }} disabled={busy}
                            onClick={() => submitAnswer(question)}>
                      {loading === `answer:${question.practice_id}`
                        ? <Loader2 size={15} className="el-spin" />
                        : <CheckCircle2 size={15} />}
                      Submit answer
                    </button>
                  )}
                </div>
              );
            })}
          </>
        )}

        {tab === "readiness" && dashboard.readiness && (
          <>
            <div className="el-glass lift el-pad">
              <div className="el-row">
                <div className="el-stat">
                  <span className="k">Interview readiness</span>
                  <span className="v">
                    {Math.round(dashboard.readiness.overall)}
                    <span style={{ fontSize: 14, color: "#8497ad" }}>/100</span>
                  </span>
                  <span className="el-muted">
                    evidence confidence {Math.round(dashboard.readiness.confidence * 100)}%
                  </span>
                </div>
                {readinessRing(dashboard.readiness.overall)}
              </div>
              <div className="el-list" style={{ marginTop: 14 }}>
                {dashboard.readiness.evidence.slice(0, 4).map((line) => (
                  <span key={line} className="el-mono">{line}</span>
                ))}
              </div>
            </div>

            <div className="el-glass lift el-pad">
              <span className="k">Dimensions</span>
              <div className="el-list" style={{ marginTop: 12 }}>
                {Object.entries(dashboard.readiness.category_scores ?? {}).map(([key, value]) => (
                  <div key={key}>
                    <div className="el-row">
                      <span style={{ fontSize: 12.5, textTransform: "capitalize" }}>
                        {key.replace(/_/g, " ")}
                      </span>
                      <span className="el-mono">{Math.round(value)}</span>
                    </div>
                    <div style={{ marginTop: 6 }}>
                      <ScoreBar value={value} tone={value < 45 ? "warn" : value >= 70 ? "ok" : undefined} />
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="el-glass lift el-pad">
              <span className="k">Blockers</span>
              <ul className="el-muted" style={{ marginTop: 10, paddingLeft: 18 }}>
                {dashboard.readiness.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
              </ul>
              <span className="k" style={{ display: "block", marginTop: 14 }}>Recommendations</span>
              <ul className="el-muted" style={{ marginTop: 10, paddingLeft: 18 }}>
                {dashboard.readiness.recommendations.map((item) => <li key={item}>{item}</li>)}
              </ul>
            </div>
          </>
        )}

        {tab === "mind" && (
          <>
            <div className="el-glass lift el-pad">
              <span className="k">Learner memory</span>
              <div className="el-list" style={{ marginTop: 12 }}>
                <p className="el-muted">
                  <strong>Bottleneck:</strong> {String(dashboard.memory.current_bottleneck ?? "none recorded")}
                </p>
                <p className="el-muted">
                  <strong>Strengths:</strong> {(dashboard.memory.strengths as string[])?.join(", ") || "—"}
                </p>
                <p className="el-muted">
                  <strong>Weaknesses:</strong> {(dashboard.memory.weaknesses as string[])?.join(", ") || "—"}
                </p>
                <p className="el-muted">
                  <strong>Preferred difficulty:</strong> {String(dashboard.memory.preferred_difficulty ?? "—")} ·{" "}
                  <strong>style:</strong> {String(dashboard.memory.learning_style ?? "mixed")}
                </p>
                <div className="el-list">
                  {((dashboard.memory.recurring_mistakes as string[]) ?? []).map((mistake) => (
                    <span key={mistake} className="el-chip warn">{mistake}</span>
                  ))}
                </div>
              </div>
            </div>

            <div className="el-glass lift el-pad">
              <span className="k">Practice history (per skill)</span>
              <div className="el-list el-scroll" style={{ marginTop: 12 }}>
                {Object.entries((dashboard.memory.practice_by_skill as Record<string, number[]>) ?? {}).map(
                  ([skill, scores]) => (
                    <div key={skill}>
                      <div className="el-row">
                        <span style={{ fontSize: 12.5 }}>{skill}</span>
                        <span className="el-mono">
                          {scores.length} attempts · last {Math.round(scores[scores.length - 1] ?? 0)}
                        </span>
                      </div>
                      <div style={{ display: "flex", gap: 4, marginTop: 6 }}>
                        {scores.slice(-10).map((score, index) => (
                          <span
                            key={index}
                            title={`${Math.round(score)}%`}
                            style={{
                              flex: 1, height: 22, borderRadius: 5,
                              background: score >= 80
                                ? "linear-gradient(180deg,#7fe0b0,#43b98a)"
                                : score >= 50
                                  ? "linear-gradient(180deg,#8fb8ff,#6a8fe0)"
                                  : "linear-gradient(180deg,#f0a98a,#d9684f)",
                              opacity: 0.9,
                            }}
                          />
                        ))}
                      </div>
                    </div>
                  ),
                )}
                {Object.keys((dashboard.memory.practice_by_skill as Record<string, number[]>) ?? {}).length === 0 && (
                  <p className="el-muted">No practice recorded yet — start with the Practice tab.</p>
                )}
              </div>
            </div>

            <div className="el-glass lift el-pad">
              <span className="k">Remediation state</span>
              <div className="el-list" style={{ marginTop: 12 }}>
                {remediationSkills.map((skill) => (
                  <div key={skill} className="el-item">
                    <span className="el-chip warn">{skill}</span>
                    <p className="el-muted" style={{ marginTop: 8 }}>
                      difficulty {dashboard.remediation[skill]?.difficulty ?? "EASY"} · prerequisites:{" "}
                      {(dashboard.remediation[skill]?.prerequisites ?? []).join(", ") || "fundamentals"}
                    </p>
                  </div>
                ))}
                {remediationSkills.length === 0 && (
                  <p className="el-muted">
                    No skill is in remediation — the learner is progressing normally.
                  </p>
                )}
              </div>
            </div>
          </>
        )}

        {tab === "mentor" && (
          <div className="el-glass el-pad" style={{ gridColumn: "1 / -1" }}>
            <div className="el-row" style={{ marginBottom: 12 }}>
              <span className="k">Mentor — grounded in your evidence</span>
              <span className="el-chip info">
                {registry?.llm === "configured" ? "LLM enhancement active" : "deterministic grounding only"}
              </span>
            </div>
            <div className="el-list el-scroll" style={{ minHeight: 160 }}>
              {chat.length === 0 && (
                <div className="el-list">
                  <p className="el-muted">
                    Ask anything about your plan, gaps, readiness or progress:
                  </p>
                  <div className="el-list" style={{ flexDirection: "row", flexWrap: "wrap" }}>
                    {["What should I study today?", "Am I interview ready?", "What are my weaknesses?"].map((prompt) => (
                      <button key={prompt} className="el-chip info" style={{ cursor: "pointer" }}
                              onClick={() => ask(prompt)}>
                        {prompt}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {chat.map((entry, index) => (
                <div
                  key={index}
                  className="el-item"
                  style={{ borderColor: entry.role === "user" ? "rgba(255,255,255,.14)" : "rgba(143,184,255,.35)" }}
                >
                  <span className="el-mono">
                    {entry.role === "user" ? "you" : "EduLearner AI"}{entry.model ? ` · ${entry.model}` : ""}
                  </span>
                  <p style={{ margin: "6px 0 0", fontSize: 13.5, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
                    {entry.content}
                  </p>
                  {(entry.actions?.length ?? 0) > 0 && (
                    <div className="el-list" style={{ flexDirection: "row", flexWrap: "wrap", marginTop: 8 }}>
                      {entry.actions?.map((action) => <span key={action} className="el-chip">{action}</span>)}
                    </div>
                  )}
                </div>
              ))}
              <div ref={chatEnd} />
            </div>
            <form
              style={{ display: "flex", gap: 10, marginTop: 14 }}
              onSubmit={(event) => {
                event.preventDefault();
                if (!message.trim()) return;
                const text = message.trim();
                setMessage("");
                ask(text);
              }}
            >
              <input
                className="el-input" value={message}
                onChange={(event) => setMessage(event.target.value)}
                placeholder="Ask the mentor…" aria-label="Message the mentor"
              />
              <button className="el-btn primary" type="submit" disabled={busy || !message.trim()}>
                {loading === "chat" ? <Loader2 size={15} className="el-spin" /> : <GraduationCap size={15} />}
                Send
              </button>
            </form>
          </div>
        )}
      </section>
                      </div>
                   </>
                )}
             </div>
          </div>
        </section>
      </div>
    </main>
  );
}

