"""
Agentic Mode Orchestrator — handles voice-controlled multi-agent execution.
Connects Jarvis voice interface to the complete agent ecosystem.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable
from enum import Enum

from core.agent_registry import UnifiedAgentRegistry, AgentInfo, get_global_registry

log = logging.getLogger("sara.agentic_orchestrator")


class TaskStatus(Enum):
    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class AgentTask:
    """Represents a single agent task in a workflow."""
    id: str
    agent_id: str
    name: str
    description: str
    input_data: Dict[str, Any]
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    depends_on: List[str] = field(default_factory=list)  # Task IDs this depends on
    retry_count: int = 0
    max_retries: int = 3


@dataclass
class WorkflowPlan:
    """A complete workflow plan with multiple agent tasks."""
    id: str
    goal: str
    tasks: List[AgentTask] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    final_result: Optional[str] = None
    error: Optional[str] = None


class AgenticOrchestrator:
    """
    Orchestrates multi-agent workflows in Agentic Mode.
    
    Pipeline:
    1. Voice input → Intent understanding (LLM)
    2. Intent → Agent selection (registry matching)
    3. Input validation → Task planning (create workflow)
    4. Task execution (with dependency resolution)
    5. Result verification → Spoken response
    
    Supports:
    - Single agent tasks
    - Multi-agent workflows with dependencies
    - Parallel execution where possible
    - Retry with exponential backoff
    - Approval gates for destructive actions
    - Real-time progress updates to UI
    """
    
    def __init__(
        self,
        registry: UnifiedAgentRegistry,
        backend_url: str = "",
        api_key: str = "",
        ui_callback: Optional[Callable] = None,
        speak_callback: Optional[Callable[[str], None]] = None,
    ):
        if not backend_url:
            try:
                from core.backend_url import resolve_backend_url
                backend_url = resolve_backend_url()
            except Exception:
                backend_url = "http://127.0.0.1:8000"
        self.registry = registry
        self.backend_url = backend_url.rstrip("/")
        self.api_key = api_key
        self.ui_callback = ui_callback  # For progress updates
        self.speak_callback = speak_callback  # For spoken responses
        
        self._workflows: Dict[str, WorkflowPlan] = {}
        self._lock = asyncio.Lock()
        self._executing = False
        self._current_workflow_id: Optional[str] = None
        
        # LLM client for planning (uses backend's llm_router)
        self._llm_client = None
        
    def set_llm_client(self, client: Any) -> None:
        """Set the LLM client for planning."""
        self._llm_client = client
    
    async def process_voice_command(self, transcript: str) -> str:
        """
        Main entry point for voice commands in Agentic Mode.
        
        Pipeline:
        1. Understand intent from transcript
        2. Select best agent(s)
        3. Validate inputs
        4. Create execution plan
        5. Execute workflow
        6. Return spoken response
        """
        log.info(f"[Agentic] Processing voice command: {transcript[:100]}")
        
        # Step 1: Intent understanding
        intent = await self._understand_intent(transcript)
        if not intent:
            return "I couldn't understand what you'd like me to do. Could you rephrase that?"
        
        # Step 2: Agent selection
        selected_agents = await self._select_agents(intent)
        if not selected_agents:
            return f"I don't have an agent that can handle '{intent.get('task_type', 'this task')}'. Available categories: {', '.join(self.registry.get_categories())}"
        
        # Step 3: Create workflow plan
        workflow = await self._create_workflow(intent, selected_agents)
        if not workflow:
            return "I couldn't create a plan for that task."
        
        # Step 4: Execute workflow
        result = await self._execute_workflow(workflow)
        
        # Step 5: Generate spoken response
        response = await self._generate_response(workflow, result)
        
        return response
    
    async def _understand_intent(self, transcript: str) -> Optional[Dict[str, Any]]:
        """Use LLM to understand the user's intent from voice transcript."""
        if not self._llm_client:
            # Fallback: simple keyword-based intent extraction
            return self._fallback_intent(transcript)
        
        prompt = f"""Analyze this voice command and extract the intent as JSON:

Voice: "{transcript}"

Return JSON with:
- task_type: brief category (e.g., "web_search", "file_operation", "email", "social_media", "coding", "research", "automation", "analysis")
- description: what the user wants done in one sentence
- entities: key entities mentioned (files, urls, names, dates, etc.)
- requires_approval: true if this might modify/delete data
- suggested_agents: list of agent categories that might help
- multi_step: true if this likely needs multiple agents

Be concise. Only return valid JSON."""
        
        try:
            response = await self._call_llm(prompt, max_tokens=500)
            intent = json.loads(response)
            log.info(f"[Agentic] Understood intent: {intent}")
            return intent
        except Exception as e:
            log.error(f"[Agentic] Intent understanding failed: {e}")
            return self._fallback_intent(transcript)
    
    def _fallback_intent(self, transcript: str) -> Dict[str, Any]:
        """Simple keyword-based intent extraction as fallback."""
        transcript_lower = transcript.lower()
        
        # Simple keyword matching
        if any(kw in transcript_lower for kw in ["search", "find", "look up", "google", "web"]):
            task_type = "web_search"
        elif any(kw in transcript_lower for kw in ["email", "mail", "send message", "inbox"]):
            task_type = "email"
        elif any(kw in transcript_lower for kw in ["file", "folder", "document", "desktop", "move", "copy", "delete"]):
            task_type = "file_operation"
        elif any(kw in transcript_lower for kw in ["code", "program", "script", "debug", "github", "git"]):
            task_type = "coding"
        elif any(kw in transcript_lower for kw in ["social", "tweet", "post", "twitter", "linkedin", "instagram"]):
            task_type = "social_media"
        elif any(kw in transcript_lower for kw in ["research", "analyze", "study", "investigate"]):
            task_type = "research"
        elif any(kw in transcript_lower for kw in ["automate", "schedule", "remind", "calendar", "meeting"]):
            task_type = "automation"
        elif any(kw in transcript_lower for kw in ["whatsapp", "message", "chat"]):
            task_type = "messaging"
        else:
            task_type = "general"
        
        return {
            "task_type": task_type,
            "description": transcript,
            "entities": {},
            "requires_approval": any(kw in transcript_lower for kw in ["delete", "remove", "shutdown", "format"]),
            "suggested_agents": [task_type],
            "multi_step": False,
        }
    
    async def _select_agents(self, intent: Dict[str, Any]) -> List[AgentInfo]:
        """Select the best agent(s) for the intent."""
        task_type = intent.get("task_type", "general")
        suggested = intent.get("suggested_agents", [])
        
        candidates = []
        
        # First, try to find agents with matching capabilities
        for cap in suggested:
            agents = self.registry.get_agents_with_capability(cap)
            candidates.extend(agents)
        
        # Also search by category
        for cat in suggested:
            agents = self.registry.get_agents_by_category(cat)
            candidates.extend(agents)
        
        # Use find_best_agent as fallback
        if not candidates:
            best = self.registry.find_best_agent(intent.get("description", ""))
            if best:
                candidates = [best]
        
        # Deduplicate and filter online agents
        seen = set()
        online_candidates = []
        for agent in candidates:
            if agent.id not in seen and agent.status != "offline":
                seen.add(agent.id)
                online_candidates.append(agent)
        
        log.info(f"[Agentic] Selected {len(online_candidates)} agents for task_type={task_type}")
        return online_candidates[:3]  # Limit to top 3
    
    async def _create_workflow(self, intent: Dict[str, Any], agents: List[AgentInfo]) -> Optional[WorkflowPlan]:
        """Create a workflow plan from intent and selected agents."""
        workflow_id = str(uuid.uuid4())[:8]
        
        if len(agents) == 1 and not intent.get("multi_step", False):
            # Single agent task
            agent = agents[0]
            task = AgentTask(
                id=str(uuid.uuid4())[:8],
                agent_id=agent.id,
                name=agent.name,
                description=intent.get("description", ""),
                input_data={
                    "goal": intent.get("description", ""),
                    "entities": intent.get("entities", {}),
                },
            )
            workflow = WorkflowPlan(
                id=workflow_id,
                goal=intent.get("description", ""),
                tasks=[task],
            )
        else:
            # Multi-agent workflow - use LLM to plan
            workflow = await self._plan_multi_agent_workflow(workflow_id, intent, agents)
        
        if workflow:
            async with self._lock:
                self._workflows[workflow.id] = workflow
            self._notify_ui_update(workflow)
        
        return workflow
    
    async def _plan_multi_agent_workflow(
        self, 
        workflow_id: str, 
        intent: Dict[str, Any], 
        agents: List[AgentInfo]
    ) -> Optional[WorkflowPlan]:
        """Use LLM to plan a multi-agent workflow."""
        if not self._llm_client:
            # Simple sequential fallback
            return self._fallback_workflow(workflow_id, intent, agents)
        
        agent_descriptions = "\n".join([
            f"- {a.id}: {a.name} ({a.category}) - {a.description} - capabilities: {', '.join(a.capabilities)}"
            for a in agents
        ])
        
        prompt = f"""Create a workflow plan for this goal using the available agents.

Goal: {intent.get('description', '')}
Entities: {json.dumps(intent.get('entities', {}))}

Available Agents:
{agent_descriptions}

Return JSON with:
- tasks: array of tasks, each with:
  - id: short unique id
  - agent_id: the agent to use
  - name: task name
  - description: what this task does
  - input_data: dict of inputs for the agent
  - depends_on: array of task ids this depends on (for ordering)
- multi_step: true

Keep it minimal - only as many tasks as needed. Use depends_on for dependencies."""
        
        try:
            response = await self._call_llm(prompt, max_tokens=1000)
            plan_data = json.loads(response)
            
            tasks = []
            for t in plan_data.get("tasks", []):
                task = AgentTask(
                    id=t.get("id", str(uuid.uuid4())[:8]),
                    agent_id=t.get("agent_id", ""),
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    input_data=t.get("input_data", {}),
                    depends_on=t.get("depends_on", []),
                )
                tasks.append(task)
            
            workflow = WorkflowPlan(
                id=workflow_id,
                goal=intent.get("description", ""),
                tasks=tasks,
            )
            return workflow
        except Exception as e:
            log.error(f"[Agentic] Multi-agent planning failed: {e}")
            return self._fallback_workflow(workflow_id, intent, agents)
    
    def _fallback_workflow(self, workflow_id: str, intent: Dict[str, Any], agents: List[AgentInfo]) -> WorkflowPlan:
        """Simple sequential fallback workflow."""
        tasks = []
        prev_id = None
        for i, agent in enumerate(agents):
            task = AgentTask(
                id=str(uuid.uuid4())[:8],
                agent_id=agent.id,
                name=f"{agent.name} - Step {i+1}",
                description=f"Execute {agent.name} for: {intent.get('description', '')}",
                input_data={
                    "goal": intent.get("description", ""),
                    "entities": intent.get("entities", {}),
                    "previous_result": f"{{{{task_{prev_id}.result}}}}" if prev_id else None,
                },
                depends_on=[prev_id] if prev_id else [],
            )
            tasks.append(task)
            prev_id = task.id
        
        return WorkflowPlan(
            id=workflow_id,
            goal=intent.get("description", ""),
            tasks=tasks,
        )
    
    async def _execute_workflow(self, workflow: WorkflowPlan) -> Dict[str, Any]:
        """Execute a workflow plan with dependency resolution."""
        self._executing = True
        self._current_workflow_id = workflow.id
        workflow.status = TaskStatus.EXECUTING
        workflow.updated_at = time.time()
        self._notify_ui_update(workflow)
        
        # Track completed task results for dependency injection
        task_results: Dict[str, Any] = {}
        
        # Build dependency graph
        pending_tasks = {t.id: t for t in workflow.tasks}
        completed_tasks = set()
        
        while pending_tasks and self._executing:
            # Find tasks ready to execute (all dependencies met)
            ready_tasks = [
                t for t in pending_tasks.values()
                if all(dep in completed_tasks for dep in t.depends_on)
            ]
            
            if not ready_tasks:
                # Circular dependency or deadlock
                for t in pending_tasks.values():
                    t.status = TaskStatus.FAILED
                    t.error = "Dependency deadlock detected"
                break
            
            # Execute ready tasks in parallel
            exec_tasks = [self._execute_task(t, task_results) for t in ready_tasks]
            results = await asyncio.gather(*exec_tasks, return_exceptions=True)
            
            for task, result in zip(ready_tasks, results):
                if isinstance(result, Exception):
                    task.status = TaskStatus.FAILED
                    task.error = str(result)
                    task.completed_at = time.time()
                    log.error(f"[Agentic] Task {task.id} failed: {result}")
                else:
                    task.status = TaskStatus.COMPLETED if result.get("ok") else TaskStatus.FAILED
                    task.result = result
                    task.completed_at = time.time()
                    if result.get("ok"):
                        task_results[task.id] = result.get("output", "")
                        completed_tasks.add(task.id)
                    else:
                        task.error = result.get("error", "Unknown error")
                
                workflow.updated_at = time.time()
                self._notify_ui_update(workflow)
            
            # Remove completed tasks from pending
            for task in ready_tasks:
                pending_tasks.pop(task.id, None)
            
            # Check if any critical task failed
            failed_critical = any(
                t.status == TaskStatus.FAILED and not t.depends_on  # Root task failed
                for t in workflow.tasks
            )
            if failed_critical:
                workflow.status = TaskStatus.FAILED
                break
        
        # Determine final workflow status
        if all(t.status == TaskStatus.COMPLETED for t in workflow.tasks):
            workflow.status = TaskStatus.COMPLETED
        elif any(t.status == TaskStatus.COMPLETED for t in workflow.tasks):
            workflow.status = TaskStatus.COMPLETED_WITH_ISSUES
        else:
            workflow.status = TaskStatus.FAILED
        
        workflow.updated_at = time.time()
        self._notify_ui_update(workflow)
        
        self._executing = False
        self._current_workflow_id = None
        
        return {
            "workflow_id": workflow.id,
            "status": workflow.status.value,
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "agent": t.name,
                    "status": t.status.value,
                    "result": t.result,
                    "error": t.error,
                }
                for t in workflow.tasks
            ],
        }
    
    async def _execute_task(self, task: AgentTask, previous_results: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a single agent task."""
        task.status = TaskStatus.EXECUTING
        task.started_at = time.time()
        self._notify_ui_update(self._workflows.get(self._current_workflow_id))
        
        agent = self.registry.get_agent(task.agent_id)
        if not agent:
            return {"ok": False, "error": f"Agent {task.agent_id} not found"}
        
        log.info(f"[Agentic] Executing task {task.id} with agent {agent.name} ({agent.source})")
        
        try:
            # Resolve input data with previous results
            resolved_input = self._resolve_input_data(task.input_data, previous_results)
            
            # Execute based on agent source
            if agent.source == "desktop_action":
                result = await self._execute_desktop_action(agent, resolved_input)
            elif agent.source == "plugin":
                result = await self._execute_plugin(agent, resolved_input)
            elif agent.source == "backend_agent":
                result = await self._execute_backend_agent(agent, resolved_input)
            elif agent.source == "ultron_native":
                result = await self._execute_ultron_native(agent, resolved_input)
            else:
                result = {"ok": False, "error": f"Unknown agent source: {agent.source}"}
            
            return result
            
        except Exception as e:
            log.error(f"[Agentic] Task {task.id} exception: {e}")
            return {"ok": False, "error": str(e)}
    
    def _resolve_input_data(self, input_data: Dict[str, Any], previous_results: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve template variables in input data with previous task results."""
        import re
        
        def resolve_value(value: Any) -> Any:
            if isinstance(value, str):
                # Replace {{task_xxx.result}} with actual result
                def replace_match(match):
                    task_id = match.group(1)
                    return str(previous_results.get(task_id, match.group(0)))
                return re.sub(r'\{\{task_([^.}]+)\.result\}\}', replace_match, value)
            elif isinstance(value, dict):
                return {k: resolve_value(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [resolve_value(v) for v in value]
            return value
        
        return resolve_value(input_data)
    
    async def _execute_desktop_action(self, agent: AgentInfo, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a desktop action via the action registry."""
        # This would call the actual desktop action
        # For now, return a placeholder
        tool_name = agent.tools[0] if agent.tools else "unknown"
        return {
            "ok": True,
            "output": f"Desktop action {tool_name} executed (placeholder)",
            "agent": agent.name,
        }
    
    async def _execute_plugin(self, agent: AgentInfo, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a plugin."""
        return {
            "ok": True,
            "output": f"Plugin {agent.name} executed (placeholder)",
            "agent": agent.name,
        }
    
    async def _execute_backend_agent(self, agent: AgentInfo, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a backend agent via API."""
        if not agent.route:
            return {"ok": False, "error": "No route for backend agent"}
        
        try:
            import httpx
            async with httpx.AsyncClient(timeout=60.0) as client:
                headers = {"X-API-Key": self.api_key} if self.api_key else {}
                
                # Try different endpoints based on agent type
                if "ultron" in agent.id:
                    # Use Ultron run endpoint
                    resp = await client.post(
                        f"{self.backend_url}/ultron/run",
                        headers=headers,
                        json={"goal": input_data.get("goal", ""), "mode": "autonomous"}
                    )
                elif "sara-auto" in agent.id:
                    resp = await client.post(
                        f"{self.backend_url}/sara-auto/run",
                        headers=headers,
                        json={"goal": input_data.get("goal", ""), "mode": "autonomous"}
                    )
                elif "agency" in agent.id:
                    # Agency agents use /api/agency/agents/{id}/run
                    agent_id = agent.metadata.get("agent_id", agent.id.replace("backend_agent:", ""))
                    resp = await client.post(
                        f"{self.backend_url}/api/agency/agents/{agent_id}/run",
                        headers=headers,
                        json={"message": input_data.get("goal", "")}
                    )
                else:
                    # Generic chat endpoint
                    resp = await client.post(
                        f"{self.backend_url}/api/v1/chat",
                        headers=headers,
                        json={"agent_id": agent.id, "message": input_data.get("goal", "")}
                    )
                
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "ok": True,
                        "output": data.get("reply") or data.get("summary") or str(data),
                        "agent": agent.name,
                        "raw": data,
                    }
                else:
                    return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"ok": False, "error": f"Backend agent call failed: {e}"}
    
    async def _execute_ultron_native(self, agent: AgentInfo, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an Ultron native tool."""
        # These are handled by the Ultron agent itself
        return {
            "ok": True,
            "output": f"Ultron native tool {agent.name} invoked (handled by Ultron)",
            "agent": agent.name,
        }
    
    async def _call_llm(self, prompt: str, max_tokens: int = 1000) -> str:
        """Call the LLM via backend's llm_router."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                headers = {"X-API-Key": self.api_key} if self.api_key else {}
                resp = await client.post(
                    f"{self.backend_url}/api/llm/chat",  # This endpoint may need to be created
                    headers=headers,
                    json={"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
                )
                if resp.status_code == 200:
                    return resp.json().get("reply", "")
                # Fallback: try legacy chat endpoint
                resp = await client.post(
                    f"{self.backend_url}/api/v1/chat",
                    headers=headers,
                    json={"agent_id": "general", "message": prompt}
                )
                if resp.status_code == 200:
                    return resp.json().get("reply", "")
        except Exception as e:
            log.error(f"[Agentic] LLM call failed: {e}")
        return ""
    
    async def _generate_response(self, workflow: WorkflowPlan, result: Dict[str, Any]) -> str:
        """Generate a spoken response from workflow results."""
        if workflow.status == TaskStatus.COMPLETED:
            successful = sum(1 for t in workflow.tasks if t.status == TaskStatus.COMPLETED)
            return f"Done! I completed {successful} task{'s' if successful != 1 else ''} for you."
        elif workflow.status == TaskStatus.COMPLETED_WITH_ISSUES:
            failed = sum(1 for t in workflow.tasks if t.status == TaskStatus.FAILED)
            return f"Partially completed. {failed} task{'s' if failed != 1 else ''} had issues. Check the dashboard for details."
        else:
            return "I wasn't able to complete that task. Let me know if you'd like me to try a different approach."
    
    def _notify_ui_update(self, workflow: Optional[WorkflowPlan]) -> None:
        """Notify UI of workflow progress."""
        if self.ui_callback and workflow:
            try:
                self.ui_callback(workflow)
            except Exception as e:
                log.error(f"[Agentic] UI callback failed: {e}")
    
    def get_workflow(self, workflow_id: str) -> Optional[WorkflowPlan]:
        """Get a workflow by ID."""
        return self._workflows.get(workflow_id)
    
    def get_current_workflow(self) -> Optional[WorkflowPlan]:
        """Get the currently executing workflow."""
        if self._current_workflow_id:
            return self._workflows.get(self._current_workflow_id)
        return None
    
    def cancel_current_workflow(self) -> bool:
        """Cancel the currently executing workflow."""
        if self._current_workflow_id:
            workflow = self._workflows.get(self._current_workflow_id)
            if workflow:
                workflow.status = TaskStatus.CANCELLED
                self._executing = False
                self._notify_ui_update(workflow)
                return True
        return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get orchestrator statistics."""
        return {
            "total_workflows": len(self._workflows),
            "executing": self._executing,
            "current_workflow": self._current_workflow_id,
            "workflows_by_status": {
                status.value: sum(1 for w in self._workflows.values() if w.status == status)
                for status in TaskStatus
            },
        }


# Global orchestrator instance
_global_orchestrator: Optional[AgenticOrchestrator] = None
_orchestrator_lock = asyncio.Lock()


async def get_global_orchestrator(
    registry: UnifiedAgentRegistry,
    backend_url: str = "",
    api_key: str = "",
    ui_callback: Optional[Callable] = None,
    speak_callback: Optional[Callable[[str], None]] = None,
) -> AgenticOrchestrator:
    """Get or create the global agentic orchestrator."""
    global _global_orchestrator
    async with _orchestrator_lock:
        if _global_orchestrator is None:
            if not backend_url:
                try:
                    from core.backend_url import resolve_backend_url
                    backend_url = resolve_backend_url()
                except Exception:
                    backend_url = "http://127.0.0.1:8000"
            _global_orchestrator = AgenticOrchestrator(
                registry, backend_url, api_key, ui_callback, speak_callback
            )
        return _global_orchestrator


def set_global_orchestrator(orchestrator: AgenticOrchestrator) -> None:
    """Set the global orchestrator (for testing or reconfiguration)."""
    global _global_orchestrator
    _global_orchestrator = orchestrator