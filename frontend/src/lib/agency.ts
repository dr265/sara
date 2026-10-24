// Unified agent source aggregator — merges static hub agents + 267 agency agents
// into one client-callable catalogue. The agency list is fetched live from
// /api/agency/* so the frontend never hardcodes 267 entries.

import { AGENT_REGISTRY, HubAgent, AgentCategory } from "./agents.config";
import { saraFetch } from "./api";

export type DivisionMeta = { id: string; label: string; color: string; icon: string; count: number };

export type AgencyAgent = {
  id: string;
  division: string;
  division_label: string;
  division_color: string;
  division_icon: string;
  name: string;
  description: string;
  color: string;
  emoji: string;
  vibe: string;
};

export type UnifiedAgent = HubAgent & { source: "hub" } | (AgencyAgent & { source: "agency"; route: string; category: AgentCategory; status: HubAgent["status"]; shortDescription: string; capabilities: string[]; icon: string });

function agencyToHub(a: AgencyAgent): UnifiedAgent {
  // Map division -> AgentCategory
  const catMap: Record<string, AgentCategory> = {
    academic: "Education", design: "Other", engineering: "Engineering", finance: "Finance",
    "game-development": "Engineering", gis: "Research", healthcare: "Health",
    marketing: "Marketing", "paid-media": "Marketing", product: "Other",
    "project-management": "Productivity", research: "Research", sales: "Business",
    security: "Engineering", "spatial-computing": "Engineering", specialized: "Other",
    support: "Business", testing: "Engineering",
  };
  return {
    ...a,
    source: "agency" as const,
    route: `/agency/${a.id}`,
    category: (catMap[a.division] ?? "Other") as AgentCategory,
    status: "online" as const,
    shortDescription: (a.description || a.vibe || "").slice(0, 160),
    capabilities: [a.division_label, a.name].filter(Boolean),
    icon: a.emoji || "🤖",
  };
}

let _cache: AgencyAgent[] | null = null;
let _cacheAt = 0;
const _TTL = 5 * 60 * 1000;

export async function fetchAgencyAgents(force = false): Promise<AgencyAgent[]> {
  if (!force && _cache && Date.now() - _cacheAt < _TTL) return _cache;
  const base = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";
  try {
    const r = await saraFetch(`${base}/api/agency/agents?limit=400`, { cache: "no-store" as RequestCache });
    if (!r.ok) throw new Error(String(r.status));
    const j = await r.json();
    _cache = (j.agents ?? []) as AgencyAgent[];
    _cacheAt = Date.now();
    return _cache;
  } catch {
    return [];
  }
}

export async function fetchUnifiedAgents(): Promise<UnifiedAgent[]> {
  const agency = await fetchAgencyAgents();
  const mapped = agency.map(agencyToHub);
  const hub: UnifiedAgent[] = AGENT_REGISTRY.map(h => ({ ...h, source: "hub" as const }));
  return [...hub, ...mapped];
}

export function searchUnified(query: string, unified: UnifiedAgent[]): UnifiedAgent[] {
  const q = query.toLowerCase().trim();
  if (!q) return unified;
  return unified.filter(a =>
    a.name.toLowerCase().includes(q) ||
    (a as any).shortDescription?.toLowerCase().includes(q) ||
    (a as any).description?.toLowerCase().includes(q) ||
    (a as any).capabilities?.some((c: string) => c.toLowerCase().includes(q)) ||
    (a as any).division?.toLowerCase().includes(q) ||
    a.id.toLowerCase().includes(q)
  );
}

export async function fetchDivisions(): Promise<DivisionMeta[]> {
  const base = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";
  try {
    const r = await saraFetch(`${base}/api/agency/divisions`, { cache: "no-store" as RequestCache });
    if (!r.ok) return [];
    const j = await r.json();
    return j.divisions ?? [];
  } catch { return []; }
}
