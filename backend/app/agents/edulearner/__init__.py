"""EduLearner AI — SARA AI Nexus Hub agent.

An agentic adaptive-learning engine: evidence-based skill assessment, gap
analysis, personalised roadmaps, daily planning, marked practice, an auditable
adaptive loop, a conversational mentor, long-term learner memory and progress
reports.

Architecture follows the established SARA agent pattern (one self-contained
package mounted as a router on the unified backend, like email_automation /
music_studio / social_media) while the internals use the proven
BaseAgent / AgentContext / OrchestratorAgent design:

    HTTP /api/edulearner/*  ->  api.py (router, validation, auth)
        ->  orchestrator.OrchestratorAgent (registry + workflows)
            ->  agents/*  (deterministic agents over AgentContext)
            ->  repository.Repository (SQLite JSON store)
            ->  llm.LLMClient (shared SARA LLM brain, optional)
"""

from .orchestrator import OrchestratorAgent, get_orchestrator

__all__ = ["OrchestratorAgent", "get_orchestrator"]
