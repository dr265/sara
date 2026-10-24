import { AGENT_REGISTRY } from "./agents.config";

export interface Destination {
  href: string;
  label: string;
  group: "Workspace" | "Agents" | "Operations";
  description: string;
}

// Only existing routes belong in the command index. Agent availability is
// intentionally not inferred from this static navigation catalog.
export const DESTINATIONS: Destination[] = [
  { href: "/workspace", label: "Document workspace", group: "Workspace", description: "Upload documents and ask questions" },
  { href: "/hub", label: "Nexus Hub", group: "Workspace", description: "Discover agents and route a task" },
  { href: "/hub/sara-bots", label: "Sara Bots", group: "Workspace", description: "Real-agent workspace: run and monitor live bots" },
  { href: "/workspace/desktop-agent", label: "Desktop Agent", group: "Workspace", description: "Desktop tools and live execution" },
  { href: "/workspace/voice-agent", label: "Voice interface", group: "Workspace", description: "Talk with SARA" },
  { href: "/workspace/agent-creator", label: "Voice agent configuration", group: "Workspace", description: "Configure your voice assistant" },
  { href: "/workspace/sara-orb", label: "Spatial interface", group: "Workspace", description: "Interactive voice orb" },
  ...AGENT_REGISTRY.map(agent => ({ href: agent.route, label: agent.name, group: "Agents" as const, description: agent.shortDescription })),
  ...[
    ["telegram-assistant", "Telegram Assistant", "Bookings, knowledge and Telegram connections"],
    ["whatsapp-agent", "WhatsApp Agent", "Guest conversations and business knowledge"],
    ["social-media", "Social Media Agency", "Connected accounts, publishing and schedules"],
    ["voice-agent", "Voice Agent", "Calls, transcripts and voice configuration"],
    ["frontend-developer", "Frontend Developer", "Code and frontend prototyping"],
    ["backend-architect", "Backend Architect", "Repository inspection and approved execution"],
    ["sara-auto", "SARA Auto", "Autonomous tasks with approval gates"],
    ["email-automation", "Email Automation", "Campaigns, personalization and dry runs"],
  ].map(([slug, label, description]) => ({ href: `/hub/${slug}`, label, description, group: "Agents" as const })),
  ...[
    ["", "Operations overview", "Business activity and metrics"],
    ["/conversations", "Conversations", "Customer messages and conversation history"],
    ["/bookings", "Leads & bookings", "Manage guest requests"],
    ["/knowledge", "Knowledge", "Business knowledge and files"],
    ["/prompts", "Prompts & rules", "Configure assistant instructions"],
    ["/sheets", "Google Sheets", "Connect and synchronize spreadsheets"],
    ["/telegram", "Telegram connection", "Configure the Telegram integration"],
    ["/documents", "Generated documents", "Review document outputs"],
    ["/voice", "Operations voice", "Voice conversation tools"],
    ["/logs", "Activity logs", "Inspect recorded activity"],
    ["/settings", "Settings", "Business and assistant settings"],
    ["/help", "Help", "Platform guidance"],
  ].map(([slug, label, description]) => ({ href: `/sara-platform${slug}`, label, description, group: "Operations" as const })),
];
