import { AudioLines, BookOpen, ChartNoAxesCombined, Code2, Cpu, FlaskConical, Globe, HeartPulse, Image, Mail, MessageSquare, Music2, Network, Orbit, Search, Shapes, Terminal, Zap, type LucideIcon } from "lucide-react";
import type { AgentCategory } from "@/lib/agents.config";

const byId: Record<string, LucideIcon> = {
  "telegram-assistant": MessageSquare, "whatsapp-agent": MessageSquare,
  "social-media-agency": Globe, "voice-agent": AudioLines,
  "frontend-developer": Code2, "backend-architect": Terminal,
  "sara-auto": Zap, "email-automation": Mail, "music-studio": Music2,
  "game-forge": Shapes, "sara-ultron": Orbit,
};
const byCategory: Record<AgentCategory, LucideIcon> = {
  Engineering: Network, Business: Cpu, "Image Generation": Image, General: Shapes,
  Health: HeartPulse, Finance: ChartNoAxesCombined, Research: FlaskConical,
  Education: BookOpen, Marketing: Globe, Productivity: Zap, Coding: Code2, Other: Search,
};
export default function AgentIcon({ id, category, size = 21 }: { id: string; category: AgentCategory; size?: number }) {
  const Icon = byId[id] ?? byCategory[category];
  return <Icon size={size} strokeWidth={1.5} aria-hidden="true" />;
}
