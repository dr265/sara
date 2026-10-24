// 1. Define strict categories as requested
export type AgentCategory = 
  | "Engineering" | "Business" | "Image Generation" | "General" 
  | "Health" | "Finance" | "Research" | "Education" 
  | "Marketing" | "Productivity" | "Coding" | "Other";

/**
 * Status values are VERIFIED against the running backend, not aspirational.
 * `online`   -> the agent's health endpoint answered HTTP 200 in the live audit
 *               (backend/_audit_agents.py). Safe to demo to a client.
 * `beta`     -> endpoint answers, but a capability is degraded or a third-party
 *               credential (SMTP, WhatsApp transport, social OAuth) is unset, so
 *               the full flow cannot complete yet.
 * `beta` (UI-only) -> no health route is exposed, so it is NOT verified; treat
 *               as unproven and do not lead a client demo with it.
 */
export interface HubAgent {
  id: string;
  name: string;
  category: AgentCategory;
  shortDescription: string;
  capabilities: string[];
  status: "online" | "beta" | "maintenance" | "offline";
  route: string;
  /** Emoji glyph shown on the hub directory card (AgentIcon overrides it in the modern card). */
  icon: string;
  /** Optional alternate entry point (e.g. the Ultron voice interface). */
  secondaryRoute?: string;
}

export const AGENT_REGISTRY: HubAgent[] = [
{
    id: "backend-architect",
    name: "Backend Architect",
    category: "Engineering",
    shortDescription: "Designs and scales backend infrastructure and API architecture.",
    capabilities: ["API Design", "Database Schema", "Cloud Deployments"],
    status: "online",
    icon: "⚙️",
    route: "/hub/backend-architect"
  },
  {
    id: "edulearner-ai",
    name: "EduLearner AI",
    category: "Education",
    shortDescription: "Evidence-based adaptive learning and skill-gap analysis.",
    capabilities: ["Skill Gap Analysis", "Adaptive Roadmap", "Interview Readiness"],
    status: "online",
    icon: "🎓",
    route: "/hub/edulearner-ai"
  },
  // VERIFIED LIVE but "smtp": false -> GMAIL_APP_PASSWORD is empty in backend/.env,
// so drafts and AI copy generation work while actual sending does not.
{
    id: "email-automation",
    name: "Email Automation",
    category: "Marketing",
    shortDescription: "Automates email campaigns and personalized outreach.",
    capabilities: ["Campaign Management", "A/B Testing", "Analytics"],
    status: "beta",
    icon: "📧",
    route: "/hub/email-automation"
  },
  {
    id: "frontend-developer",
    name: "Frontend Developer",
    category: "Engineering",
    shortDescription: "Builds responsive React/Next.js interfaces from design specs.",
    capabilities: ["UI/UX Implementation", "State Management", "Performance Optimization"],
    status: "online",
    icon: "🎨",
    route: "/hub/frontend-developer"
  },
  {
    id: "sara-auto",
    name: "SARA Universal",
    category: "General",
    shortDescription: "General purpose orchestrator for complex multi-step workflows.",
    capabilities: ["Task Routing", "Reasoning", "API Integration"],
    status: "online",
    icon: "🧠",
    route: "/hub/sara-auto"
  },
  {
    id: "sara-ultron",
    name: "SARA Ultron",
    category: "General",
    shortDescription: "Plans a goal into steps, executes them, re-plans around failures and remembers the outcome.",
    capabilities: ["Goal Decomposition", "Tool Execution", "Replanning", "Agent Delegation", "Long-term Memory", "Voice Control"],
    status: "online",
    icon: "🛰️",
    route: "/hub/sara-ultron",
    secondaryRoute: "/hub/sara-ultron/voice"
  },
  // VERIFIED LIVE but running with a credential gap:
// /whatsapp/health reports "configured": false -> the transport (Unipile DSN /
// Kapso / Zernio) is not wired, so the agent runs but cannot receive a message.
{
    id: "whatsapp-agent",
    name: "SARA WhatsApp Engine",
    category: "Business",
    shortDescription: "Core engine for WhatsApp conversational AI.",
    capabilities: ["Memory Management", "Document RAG", "Data Syncing", "Persona Engine"],
    status: "beta",
    icon: "💬",
    route: "/hub/whatsapp-agent"
  },
  {
    id: "social-media",
    name: "Social Media Manager",
    category: "Marketing",
    shortDescription: "Strategizes and schedules cross-platform social content (X/Twitter).",
    capabilities: ["Content Strategy", "Trend Analysis", "API Publishing"],
    status: "online",
    icon: "📱",
    route: "/hub/social-media"
  },
  {
    id: "trading-agent",
    name: "Trading Agent",
    category: "Finance",
    shortDescription: "Analyzes market signals and formulates trade logic.",
    capabilities: ["Technical Analysis", "Signal Generation", "Live Execution"],
    status: "online",
    icon: "📊",
    route: "/hub/trading-agent"
  },
{
    id: "telegram-assistant",
    name: "Telegram Assistant",
    category: "Business",
    shortDescription: "Automates community engagement on Telegram.",
    capabilities: ["Group Moderation", "FAQ Responses", "Broadcasts"],
    status: "online",
    icon: "✈️",
    route: "/hub/telegram-assistant"
  },
  {
    id: "voice-agent",
    name: "Voice Agent",
    category: "Other",
    shortDescription: "Voice-driven conversational AI interface.",
    capabilities: ["Speech-to-Text", "Vocal Synthesis", "Realtime Calls"],
    status: "beta",
    icon: "🎙️",
    route: "/hub/voice-agent"
  },
  {
    id: "election-campaign",
    name: "Dynamic Election Campaign Voice Agent",
    category: "Business",
    shortDescription: "Configurable campaign voice agent: editable candidate details, approved message, FAQs, hand-off and calling schedule, dialled through the existing Twilio pipeline.",
    capabilities: ["Editable Campaign Profile", "Multilingual Outreach", "Approved FAQ Answers", "Do-Not-Call Safeguards", "Live Provider Status"],
    status: "beta",
    icon: "🗳️",
    route: "/hub/voice-agent#campaign"
  },
  {
    id: "ai-web-scraper",
    name: "AI Web Scraper",
    category: "Research",
    shortDescription: "Automated extraction and synthesis of web data.",
    capabilities: ["Data Extraction", "DOM Parsing", "Data Structuring"],
    status: "online",
    icon: "🕸️",
    route: "/hub/ai-web-scraper"
  },
  {
    id: "game-forge",
    name: "Game Forge",
    category: "Engineering",
    shortDescription: "Generates game assets and logic.",
    capabilities: ["Level Design", "Sprite Gen", "Logic"],
    status: "online",
    icon: "🎮",
    route: "/hub/game-forge"
  },
  {
    id: "music-studio",
    name: "Music Studio",
    category: "Other",
    shortDescription: "AI generated music and audio.",
    capabilities: ["Beat Gen", "Vocal Synth", "Mixing"],
    status: "online",
    icon: "🎵",
    route: "/hub/music-studio"
  }
];

// Helper functions for the Frontend Components
export const getAgentsByCategory = (category: AgentCategory) => {
  return AGENT_REGISTRY.filter(agent => agent.category === category);
};

export const searchAgents = (query: string) => {
  const lowerQuery = query.toLowerCase();
  return AGENT_REGISTRY.filter(agent => 
    agent.name.toLowerCase().includes(lowerQuery) ||
    agent.shortDescription.toLowerCase().includes(lowerQuery) ||
    agent.capabilities.some(cap => cap.toLowerCase().includes(lowerQuery))
  );
};