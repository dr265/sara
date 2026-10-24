import json
import os
import time
from pathlib import Path
from typing import List, Dict

class PersistentMemory:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        # Creates a dedicated memory folder for each agent
        self.memory_dir = Path(os.environ.get("FULLAI_HOME", Path.home() / "FullAI_Workspace")) / "memory" / agent_id
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        
        self.experience_file = self.memory_dir / "experiences.json"
        self.skills_file = self.memory_dir / "learned_skills.json"
        
        self._ensure_files()

    def _ensure_files(self):
        if not self.experience_file.exists():
            self.experience_file.write_text(json.dumps([]))
        if not self.skills_file.exists():
            self.skills_file.write_text(json.dumps([]))

    def save_experience(self, goal: str, plan_executed: List[dict], success: bool, error_logs: str = ""):
        """Saves the outcome of a task so the agent remembers what worked and what failed."""
        experiences = json.loads(self.experience_file.read_text(encoding="utf-8-sig"))
        if not isinstance(experiences, list):
            experiences = []  # corrupt/legacy shape: reset rather than crash
        
        # Keep only the last 50 experiences to prevent prompt bloating
        if len(experiences) >= 50:
            experiences.pop(0)
            
        experiences.append({
            "timestamp": time.time(),
            "goal": goal,
            "plan": plan_executed,
            "success": success,
            "errors": error_logs
        })
        self.experience_file.write_text(json.dumps(experiences, indent=2))

    def add_learned_skill(self, rule: str):
        """Adds a permanent rule to the agent's behavior based on past mistakes."""
        skills = json.loads(self.skills_file.read_text(encoding="utf-8-sig"))
        if isinstance(skills, str):
            # Legacy bare-string store (was written as a JSON string once).
            # Normalize to a list so the content survives the next write.
            skills = [skills]
        elif not isinstance(skills, list):
            skills = []
        if rule not in skills:
            skills.append(rule)
            self.skills_file.write_text(json.dumps(skills, indent=2), encoding="utf-8")

    def get_context_for_prompt(self, current_goal: str) -> str:
        """Retrieves relevant past learnings to inject into the LLM system prompt."""
        skills = json.loads(self.skills_file.read_text())
        experiences = json.loads(self.experience_file.read_text())
        
        # Simple keyword matching (Can be upgraded to Vector/ChromaDB later for 200+ agents)
        relevant_exps = [exp for exp in experiences if any(word in exp["goal"].lower() for word in current_goal.lower().split())]
        
        context = "PAST LEARNINGS & RULES:\n"
        if skills:
            context += "\n".join([f"- {skill}" for skill in skills]) + "\n\n"
            
        if relevant_exps:
            context += "SIMILAR PAST EXPERIENCES:\n"
            for exp in relevant_exps[-3:]: # Inject top 3 recent similar tasks
                status = "SUCCESS" if exp["success"] else f"FAILED ({exp['errors']})"
                context += f"- Goal: {exp['goal']} | Result: {status}\n"
                
        return context if (skills or relevant_exps) else "No past memory for this specific task."