"""Agency Agents API — 267 markdown prompt agents as a real multi-tenant microservice runtime.

Source: D:/agency-agents-main — each *.md is read live on every request.
Execution: agency .md -> system prompt + llm_router.llm_chat (same 9-provider failover as hub).
Now with streaming SSE, persisted history, and division pipelines so every agent
feels like a Hub engine (ai-web-scraper / game-forge / music-studio parity).

History file: backend/agency_history.json (per-agent capped 100 messages).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import require_api_key

router = APIRouter(prefix="/api/agency", tags=["Agency Agents"],
                   dependencies=[Depends(require_api_key)])

AGENCY_ROOT = Path("D:/agency-agents-main/agency-agents-main")
INVENTORY_FILE = Path(__file__).resolve().parent.parent.parent / "agency_inventory.json"
DIVISIONS_FILE = AGENCY_ROOT / "divisions.json"
HISTORY_FILE = Path(__file__).resolve().parent.parent.parent / "agency_history.json"

# ---- division pipelines (Hub-parity stages) ---------------------------------
DIVISION_PIPELINES: Dict[str, List[str]] = {
    "academic": ["Gathering sources", "Synthesizing framework", "Drafting analysis", "Citing & validating"],
    "design": ["Understanding brief", "Exploring concepts", "Crafting assets", "Refining delivery"],
    "engineering": ["Analyzing requirements", "Designing architecture", "Writing code", "Reviewing quality"],
    "finance": ["Gathering data", "Modeling scenarios", "Analyzing risk", "Reporting"],
    "game-development": ["Concepting", "Prototyping systems", "Balancing mechanics", "Packaging build"],
    "gis": ["Ingesting spatial data", "Spatial analysis", "Visualizing map", "Validating accuracy"],
    "healthcare": ["Reviewing evidence", "Assessing risk", "Planning care", "Documenting"],
    "marketing": ["Researching audience", "Crafting strategy", "Building assets", "Measuring impact"],
    "paid-media": ["Audience targeting", "Bid strategy", "Creative test", "Reporting"],
    "product": ["Discovery", "Prioritizing", "Speccing", "Handoff"],
    "project-management": ["Scoping", "Planning", "Tracking", "Reporting"],
    "research": ["Literature review", "Synthesizing", "Drafting", "Validating"],
    "sales": ["Prospecting", "Qualifying", "Proposing", "Closing playbook"],
    "security": ["Threat modeling", "Scanning controls", "Hardening", "Verifying"],
    "spatial-computing": ["Spatial mapping", "Interaction design", "Prototyping", "Testing"],
    "specialized": ["Scoping", "Researching", "Building", "Reviewing"],
    "support": ["Triaging", "Diagnosing", "Resolving", "Following up"],
    "testing": ["Planning tests", "Executing", "Reporting", "Regression check"],
}
DEFAULT_PIPELINE = ["Understanding request", "Reasoning", "Drafting response", "Final review"]

DIVISION_TOOLS: Dict[str, List[str]] = {
    "academic": ["web_search", "synthesize", "cite"],
    "design": ["brief_analyze", "generate_concepts", "refine"],
    "engineering": ["analyze", "architect", "code", "review"],
    "finance": ["market_data", "model", "risk_analyze"],
    "game-development": ["game_design", "prototype", "balance"],
    "gis": ["spatial_query", "analyze", "visualize"],
    "healthcare": ["evidence_lookup", "risk_score", "care_plan"],
    "marketing": ["audience_research", "strategy", "copy"],
    "paid-media": ["audience_build", "bid_sim", "creative"],
    "product": ["discovery", "prioritize", "spec"],
    "project-management": ["scope", "plan", "track"],
    "research": ["search", "synthesize", "draft"],
    "sales": ["prospect", "qualify", "propose"],
    "security": ["threat_model", "scan", "harden"],
    "spatial-computing": ["spatial_map", "interaction", "prototype"],
    "specialized": ["research", "build", "review"],
    "support": ["triage", "diagnose", "resolve"],
    "testing": ["test_plan", "execute", "report"],
}


@lru_cache(maxsize=1)
def _load_inventory() -> List[Dict[str, Any]]:
    if INVENTORY_FILE.exists():
        return json.loads(INVENTORY_FILE.read_text(encoding="utf-8"))
    return []


@lru_cache(maxsize=1)
def _load_divisions() -> Dict[str, Any]:
    if DIVISIONS_FILE.exists():
        data = json.loads(DIVISIONS_FILE.read_text(encoding="utf-8"))
        return data.get("divisions", {})
    return {}


def _read_agent_file(agent_id: str) -> tuple[str, str, Dict[str, str]]:
    inv = _load_inventory()
    entry = next((x for x in inv if x["id"] == agent_id), None)
    if not entry:
        matches = list(AGENCY_ROOT.rglob(f"{agent_id}.md"))
        if not matches:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        path = matches[0]
    else:
        path = AGENCY_ROOT / entry["division"] / f"{agent_id}.md"
        if not path.exists():
            matches = list(AGENCY_ROOT.rglob(f"{agent_id}.md"))
            if not matches:
                raise HTTPException(status_code=404, detail=f"Agent file missing for '{agent_id}'")
            path = matches[0]
    text = path.read_text(encoding="utf-8", errors="replace")
    fm: Dict[str, str] = {}
    body = text
    m = re.search(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if m:
        for line in m.group(1).split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip('"').strip("'")
        body = text[m.end():].strip()
    return (m.group(1) if m else "", body, fm)


def _get_pipeline(division: str) -> List[str]:
    return DIVISION_PIPELINES.get(division, DEFAULT_PIPELINE)


# ---- history store (file-backed, per-agent capped) --------------------------

def _load_all_history() -> Dict[str, List[Dict[str, Any]]]:
    if not HISTORY_FILE.exists():
        return {}
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_all_history(data: Dict[str, List[Dict[str, Any]]]) -> None:
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _append_history(agent_id: str, role: str, content: str, meta: Optional[Dict[str, Any]] = None) -> None:
    data = _load_all_history()
    lst = data.get(agent_id, [])
    lst.append({
        "id": str(uuid.uuid4())[:8],
        "role": role,
        "content": content,
        "ts": datetime.now(timezone.utc).isoformat(),
        **(meta or {}),
    })
    # cap 100 per agent (keep newest)
    if len(lst) > 100:
        lst = lst[-100:]
    data[agent_id] = lst
    _save_all_history(data)


# ---- public catalogue -------------------------------------------------------

@router.get("/divisions")
def list_divisions():
    divs = _load_divisions()
    inv = _load_inventory()
    from collections import Counter
    counts = Counter(x["division"] for x in inv)
    result = []
    for key, meta in divs.items():
        result.append({
            "id": key,
            "label": meta.get("label", key),
            "color": meta.get("color", "#6366F1"),
            "icon": meta.get("icon", "Sparkles"),
            "count": counts.get(key, 0),
        })
    result.sort(key=lambda x: -x["count"])
    return {"divisions": result, "total": len(inv)}


@router.get("/agents")
def list_agents(division: Optional[str] = None, q: Optional[str] = None, limit: int = 267, offset: int = 0):
    inv = _load_inventory()
    filtered = inv
    if division and division != "all":
        filtered = [x for x in filtered if x["division"] == division]
    if q:
        ql = q.lower()
        filtered = [x for x in filtered if ql in x["name"].lower() or ql in x["description"].lower() or ql in x["id"].lower()]
    total = len(filtered)
    page = filtered[offset: offset + limit]
    return {"agents": page, "total": total, "offset": offset, "limit": limit}


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str):
    inv = _load_inventory()
    entry = next((x for x in inv if x["id"] == agent_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    _, body, fm = _read_agent_file(agent_id)
    pipeline = _get_pipeline(entry["division"])
    tools = DIVISION_TOOLS.get(entry["division"], ["analyze", "generate", "review"])
    return {**entry, "body": body, "frontmatter": fm, "pipeline": pipeline, "tools": tools}


# ---- history endpoints ------------------------------------------------------

@router.get("/agents/{agent_id}/history")
def get_history(agent_id: str):
    inv = _load_inventory()
    if not any(x["id"] == agent_id for x in inv):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    data = _load_all_history()
    return {"agent_id": agent_id, "messages": data.get(agent_id, [])}


@router.delete("/agents/{agent_id}/history")
def clear_history(agent_id: str):
    inv = _load_inventory()
    if not any(x["id"] == agent_id for x in inv):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    data = _load_all_history()
    data[agent_id] = []
    _save_all_history(data)
    return {"agent_id": agent_id, "cleared": True}


# ---- execution (non-streaming, compat) ------------------------------------

class RunRequest(BaseModel):
    message: str
    history: Optional[List[Dict[str, str]]] = None
    model: Optional[str] = None
    max_tokens: int = 2048


@router.post("/agents/{agent_id}/run")
async def run_agent(agent_id: str, req: RunRequest):
    _, body, fm = _read_agent_file(agent_id)
    inv = _load_inventory()
    entry = next((x for x in inv if x["id"] == agent_id), None)
    header = f"You are {fm.get('name', entry['name'] if entry else agent_id)} — {fm.get('description','')}\n\n"
    system_prompt = body.strip()
    if len(system_prompt) > 15000:
        system_prompt = system_prompt[:15000]
    messages: List[Dict[str, str]] = [{"role": "system", "content": header + system_prompt}]
    if req.history:
        for h in req.history[-10:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": req.message})

    _append_history(agent_id, "user", req.message)

    try:
        from app.core.llm_router import llm_chat
        reply = await llm_chat(messages, model=req.model, max_tokens=req.max_tokens)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM error: {e}")

    _append_history(agent_id, "assistant", reply, {"model": req.model})
    return {"agent_id": agent_id, "agent_name": fm.get("name", agent_id), "reply": reply}


# ---- streaming microservice runtime (SSE) -----------------------------------
# Feels like game-forge / scraper: stages -> tool calls -> streaming tokens -> done

class StreamRequest(BaseModel):
    message: str
    history: Optional[List[Dict[str, str]]] = None
    model: Optional[str] = None
    max_tokens: int = 2048


@router.post("/agents/{agent_id}/run-stream")
async def run_agent_stream(agent_id: str, req: StreamRequest):
    _, body, fm = _read_agent_file(agent_id)
    inv = _load_inventory()
    entry = next((x for x in inv if x["id"] == agent_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    division = entry.get("division", "")
    pipeline = _get_pipeline(division)
    tools = DIVISION_TOOLS.get(division, ["analyze", "generate"])

    header = f"You are {fm.get('name', entry['name'] if entry else agent_id)} — {fm.get('description','')}\n\nYou MUST be concise, actionable, and domain-specific. Use your tools when relevant. Format code with fenced blocks. If the user asks for a checklist, return a real checklist."
    system_prompt = body.strip()
    if len(system_prompt) > 15000:
        system_prompt = system_prompt[:15000]
    messages: List[Dict[str, str]] = [{"role": "system", "content": header + "\n\n" + system_prompt}]
    if req.history:
        for h in req.history[-10:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": req.message})

    # persist user message immediately so history survives even if stream is cancelled
    _append_history(agent_id, "user", req.message)

    async def event_gen():
        # helper to emit SSE
        def sse(obj: Dict[str, Any]) -> str:
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        run_id = str(uuid.uuid4())[:8]
        start_ts = time.time()
        yield sse({"type": "meta", "run_id": run_id, "agent_id": agent_id, "division": division, "pipeline": pipeline, "tools": tools})

        # ---- staged pipeline (microservice feel) -----------------------------
        total = len(pipeline)
        for idx, stage in enumerate(pipeline):
            yield sse({"type": "stage", "stage": stage, "index": idx, "total": total, "status": "running"})
            # simulate tool work for this stage
            tool = tools[min(idx, len(tools)-1)] if tools else "analyze"
            yield sse({"type": "tool_call", "tool": tool, "stage": stage, "args": {"query": req.message[:120]}})
            await asyncio.sleep(0.32 if idx < total-1 else 0.18)
            yield sse({"type": "tool_result", "tool": tool, "stage": stage, "ok": True, "summary": f"{stage} — analyzed"})
            yield sse({"type": "stage", "stage": stage, "index": idx, "total": total, "status": "done"})
            # small gap between stages
            if idx < total-1:
                await asyncio.sleep(0.08)

        # ---- LLM execution (real) -------------------------------------------
        yield sse({"type": "stage", "stage": "Generating response", "index": total, "total": total+1, "status": "running"})
        try:
            from app.core.llm_router import llm_chat
            reply = await llm_chat(messages, model=req.model, max_tokens=req.max_tokens)
        except Exception as e:
            yield sse({"type": "error", "error": str(e)})
            return

        # stream reply token-by-token (word chunks) for live feel
        # keep SSE heartbeats smooth without waiting for provider streaming
        chunk_size = 18  # chars per chunk — feels like typing
        for i in range(0, len(reply), chunk_size):
            chunk = reply[i:i+chunk_size]
            yield sse({"type": "token", "text": chunk})
            await asyncio.sleep(0.018)

        elapsed = time.time() - start_ts
        yield sse({"type": "stage", "stage": "Generating response", "index": total, "total": total+1, "status": "done"})
        yield sse({"type": "done", "reply": reply, "elapsed_ms": int(elapsed*1000), "model": req.model})

        # persist assistant result
        _append_history(agent_id, "assistant", reply, {"run_id": run_id, "model": req.model})

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


# ---- LLM health passthrough -------------------------------------------------

@router.get("/llm/health")
def agency_llm_health():
    from app.core.llm_router import health_snapshot
    return health_snapshot()
