"""
Agentic Mode Tools — tools available to the LLM when in Agentic Mode.
These tools allow the LLM to discover agents, create workflows, and execute multi-agent tasks.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.agent_registry import UnifiedAgentRegistry, get_global_registry
from core.agentic_orchestrator import AgenticOrchestrator, get_global_orchestrator, WorkflowPlan, TaskStatus


# These tools are added to the LLM's tool set when in Agentic Mode
AGENTIC_TOOL_DECLARATIONS = [
    {
        "name": "agentic_list_agents",
        "description": (
            "List all available agents in the SARA ecosystem. Use this to understand what "
            "capabilities are available before planning a task. Returns agents grouped by category "
            "with their status, capabilities, and descriptions."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": "Optional category filter (e.g., 'Desktop Automation', 'Backend', 'Research', 'Engineering')"
                },
                "source": {
                    "type": "STRING",
                    "description": "Optional source filter: 'desktop_action', 'backend_agent', 'plugin', 'ultron_native'"
                },
                "status": {
                    "type": "STRING",
                    "description": "Optional status filter: 'online', 'degraded', 'offline', 'unknown'"
                },
                "capability": {
                    "type": "STRING",
                    "description": "Optional capability filter (e.g., 'execute', 'delegate', 'plan', 'chat')"
                },
            },
            "required": [],
        },
    },
    {
        "name": "agentic_get_agent_details",
        "description": (
            "Get detailed information about a specific agent including its tools, endpoints, "
            "health status, and metadata. Use this before delegating a task to an agent."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "agent_id": {
                    "type": "STRING",
                    "description": "The agent ID to get details for (e.g., 'desktop_action:web_search', 'backend_agent:sara-ultron')"
                },
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "agentic_create_workflow",
        "description": (
            "Create a multi-agent workflow plan for a complex goal. The orchestrator will analyze "
            "the goal, select appropriate agents, and create an execution plan with dependencies. "
            "Returns a workflow ID that can be executed with agentic_execute_workflow."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {
                    "type": "STRING",
                    "description": "The user's goal in natural language"
                },
                "preferred_agents": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Optional list of agent IDs to prefer for this workflow"
                },
                "require_approval": {
                    "type": "BOOLEAN",
                    "description": "Whether to require approval before executing destructive actions (default: true)",
                    "default": True
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "agentic_execute_workflow",
        "description": (
            "Execute a previously created workflow plan. This runs all tasks in the workflow, "
            "respecting dependencies, and provides real-time progress updates. Returns the final "
            "results from all agents."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "workflow_id": {
                    "type": "STRING",
                    "description": "The workflow ID returned by agentic_create_workflow"
                },
            },
            "required": ["workflow_id"],
        },
    },
    {
        "name": "agentic_get_workflow_status",
        "description": (
            "Get the current status of a workflow execution, including progress of individual tasks, "
            "which agent is currently running, and any errors."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "workflow_id": {
                    "type": "STRING",
                    "description": "The workflow ID to check"
                },
            },
            "required": ["workflow_id"],
        },
    },
    {
        "name": "agentic_cancel_workflow",
        "description": (
            "Cancel a currently running workflow. All pending tasks will be stopped, and completed "
            "task results will be preserved."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "workflow_id": {
                    "type": "STRING",
                    "description": "The workflow ID to cancel"
                },
            },
            "required": ["workflow_id"],
        },
    },
    {
        "name": "agentic_delegate_to_agent",
        "description": (
            "Delegate a specific task directly to a single agent without creating a full workflow. "
            "Use this for simple, single-agent tasks. Returns the agent's response."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "agent_id": {
                    "type": "STRING",
                    "description": "The agent ID to delegate to"
                },
                "task": {
                    "type": "STRING",
                    "description": "The task description in natural language"
                },
                "context": {
                    "type": "OBJECT",
                    "description": "Optional additional context/data for the agent",
                    "additionalProperties": True
                },
            },
            "required": ["agent_id", "task"],
        },
    },
    {
        "name": "agentic_find_best_agent",
        "description": (
            "Find the best agent for a given task description. Uses semantic matching against "
            "agent capabilities, categories, and descriptions. Returns the recommended agent with "
            "reasoning."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task_description": {
                    "type": "STRING",
                    "description": "Natural language description of what needs to be done"
                },
                "required_capabilities": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Optional list of required capabilities (e.g., ['execute', 'web_search'])"
                },
            },
            "required": ["task_description"],
        },
    },
    {
        "name": "agentic_check_agent_health",
        "description": (
            "Check the health status of all agents or a specific agent. Updates the registry with "
            "current health information from backend endpoints."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "agent_id": {
                    "type": "STRING",
                    "description": "Optional specific agent ID to check. If omitted, checks all agents with health endpoints."
                },
            },
            "required": [],
        },
    },
    {
        "name": "agentic_get_registry_stats",
        "description": (
            "Get statistics about the agent registry: total agents, breakdown by category, source, "
            "and status. Useful for understanding the overall ecosystem state."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
            "required": [],
},
        },
        {
            "name": "agentic_check_messages",
            "description": (
                "Check Gmail and WhatsApp for messages in a specific time window. "
                "Uses the actual connected Gmail integration (Email Automation Agent) "
                "and authorized WhatsApp integration (WhatsApp Agent) to retrieve "
                "real message data. Returns sender, timestamp, subject/preview by source. "
                "Reports partial failure honestly if a service is unavailable."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "minutes_back": {
                        "type": "INTEGER",
                        "description": "How many minutes back to check (default: 10)",
                        "default": 10
                    },
                    "sources": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "Sources to check: 'gmail', 'whatsapp' (default: both)",
                        "default": ["gmail", "whatsapp"]
                    }
                },
                "required": [],
            },
        },
    ]


async def execute_agentic_tool(
    name: str,
    args: Dict[str, Any],
    registry: Optional[UnifiedAgentRegistry] = None,
    orchestrator: Optional[AgenticOrchestrator] = None,
) -> str:
    """Execute an agentic mode tool and return the result as a string."""
    
    if registry is None:
        registry = get_global_registry()
    
    if orchestrator is None:
        orchestrator = await get_global_orchestrator(registry)
    
    try:
        if name == "agentic_list_agents":
            return await _list_agents(registry, args)
        elif name == "agentic_get_agent_details":
            return await _get_agent_details(registry, args)
        elif name == "agentic_create_workflow":
            return await _create_workflow(orchestrator, args)
        elif name == "agentic_execute_workflow":
            return await _execute_workflow(orchestrator, args)
        elif name == "agentic_get_workflow_status":
            return await _get_workflow_status(orchestrator, args)
        elif name == "agentic_cancel_workflow":
            return await _cancel_workflow(orchestrator, args)
        elif name == "agentic_delegate_to_agent":
            return await _delegate_to_agent(registry, orchestrator, args)
        elif name == "agentic_find_best_agent":
            return await _find_best_agent(registry, args)
        elif name == "agentic_check_agent_health":
            return await _check_agent_health(registry, args)
        elif name == "agentic_get_registry_stats":
            return await _get_registry_stats(registry, args)
        elif name == "agentic_check_messages":
            return await _check_messages(registry, orchestrator, args)
        else:
            return f"Unknown agentic tool: {name}"
    except Exception as e:
        return f"Agentic tool '{name}' failed: {e}"


async def _list_agents(registry: UnifiedAgentRegistry, args: Dict[str, Any]) -> str:
    """List agents with optional filters."""
    category = args.get("category")
    source = args.get("source")
    status = args.get("status")
    capability = args.get("capability")
    
    agents = registry.get_all_agents()
    
    if category:
        agents = [a for a in agents if a.category.lower() == category.lower()]
    if source:
        agents = [a for a in agents if a.source == source]
    if status:
        agents = [a for a in agents if a.status == status]
    if capability:
        agents = [a for a in agents if capability in a.capabilities]
    
    # Group by category
    by_category = {}
    for agent in agents:
        if agent.category not in by_category:
            by_category[agent.category] = []
        by_category[agent.category].append({
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "capabilities": agent.capabilities,
            "source": agent.source,
            "status": agent.status,
            "tools": agent.tools,
            "route": agent.route,
        })
    
    result = {
        "total": len(agents),
        "categories": by_category,
        "filters_applied": {
            "category": category,
            "source": source,
            "status": status,
            "capability": capability,
        }
    }
    
    return json.dumps(result, indent=2)


async def _get_agent_details(registry: UnifiedAgentRegistry, args: Dict[str, Any]) -> str:
    """Get detailed information about a specific agent."""
    agent_id = args.get("agent_id")
    agent = registry.get_agent(agent_id)
    
    if not agent:
        return f"Agent not found: {agent_id}"
    
    return json.dumps({
        "id": agent.id,
        "name": agent.name,
        "category": agent.category,
        "description": agent.description,
        "capabilities": agent.capabilities,
        "source": agent.source,
        "route": agent.route,
        "status": agent.status,
        "health_endpoint": agent.health_endpoint,
        "tools": agent.tools,
        "metadata": agent.metadata,
        "last_health_check": agent.last_health_check,
        "health_error": agent.health_error,
    }, indent=2)


async def _create_workflow(orchestrator: AgenticOrchestrator, args: Dict[str, Any]) -> str:
    """Create a workflow plan from a goal."""
    goal = args.get("goal", "")
    preferred_agents = args.get("preferred_agents", [])
    require_approval = args.get("require_approval", True)
    
    # This will use the orchestrator's intent understanding and planning
    # We need to simulate the intent understanding step
    from core.agentic_orchestrator import AgenticOrchestrator
    
    # Create a mock intent from the goal
    intent = {
        "task_type": "general",
        "description": goal,
        "entities": {},
        "requires_approval": require_approval,
        "suggested_agents": preferred_agents if preferred_agents else [],
        "multi_step": len(preferred_agents) > 1 or "workflow" in goal.lower() or "multi" in goal.lower(),
    }
    
    # Select agents
    if preferred_agents:
        agents = []
        for pa in preferred_agents:
            agent = orchestrator.registry.get_agent(pa)
            if agent:
                agents.append(agent)
    else:
        agents = await orchestrator._select_agents(intent)
    
    if not agents:
        return json.dumps({"error": "No suitable agents found for this task"})
    
    # Create workflow
    workflow = await orchestrator._create_workflow(intent, agents)
    
    if not workflow:
        return json.dumps({"error": "Failed to create workflow"})
    
    return json.dumps({
        "workflow_id": workflow.id,
        "goal": workflow.goal,
        "task_count": len(workflow.tasks),
        "tasks": [
            {
                "id": t.id,
                "name": t.name,
                "agent_id": t.agent_id,
                "description": t.description,
                "depends_on": t.depends_on,
            }
            for t in workflow.tasks
        ],
        "status": workflow.status.value,
    }, indent=2)


async def _execute_workflow(orchestrator: AgenticOrchestrator, args: Dict[str, Any]) -> str:
    """Execute a workflow."""
    workflow_id = args.get("workflow_id")
    workflow = orchestrator.get_workflow(workflow_id)
    
    if not workflow:
        return json.dumps({"error": f"Workflow not found: {workflow_id}"})
    
    if workflow.status in (TaskStatus.EXECUTING, TaskStatus.PLANNING):
        return json.dumps({"error": f"Workflow already {workflow.status.value}"})
    
    # Execute in background
    import asyncio
    asyncio.create_task(orchestrator._execute_workflow(workflow))
    
    return json.dumps({
        "workflow_id": workflow_id,
        "status": "started",
        "message": "Workflow execution started. Check status with agentic_get_workflow_status."
    }, indent=2)


async def _get_workflow_status(orchestrator: AgenticOrchestrator, args: Dict[str, Any]) -> str:
    """Get workflow execution status."""
    workflow_id = args.get("workflow_id")
    workflow = orchestrator.get_workflow(workflow_id)
    
    if not workflow:
        return json.dumps({"error": f"Workflow not found: {workflow_id}"})
    
    return json.dumps({
        "workflow_id": workflow.id,
        "goal": workflow.goal,
        "status": workflow.status.value,
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
        "tasks": [
            {
                "id": t.id,
                "name": t.name,
                "agent_id": t.agent_id,
                "status": t.status.value,
                "started_at": t.started_at,
                "completed_at": t.completed_at,
                "error": t.error,
                "has_result": t.result is not None,
            }
            for t in workflow.tasks
        ],
        "final_result": workflow.final_result,
        "error": workflow.error,
    }, indent=2)


async def _cancel_workflow(orchestrator: AgenticOrchestrator, args: Dict[str, Any]) -> str:
    """Cancel a workflow."""
    workflow_id = args.get("workflow_id")
    success = orchestrator.cancel_current_workflow()
    
    if success:
        return json.dumps({
            "workflow_id": workflow_id,
            "status": "cancelled",
            "message": "Workflow cancelled successfully"
        }, indent=2)
    else:
        return json.dumps({"error": f"No running workflow with ID: {workflow_id}"}, indent=2)


async def _delegate_to_agent(
    registry: UnifiedAgentRegistry, 
    orchestrator: AgenticOrchestrator, 
    args: Dict[str, Any]
) -> str:
    """Delegate a task directly to a single agent."""
    agent_id = args.get("agent_id")
    task = args.get("task", "")
    context = args.get("context", {})
    
    agent = registry.get_agent(agent_id)
    if not agent:
        return json.dumps({"error": f"Agent not found: {agent_id}"})
    
    # Create a simple workflow with one task
    from core.agentic_orchestrator import AgentTask, WorkflowPlan, TaskStatus
    import uuid
    
    task_obj = AgentTask(
        id=str(uuid.uuid4())[:8],
        agent_id=agent.id,
        name=agent.name,
        description=task,
        input_data={"goal": task, **context},
    )
    
    workflow = WorkflowPlan(
        id=str(uuid.uuid4())[:8],
        goal=task,
        tasks=[task_obj],
    )
    
    orchestrator._workflows[workflow.id] = workflow
    result = await orchestrator._execute_workflow(workflow)
    
    # Extract the single task result
    task_result = result["tasks"][0] if result.get("tasks") else {}
    
    return json.dumps({
        "agent_id": agent_id,
        "agent_name": agent.name,
        "task": task,
        "status": task_result.get("status", "unknown"),
        "result": task_result.get("result"),
        "error": task_result.get("error"),
    }, indent=2)


async def _find_best_agent(registry: UnifiedAgentRegistry, args: Dict[str, Any]) -> str:
    """Find the best agent for a task."""
    task_description = args.get("task_description", "")
    required_capabilities = args.get("required_capabilities", [])
    
    agent = registry.find_best_agent(task_description, required_capabilities)
    
    if not agent:
        return json.dumps({
            "found": False,
            "message": "No suitable agent found",
            "available_categories": registry.get_categories(),
        }, indent=2)
    
    return json.dumps({
        "found": True,
        "agent": {
            "id": agent.id,
            "name": agent.name,
            "category": agent.category,
            "description": agent.description,
            "capabilities": agent.capabilities,
            "source": agent.source,
            "status": agent.status,
            "tools": agent.tools,
        },
        "reasoning": f"Best match for '{task_description}' based on capabilities and description",
    }, indent=2)


async def _check_agent_health(registry: UnifiedAgentRegistry, args: Dict[str, Any]) -> str:
    """Check agent health."""
    agent_id = args.get("agent_id")
    
    if agent_id:
        agent = registry.get_agent(agent_id)
        if not agent:
            return json.dumps({"error": f"Agent not found: {agent_id}"})
        
        if agent.health_endpoint:
            healthy = await registry.check_agent_health(agent)
            return json.dumps({
                "agent_id": agent_id,
                "name": agent.name,
                "healthy": healthy,
                "status": agent.status,
                "last_check": agent.last_health_check,
                "error": agent.health_error,
            }, indent=2)
        else:
            return json.dumps({
                "agent_id": agent_id,
                "name": agent.name,
                "healthy": agent.status != "offline",
                "status": agent.status,
                "note": "No health endpoint - status based on registry",
            }, indent=2)
    else:
        # Check all agents with health endpoints
        results = await registry.check_all_health()
        return json.dumps({
            "checked": len(results),
            "results": results,
            "summary": {
                "healthy": sum(1 for v in results.values() if v),
                "unhealthy": sum(1 for v in results.values() if not v),
            }
        }, indent=2)


async def _get_registry_stats(registry: UnifiedAgentRegistry, args: Dict[str, Any]) -> str:
    """Get registry statistics."""
    stats = registry.get_stats()
    return json.dumps(stats, indent=2)


async def _check_messages(registry: UnifiedAgentRegistry, orchestrator: AgenticOrchestrator, args: Dict[str, Any]) -> str:
    """Check Gmail and WhatsApp for messages in a time window.
    
    Uses the actual connected Gmail integration (Email Automation Agent)
    and authorized WhatsApp integration (WhatsApp Agent).
    Reports partial failure honestly if a service is unavailable.
    """
    minutes_back = args.get("minutes_back", 10)
    sources = args.get("sources", ["gmail", "whatsapp"])
    
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes_back)
    results = {"window_minutes": minutes_back, "sources": {}, "errors": []}
    
    # Check Gmail via Email Automation Agent
    if "gmail" in sources:
        gmail_result = await _check_gmail_messages(orchestrator, cutoff)
        results["sources"]["gmail"] = gmail_result
        if "error" in gmail_result:
            results["errors"].append(f"Gmail: {gmail_result['error']}")
    
    # Check WhatsApp via WhatsApp Agent
    if "whatsapp" in sources:
        whatsapp_result = await _check_whatsapp_messages(orchestrator, cutoff)
        results["sources"]["whatsapp"] = whatsapp_result
        if "error" in whatsapp_result:
            results["errors"].append(f"WhatsApp: {whatsapp_result['error']}")
    
    # Summary
    total_messages = sum(
        len(src.get("messages", [])) for src in results["sources"].values()
    )
    results["summary"] = {
        "total_messages": total_messages,
        "partial_failure": len(results["errors"]) > 0,
    }
    
    return json.dumps(results, indent=2, default=str)


async def _check_gmail_messages(orchestrator: AgenticOrchestrator, cutoff: datetime) -> Dict[str, Any]:
    """Check Gmail via the Email Automation Agent."""
    try:
        # Find the Email Automation Agent
        agent = orchestrator.registry.find_best_agent(
            "check Gmail messages and retrieve recent emails",
            required_capabilities=["email", "gmail", "read"]
        )
        
        if not agent:
            return {"error": "Email Automation Agent not available", "messages": []}
        
        if agent.source == "backend_agent":
            # Use the backend API to check Gmail
            import requests
            try:
                from core.config_manager import get_api_key
                api_key = get_api_key()
                resp = requests.get(
                    f"{orchestrator.registry._backend_url}/api/email/messages",
                    headers={"X-API-Key": api_key},
                    params={"since": cutoff.isoformat()},
                    timeout=10
                )
                if resp.ok:
                    data = resp.json()
                    return {"messages": data.get("messages", []), "source": "backend_email_agent"}
                else:
                    return {"error": f"Email API returned {resp.status_code}", "messages": []}
            except Exception as e:
                return {"error": f"Email API request failed: {e}", "messages": []}
        else:
            # Delegate to the agent via the orchestrator
            task = f"Check Gmail for messages since {cutoff.isoformat()} and return sender, timestamp, subject, and preview"
            workflow_id = await _create_delegation_workflow(orchestrator, agent.id, task)
            if workflow_id:
                return {"workflow_id": workflow_id, "status": "delegated", "messages": []}
            else:
                return {"error": "Failed to delegate to Email Agent", "messages": []}
                
    except Exception as e:
        return {"error": f"Gmail check failed: {e}", "messages": []}


async def _check_whatsapp_messages(orchestrator: AgenticOrchestrator, cutoff: datetime) -> Dict[str, Any]:
    """Check WhatsApp via the WhatsApp Agent."""
    try:
        # Find the WhatsApp Agent
        agent = orchestrator.registry.find_best_agent(
            "check WhatsApp messages and retrieve recent chats",
            required_capabilities=["whatsapp", "messaging", "read"]
        )
        
        if not agent:
            return {"error": "WhatsApp Agent not available", "messages": []}
        
        if agent.source == "backend_agent":
            # Use the backend API to check WhatsApp
            import requests
            try:
                from core.config_manager import get_api_key
                api_key = get_api_key()
                resp = requests.get(
                    f"{orchestrator.registry._backend_url}/api/whatsapp/messages",
                    headers={"X-API-Key": api_key},
                    params={"since": cutoff.isoformat()},
                    timeout=10
                )
                if resp.ok:
                    data = resp.json()
                    return {"messages": data.get("messages", []), "source": "backend_whatsapp_agent"}
                else:
                    return {"error": f"WhatsApp API returned {resp.status_code}", "messages": []}
            except Exception as e:
                return {"error": f"WhatsApp API request failed: {e}", "messages": []}
        else:
            # Delegate to the agent via the orchestrator
            task = f"Check WhatsApp for messages since {cutoff.isoformat()} and return sender, timestamp, and preview"
            workflow_id = await _create_delegation_workflow(orchestrator, agent.id, task)
            if workflow_id:
                return {"workflow_id": workflow_id, "status": "delegated", "messages": []}
            else:
                return {"error": "Failed to delegate to WhatsApp Agent", "messages": []}
                
    except Exception as e:
        return {"error": f"WhatsApp check failed: {e}", "messages": []}


async def _create_delegation_workflow(orchestrator: AgenticOrchestrator, agent_id: str, task: str) -> Optional[str]:
    """Create a simple workflow to delegate a task to a specific agent."""
    try:
        from core.agentic_orchestrator import AgentTask, WorkflowPlan, TaskStatus
        import uuid
        
        task_obj = AgentTask(
            id=str(uuid.uuid4())[:8],
            agent_id=agent_id,
            name=orchestrator.registry.get_agent(agent_id).name,
            description=task,
            input_data={"goal": task},
        )
        
        workflow = WorkflowPlan(
            id=str(uuid.uuid4())[:8],
            goal=task,
            tasks=[task_obj],
        )
        
        orchestrator._workflows[workflow.id] = workflow
        return workflow.id
    except Exception:
        return None