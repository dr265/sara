"""
core/agent_router.py — voice → intent → real agent execution.

Pipeline (spec §5):
    request text → intent extraction → capability discovery → agent selection
    → input validation → task plan → REAL execution → verification → result

The LLM (core.llm_client.call_llm — the SAME provider config Jarvis already
uses) only interprets and plans; every unit of work is executed through a
real interface:

  * backend agents  → their actual HTTP endpoints on the unified backend
                      (invocation shapes taken from the real request models —
                      see INVOKE_SPEC in core.agent_registry)
  * Ultron runs     → POST /ultron/run + poll GET /ultron/status, stop via
                      POST /ultron/stop (the platform's own orchestrator is
                      reused for open-ended multi-step goals)
  * desktop actions → the live ActionRegistry handlers on this machine
  * agency agents   → local inventory match then POST /api/agency/…/run

If the LLM provider is down, routing still works through the offline keyword
scorer in the registry (spec §10). Nothing here ever reports success without
a verified response from the executing system.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from core.agent_registry import (
    AgentInfo, AgentState, UnifiedAgentRegistry,
)

#: step-level budgets (seconds)
STEP_TIMEOUT_SYNC   = 150.0     # one synchronous backend/local step
ULTRON_POLL_TIMEOUT = 600.0     # whole delegated Ultron run
ULTRON_POLL_INTERVAL = 2.0

# ── artifact collection ──────────────────────────────────────────────────
# Key names inside agent payloads that carry REAL produced outputs.
_ART_URL_KEYS = ("url", "audio_url", "audioUrl", "file_url", "fileUrl",
                 "download_url", "downloadUrl", "image", "image_url",
                 "link", "href", "path", "file", "file_path", "output_file",
                 "audio", "track_url", "trackUrl")
_ART_TITLE_KEYS = ("prompt", "title", "name", "label", "caption",
                   "filename", "file_name")

_AUDIO_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac")
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_DOC_EXT = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
            ".txt", ".md", ".csv", ".json")

#: Absolute Windows paths mentioned inside local action output text.
_TEXT_PATH_RE = re.compile(r"[A-Za-z]:\\[^\r\n\"'<>|*?]{3,180}\."
                           r"[A-Za-z0-9]{1,5}")


def _artifact_kind(ref: str) -> str:
    low = ref.lower().split("?", 1)[0]
    if low.endswith(_AUDIO_EXT):
        return "audio"
    if low.endswith(_IMAGE_EXT):
        return "image"
    if low.endswith(_DOC_EXT):
        return "document"
    return "link"


def _collect_artifacts(payload: Any, base_url: str,
                       limit: int = 12) -> List[Dict[str, Any]]:
    """Typed artifacts (audio/files/links) found in a JSON payload.

    Bounded scan (depth ≤ 3, ≤ 20 list items per level); a reference is only
    ever taken from the payload itself — nothing is constructed or guessed.
    Music-studio replies (tracks[].url → /api/music/audio/…), scraper
    documents, generated files and image URLs all flow through here.
    """
    found: List[Dict[str, Any]] = []
    seen: set = set()

    def _title_for(container: Dict[str, Any]) -> str:
        for key in _ART_TITLE_KEYS:
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:90]
        return ""

    def _add(ref: str, title: str) -> None:
        if len(found) >= limit or not ref:
            return
        key = (title, ref)
        if key in seen:
            return
        seen.add(key)
        kind = _artifact_kind(ref)
        art: Dict[str, Any] = {"kind": kind, "title": title or ref[:90]}
        if ref.startswith(("http://", "https://")):
            art["url"] = ref
        elif ref.startswith("/"):
            art["url"] = f"{base_url}{ref}"
        else:
            art["path"] = ref
        found.append(art)

    def _walk(node: Any, depth: int) -> None:
        if depth > 3 or len(found) >= limit:
            return
        if isinstance(node, dict):
            title = _title_for(node)
            for key, val in node.items():
                if isinstance(val, str) and key in _ART_URL_KEYS:
                    value = val.strip()
                    if value.startswith(("http://", "https://", "/")) or \
                            re.match(r"^[A-Za-z]:\\", value):
                        _add(value, title)
                elif isinstance(val, (dict, list)):
                    _walk(val, depth + 1)
        elif isinstance(node, list):
            for item in node[:20]:
                _walk(item, depth + 1)

    _walk(payload, 0)
    return found

#: result text handed to the voice layer (bounded so TTS stays usable)
MAX_RESULT_CHARS = 1800

_JSON_RE = re.compile(r"\{.*\}", re.S)

#: Words that mean "this is conversation, not an execution task".
_CONVERSATION_MARKERS = (
    "hello", "hi ", "hey", "good morning", "good evening", "how are you",
    "thank you", "thanks", "who are you", "what can you do",
)

_EMIT_NOOP: Callable[..., None] = lambda *a, **k: None


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """First parseable JSON object inside an LLM reply (fences tolerated)."""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(),
                     flags=re.M).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = _JSON_RE.search(cleaned)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _short(text: Any, limit: int = 400) -> str:
    s = "" if text is None else str(text)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


class TaskRouter:
    """Plans requests against the live registry and executes them for real."""

    def __init__(self, registry: UnifiedAgentRegistry,
                 llm_call: Optional[Callable[[list], Dict[str, Any]]] = None,
                 http: Optional[Any] = None):
        self.registry = registry
        #: injected for tests; defaults to core.llm_client.call_llm lazily
        self._llm_call = llm_call
        #: injected HTTP client (requests-compatible) for tests
        self._http = http or requests

    # ------------------------------------------------------------------ LLM

    def _llm(self, messages: list, timeout: int = 45) -> str:
        call = self._llm_call
        if call is None:
            from core.llm_client import call_llm as call      # real provider
        out = call(messages)
        if isinstance(out, dict):
            return str(out.get("content") or "")
        return str(out or "")

    # -------------------------------------------------------------- planning

    def _snapshot(self, limit: int = 40) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Compact, honest registry view for the planning prompt."""
        rows: List[Dict[str, Any]] = []
        for a in self.registry.selectable():
            rows.append({
                "id": a.id, "name": a.name, "caps": a.capabilities[:8],
                "state": a.state, "detail": a.detail[:120],
                "consequential": a.consequential,
                "accepts": a.accepts, "async": a.invoke_async,
            })
        # the agency meta-agent deserves a mention even before first probe
        if not any(r["id"] == "agency-agents" for r in rows):
            ag = self.registry.get_agent("agency-agents")
            if ag:
                rows.append({"id": ag.id, "name": ag.name,
                             "caps": ag.capabilities, "state": ag.state,
                             "detail": ag.detail, "consequential": False,
                             "accepts": ag.accepts, "async": False})
        blocked = [f"{a.id} ({a.state}: {a.detail[:60]})"
                   for a in self.registry.get_all_agents()
                   if a.state in (AgentState.BLOCKED.value,
                                  AgentState.UNAVAILABLE.value)
                   and a.source == "backend_agent"][:12]
        return rows[:limit], blocked

    def plan_task(self, request: str, task: Any = None) -> Dict[str, Any]:
        """Produce a validated decision for one request.

        Returns {"kind": execute|clarify|converse|error, ...}.
        Never raises (a planning failure becomes an error decision).
        """
        try:
            snapshot, blocked = self._snapshot()
            decision = self._llm_plan(request, snapshot, blocked)
        except Exception as exc:
            # LLM unavailable → offline keyword routing (spec §10)
            decision = self._fallback_plan(request)
            decision.setdefault(
                "notes", f"offline fallback ({type(exc).__name__})")
        return self._validate(decision, request)

    def _llm_plan(self, request: str, snapshot: List[Dict[str, Any]],
                  blocked: List[str]) -> Dict[str, Any]:
        system = (
            "You are the task router of Jarvis, a voice assistant running in "
            "SARA Agentic Mode. Decide what to do with the user's request. "
            "You NEVER claim work was done — you only classify and select "
            "real agents that will actually execute.\n\n"
            "OUTPUT ONLY JSON, no prose:\n"
            '{"kind": "execute" | "clarify" | "converse",\n'
            ' "agents": ["<agent id>", ...],        // execution order\n'
            ' "params": {"<agent id>": {...}},      // real arguments per accepts\n'
            ' "question": "<only for clarify: the ONE missing fact to ask>",\n'
            ' "reason": "<max 20 words>"}\n\n'
            "RULES\n"
            "1. Use ONLY ids from AVAILABLE AGENTS. Never invent an id.\n"
            "2. 'converse' for greetings/smalltalk that need no execution.\n"
            "3. 'clarify' only when a required fact is genuinely missing "
            "(a URL to scrape, a recipient, which contact) — ask ONE question.\n"
            "4. For multi-part goals (research AND analyse AND report AND "
            "email), list every needed agent in execution order — the "
            "supervisor 'sara-ultron' is best for open-ended combined goals: "
            'give it {"goal": "<full natural request>"} in params.\n'
            "5. For a domain-specialist request (deep knowledge question, "
            "analysis, report), you may use 'agency-agents' with params "
            '{"agent_id": "<best inventory id>", "message": "<request>"} — '
            "prefer it when no operational agent fits better.\n"
            "6. Never select an agent listed in UNAVAILABLE/BLOCKED; if the "
            "only fit is blocked, use 'clarify' with a question stating what "
            "credential/setup is missing.\n"
            "7. params values must satisfy each agent's 'accepts'. Required "
            "fields marked REQUIRED must be filled from the request.\n"
            "8. Desktop actions (id desktop_action:<name>) execute locally "
            "when the task is a local computer action.\n"
        )
        snap_text = json.dumps(snapshot, ensure_ascii=False, default=str)
        blocked_text = ", ".join(blocked) or "none"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content":
                f"AVAILABLE AGENTS\n{snap_text}\n\n"
                f"UNAVAILABLE/BLOCKED (do not select)\n{blocked_text}\n\n"
                f"USER REQUEST\n{request}"},
        ]
        reply = self._llm(messages, timeout=45)
        decision = extract_json(reply)
        if decision is None:
            raise RuntimeError("router reply was not JSON")
        return decision

    def _fallback_plan(self, request: str) -> Dict[str, Any]:
        """Offline routing: conversation check → registry keyword scorer."""
        low = request.lower().strip()
        if any(m in low[:60] for m in _CONVERSATION_MARKERS) and len(low) < 40:
            return {"kind": "converse", "reason": "smalltalk"}
        best = self.registry.find_best_agent(request)
        if best is None:
            return {"kind": "clarify", "no_agent": True,
                    "question": ("I could not find an agent ready for that. "
                                 "What outcome exactly do you need?")}
        # Build usable params from the request so the degraded (offline) path
        # still feeds each agent the fields its endpoint requires.
        accepts = getattr(best, "accepts", None) or {}
        if isinstance(accepts, dict) and "properties" in accepts:
            accepts = accepts.get("properties") or {}
        keys = set(accepts.keys()) if isinstance(accepts, dict) else set()
        params: Dict[str, Any] = {}
        if "url" in keys:
            import re
            m = re.search(r"\b([a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?)\b",
                          request)
            if m:
                params["url"] = m.group(1)
        if "prompt" in keys:
            params["prompt"] = request
        if "goal" in keys and best.id in ("sara-ultron", "sara-auto"):
            params["goal"] = request
        if not params and best.id in ("sara-ultron", "sara-auto"):
            params = {"goal": request}
        if params:
            params = {best.id: params}
        return {"kind": "execute", "agents": [best.id], "params": params,
                "reason": "keyword match"}

    # ----------------------------------------------------------- validation

    def _validate(self, decision: Dict[str, Any], request: str) -> Dict[str, Any]:
        """Reject invented or unselectable agent ids (spec §5 step 3-4)."""
        kind = str(decision.get("kind") or "").lower()
        if kind not in ("execute", "clarify", "converse"):
            if kind in ("error", ""):
                return {"kind": "error",
                        "error": str(decision.get("error") or "no decision")}
            return {"kind": "converse", "reason": f"unknown kind {kind}"}
        if kind != "execute":
            return decision
        agents = [str(a) for a in (decision.get("agents") or []) if a]
        kept: List[str] = []
        dropped: List[str] = []
        for agent_id in agents:
            info = self.registry.get_agent(agent_id)
            if info is None or not info.invocable:
                dropped.append(agent_id)
            else:
                kept.append(agent_id)
        if not kept:
            blocked_note = f" (dropped unavailable: {', '.join(dropped)})" \
                if dropped else ""
            return {"kind": "clarify", "no_agent": True,
                    "question": (f"No ready agent can perform that right now"
                                 f"{blocked_note}. Which alternative outcome "
                                 f"would you like?")}
        params = decision.get("params")
        if not isinstance(params, dict):
            params = {}
        # agency meta-agent without a chosen specialist → local match later
        return {"kind": "execute", "agents": kept, "params": params,
                "question": "", "reason": _short(decision.get("reason"), 120),
                "dropped": dropped}

    def requires_confirmation(self, decision: Dict[str, Any]) -> bool:
        """True when at least one selected agent's normal work is an external
        consequential action (send/publish/trade) — gate before executing."""
        if decision.get("approved"):
            return False
        for agent_id in (decision.get("agents") or []):
            info = self.registry.get_agent(str(agent_id))
            if info is not None and info.consequential:
                return True
        return False

    # ------------------------------------------------------------- execution

    def execute_task(self, task: Any,
                     emit: Callable[..., None] = _EMIT_NOOP) -> str:
        """Run every planned step for real, in order, with output passing.

        Sequential on purpose: most chains are prerequisites (scrape → analyse
        → email), cancellation stays responsive between steps, and a failed
        prerequisite stops the chain (no false success — spec §6).
        Returns the combined verified result text.
        """
        decision = task.decision or {}
        agents: List[str] = list(decision.get("agents") or [])
        params_by_agent: Dict[str, Any] = decision.get("params") or {}
        outputs: Dict[str, str] = {}
        previous = ""
        results: List[str] = []

        for index, agent_id in enumerate(agents):
            if task.cancel.is_set():
                emit(task, "cancelled", f"Stopped before {agent_id}.")
                break
            info = self.registry.get_agent(agent_id)
            if info is None:
                task.error = f"agent '{agent_id}' vanished from the registry"
                emit(task, "error", task.error)
                break

            params = dict(params_by_agent.get(agent_id) or {})
            # cross-step output passing for follow-on agents
            if previous:
                params.setdefault("__previous_output__", previous)
            if index > 0 and info.id == "agency-agents" and \
                    not params.get("message"):
                params["message"] = task.request
            self._inject_request_context(params, task, info)

            self.registry.set_running(agent_id, task.task_id)
            step = task.steps[index] if index < len(task.steps) else None
            if step is not None:
                step["state"] = "running"
                step["t_start"] = time.time()
            emit(task, "step_start",
                 f"{info.name}: executing…", agent=agent_id)

            ok, output, external_id = self._invoke_agent(
                info, params, task, emit)
            if external_id:
                task.external_task_id = external_id

            if step is not None:
                step["state"] = "done" if ok else "failed"
                step["output"] = _short(output, 600)
                step["t_end"] = time.time()
                if not ok:
                    step["error"] = _short(output, 400)
            self.registry.set_finished(agent_id, ok, _short(output, 300))

            if task.cancel.is_set():
                emit(task, "cancelled", f"Stopped during {info.name}.")
                break
            if not ok:
                task.error = (f"{info.name} failed: {_short(output, 400)}")
                emit(task, "step_failed", task.error, agent=agent_id)
                break   # prerequisite failed → no false success (spec §6)

            outputs[agent_id] = output
            previous = output
            results.append(f"{info.name}: {_short(output, 700)}")
            emit(task, "step_done", f"{info.name}: OK", agent=agent_id)

        if task.cancel.is_set() or task.error:
            return ""
        if not results:
            task.error = "no step produced a result"
            return ""
        return self._combine(results)

    @staticmethod
    def _inject_request_context(params: Dict[str, Any], task: Any,
                                info: AgentInfo) -> None:
        """Guarantee the raw request reaches the agent even when the LLM's
        params were thin — required fields only, never overwriting real ones."""
        req = task.request
        if info.id == "sara-ultron" or info.id == "sara-auto":
            params.setdefault("goal", req)
        elif info.id == "agency-agents":
            params.setdefault("message", req)
            if not params.get("agent_id"):
                match = task.decision.get("agency_match") or ""
                params.setdefault("agent_id", match)
        elif info.source in ("desktop_action", "plugin"):
            params.setdefault("text", req)
            params.setdefault("query", req)
            params.setdefault("task", req)

    @staticmethod
    def _combine(results: List[str]) -> str:
        text = "\n".join(f"• {r}" for r in results)
        if len(text) > MAX_RESULT_CHARS:
            text = text[: MAX_RESULT_CHARS - 1] + "…"
        return text

    # ------------------------------------------------- artifact collection

    def _note_artifacts(self, payload: Any, task: Any) -> None:
        """Record real outputs found in a backend JSON reply on the task."""
        try:
            arts = getattr(task, "artifacts", None)
            if arts is None:
                return
            existing = {(a.get("kind"), a.get("url") or a.get("path"))
                        for a in arts if isinstance(a, dict)}
            for art in _collect_artifacts(payload, self.registry.backend_url):
                key = (art.get("kind"), art.get("url") or art.get("path"))
                if key not in existing and len(arts) < 20:
                    arts.append(art)
                    existing.add(key)
        except Exception:
            pass

    def _note_text_artifacts(self, text: Any, task: Any) -> None:
        """Absolute file paths mentioned in an output become file artifacts."""
        try:
            arts = getattr(task, "artifacts", None)
            if arts is None:
                return
            existing = {a.get("path") for a in arts if isinstance(a, dict)}
            for match in _TEXT_PATH_RE.findall(str(text or ""))[:4]:
                path = match.strip()
                if path in existing or len(arts) >= 20:
                    continue
                arts.append({"kind": _artifact_kind(path),
                             "title": path.rsplit("\\", 1)[-1][:90],
                             "path": path})
                existing.add(path)
        except Exception:
            pass

    # -------------------------------------------------- ADAPTIVE fast path

    #: Desktop actions that must never run from the ADAPTIVE fast path even
    #: with a strong word score — they change the machine or drive the UI.
    _FASTPATH_BLOCKED = {
        "desktop_action:shutdown_sara",
        "desktop_action:computer_control",
        "desktop_action:computer_settings",
        "desktop_action:file_controller",
        "desktop_action:dev_agent",
    }

    def local_fast_plan(self, request: str) -> Optional[Dict[str, Any]]:
        """Unambiguous local action → ready-to-run decision, or None.

        ADAPTIVE Stage 1 uses this to skip LLM planning for obvious local
        commands. Strict on purpose: at least 2 content words, a solid score
        on name/capabilities, and a clear margin over the runner-up — an
        ambiguous request is left to the normal planner. Execution still
        goes through the full verified task machinery.
        """
        words = {w for w in re.findall(r"[a-z0-9]+", (request or "").lower())
                 if len(w) > 2}
        if len(words) < 2:
            return None
        scored: List[Tuple[int, AgentInfo]] = []
        for agent in self.registry.get_all_agents():
            if agent.source != "desktop_action" \
                    or agent.id in self._FASTPATH_BLOCKED:
                continue
            if not agent.invocable or agent.consequential:
                continue
            name_cap_words = set(re.findall(
                r"[a-z0-9]+",
                (agent.name + " " + " ".join(agent.capabilities)).lower()))
            desc_words = set(re.findall(r"[a-z0-9]+",
                                        (agent.description or "").lower()))
            score = 0
            for w in words:
                if w in name_cap_words:
                    score += 3
                elif w in desc_words:
                    score += 1
                elif len(w) > 4 and any(
                        bw.startswith(w) for bw in name_cap_words
                        if len(bw) > 3):
                    score += 2
            if score:
                scored.append((score, agent))
        if not scored:
            return None
        scored.sort(key=lambda t: t[0], reverse=True)
        best_score, best = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0
        if best_score < 3 or (best_score - runner_up) < 3:
            return None
        # Safety: only actions that can interpret a free-text request on
        # their own may run here. Actions with structured parameters
        # (app_name, city, …) need the planner to fill them — the fast path
        # passes no args, so auto-running them could misfire. Leave those
        # to Stage 2.
        accepts = getattr(best, "accepts", None) or {}
        if accepts and not any(k in accepts for k in ("text", "query", "task")):
            return None
        return {"kind": "execute", "agents": [best.id],
                "params": {best.id: {}}, "question": "",
                "reason": f"direct local match: {best.name}",
                "fast_path": True, "dropped": []}

    # ------------------------------------------------------- agent invocation

    def _invoke_agent(self, info: AgentInfo, params: Dict[str, Any],
                      task: Any,
                      emit: Callable[..., None]) -> Tuple[bool, str, str]:
        """One agent execution. Returns (ok, output, external_task_id)."""
        try:
            if info.source in ("desktop_action", "plugin"):
                result: Tuple[bool, str, str] = (
                    True, self._invoke_local(info, params), "")
            elif info.id == "agency-agents":
                result = self._invoke_agency(params, task, emit)
            elif not info.invoke_path:
                result = (False,
                          (f"{info.name} exposes no execution endpoint — "
                           f"not attempted. {info.metadata.get('notes','')}"), "")
            elif info.invoke_async:
                result = self._invoke_async_backend(info, params, task, emit)
            else:
                result = self._invoke_sync_backend(info, params, task, emit)
            if result[0]:
                self._note_text_artifacts(result[1], task)
            return result
        except Exception as exc:   # honest failure, never a fake success
            return False, f"{info.name}: {type(exc).__name__}: {exc}", ""

    def _invoke_local(self, info: AgentInfo, params: Dict[str, Any]) -> str:
        """Run a desktop action handler exactly as the Live session does."""
        name = (info.tools or [""])[0]
        runner = self.registry.local_runner(name)
        if runner is None:
            raise RuntimeError(f"local handler for '{name}' not bound")
        from core.action_loader import _call_handler
        out = _call_handler(runner, params, {})
        return str(out or "Done.")

    def _invoke_agency(self, params: Dict[str, Any], task: Any,
                       emit: Callable[..., None]) -> Tuple[bool, str, str]:
        """Match a specialist from the real inventory, then run it."""
        agent_id = str(params.get("agent_id") or "").strip()
        message = str(params.get("message") or task.request)
        if not agent_id:
            matches = self.registry.find_agency_agents(
                params.get("__previous_output__") or message, limit=1)
            if not matches:
                matches = self.registry.find_agency_agents(message, limit=1)
            if not matches:
                return False, ("No agency specialist matched this request "
                               "(inventory search found nothing)."), ""
            agent_id = str(matches[0].get("id") or "")
            emit(task, "step_note",
                 f"Agency specialist selected: {matches[0].get('name', agent_id)}")
        body = {"message": message,
                "max_tokens": int(params.get("max_tokens") or 2048)}
        url = (f"{self.registry.backend_url}/api/agency/agents/"
               f"{agent_id}/run")
        resp = self._http.post(url, json=body,
                               headers=self.registry.conn.headers(),
                               timeout=STEP_TIMEOUT_SYNC)
        if resp.status_code >= 400:
            return False, f"agency/{agent_id} HTTP {resp.status_code}: " \
                          f"{_short(resp.text, 300)}", ""
        try:
            payload = resp.json()
        except Exception:
            return True, _short(resp.text, 1500), ""
        self._note_artifacts(payload, task)
        reply = payload.get("reply") or payload.get("content") \
            or payload.get("result") or json.dumps(payload, default=str)[:1500]
        return True, str(reply)[:1500], ""

    # -- synchronous backend POST (JSON or SSE responses) ------------------

    def _invoke_sync_backend(self, info: AgentInfo, params: Dict[str, Any],
                             task: Any,
                             emit: Callable[..., None]) -> Tuple[bool, str, str]:
        url = f"{self.registry.backend_url}{info.invoke_path}"
        body = self._clean_params(params)
        resp = self._http.post(url, json=body,
                               headers=self.registry.conn.headers(),
                               timeout=STEP_TIMEOUT_SYNC, stream=True)
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str(resp.json().get("detail") or resp.text)
            except Exception:
                detail = resp.text
            return False, (f"{info.name} rejected the request "
                           f"(HTTP {resp.status_code}): {_short(detail, 400)}"), ""
        ctype = (resp.headers.get("content-type") or "").lower()
        if "event-stream" in ctype:
            return self._consume_sse(resp, info, task, emit)
        try:
            payload = resp.json()
        except Exception:
            return True, _short(resp.text, 1500), ""
        self._note_artifacts(payload, task)
        return True, self._extract_payload(payload), ""

    @staticmethod
    def _clean_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Drop router-internal keys before hitting a real API."""
        return {k: v for k, v in params.items()
                if not k.startswith("__")}

    @staticmethod
    def _extract_payload(payload: Any) -> str:
        """Best human-readable rendering of a JSON reply (bounded)."""
        if isinstance(payload, str):
            return _short(payload, 1500)
        if isinstance(payload, dict):
            for key in ("reply", "result", "content", "text", "report",
                        "summary", "answer", "message", "detail"):
                val = payload.get(key)
                if isinstance(val, (str, int, float)) and str(val).strip():
                    return _short(val, 1500)
            return _short(json.dumps(payload, ensure_ascii=False,
                                     default=str), 1500)
        return _short(str(payload), 1500)

    def _consume_sse(self, resp: Any, info: AgentInfo, task: Any,
                     emit: Callable[..., None]) -> Tuple[bool, str, str]:
        """Read a Server-Sent-Events execution stream to its end.

        Collects `data:` events, surfaces progress lines, and returns the
        terminal event's payload (or a bounded concatenation). A stream that
        dies mid-way is reported as a failure — never as a result.
        """
        events: List[Any] = []
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                chunk = raw[5:].strip()
                if chunk in ("[DONE]", "done"):
                    break
                try:
                    events.append(json.loads(chunk))
                except Exception:
                    events.append(chunk)
        except Exception as exc:
            return False, f"{info.name} stream aborted: {exc}", ""
        if not events:
            return False, f"{info.name} returned an empty stream.", ""
        # terminal = last dict carrying a result-ish key, else the last event
        terminal: Any = events[-1]
        for ev in reversed(events):
            if isinstance(ev, dict) and any(
                    k in ev for k in ("result", "report", "answer", "final",
                                      "output", "content", "reply", "error",
                                      "done", "status")):
                terminal = ev
                break
        if isinstance(terminal, dict):
            if terminal.get("error"):
                return False, _short(terminal["error"], 800), ""
            status = str(terminal.get("status") or "").lower()
            if status in ("failed", "error"):
                return False, _short(terminal, 800), ""
            self._note_artifacts(terminal, task)
            return True, self._extract_payload(terminal), ""
        return True, _short(terminal, 1500), ""

    # -- asynchronous backend runs (Ultron / sara-auto / game / architect) --

    def _invoke_async_backend(self, info: AgentInfo, params: Dict[str, Any],
                              task: Any,
                              emit: Callable[..., None]) -> Tuple[bool, str, str]:
        """POST for a task id, then poll until a real terminal phase.

        The status path is derived per agent (platforms differ), the terminal
        vocabulary is the one the backend itself declares, and the loop
        honours task.cancel — a cancel issues the agent's stop route and
        waits for its confirmed phase before reporting cancelled.
        """
        base = self.registry.backend_url
        url = f"{base}{info.invoke_path}"
        body = self._clean_params(params)
        resp = self._http.post(url, json=body,
                               headers=self.registry.conn.headers(), timeout=30)
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str(resp.json().get("detail") or resp.text)
            except Exception:
                detail = resp.text
            return False, (f"{info.name} rejected the request "
                           f"(HTTP {resp.status_code}): {_short(detail, 400)}"), ""
        try:
            started = resp.json()
        except Exception:
            return False, f"{info.name} returned a non-JSON start response.", ""

        external_id = str(started.get("task_id")
                          or started.get("id")
                          or started.get("session_id") or "")
        if not external_id:
            # some endpoints do the whole job synchronously after all
            return True, self._extract_payload(started), ""
        emit(task, "external", f"{info.name} run {external_id[:8]} started "
                               f"on the backend", external_id=external_id)

        status_path = {
            "sara-ultron": "/ultron/status?task_id=",
            "sara-auto": "/sara-auto/status?task_id=",
        }.get(info.id)
        if status_path is None:
            # architect/gameforge: no shared poll contract — report the real
            # handle honestly instead of inventing completion.
            return True, (f"{info.name} started (id {external_id}). "
                          f"Track it via {info.output_format}"), external_id

        deadline = time.time() + ULTRON_POLL_TIMEOUT
        terminal = {"completed", "completed_with_issues", "failed", "stopped"}
        last_phase = ""
        snapshot: Dict[str, Any] = {}
        while time.time() < deadline:
            if task.cancel.is_set():
                stopped = self.stop_external(external_id, info_id=info.id)
                note = "" if stopped else " (backend stop FAILED)"
                return False, f"cancelled by user{note}", external_id
            try:
                s = self._http.get(f"{base}{status_path}{external_id}",
                                   headers=self.registry.conn.headers(),
                                   timeout=10)
                if s.status_code >= 400:
                    return False, (f"{info.name} status HTTP {s.status_code}: "
                                   f"{_short(s.text, 200)}"), external_id
                snapshot = s.json()
            except Exception as exc:
                emit(task, "poll_retry", f"status poll failed: {exc}")
                time.sleep(ULTRON_POLL_INTERVAL)
                continue
            phase = str(snapshot.get("phase") or snapshot.get("status") or "")
            if phase != last_phase:
                last_phase = phase
                cur = snapshot.get("current_tool")
                emit(task, "progress",
                     f"{info.name}: {phase or 'working'}"
                     + (f" ({cur})" if cur else ""),
                     phase=phase)
            if phase in terminal:
                self._note_artifacts(snapshot, task)
                return self._verdict_ultron(info, snapshot, external_id)
            time.sleep(ULTRON_POLL_INTERVAL)
        return False, (f"{info.name} did not finish within "
                       f"{int(ULTRON_POLL_TIMEOUT)}s (phase "
                       f"'{last_phase or '?'}') — still running on the "
                       f"backend."), external_id

    @staticmethod
    def _verdict_ultron(info: AgentInfo, snapshot: Dict[str, Any],
                        external_id: str) -> Tuple[bool, str, str]:
        """Terminal phase + real step results → verdict, never a guess."""
        phase = str(snapshot.get("phase") or "")
        steps = snapshot.get("steps") or []
        plan = snapshot.get("plan") or []
        done = sum(1 for s in steps if s.get("ok") or s.get("skipped"))
        failed = sum(1 for s in steps if not s.get("ok") and not s.get("skipped"))
        lines = [f"{info.name} run {external_id[:8]} → {phase}",
                 f"steps: {done}/{len(plan) or len(steps)} ok"
                 + (f", {failed} failed" if failed else "")]
        for s in steps[-4:]:
            mark = "OK" if (s.get("ok") or s.get("skipped")) else "FAIL"
            lines.append(f"  [{mark}] {s.get('tool', '?')}: "
                         f"{_short(s.get('output'), 220)}")
        ok = phase in ("completed", "completed_with_issues")
        if phase == "completed_with_issues":
            lines.append("(completed with issues — see step list)")
        if phase == "stopped":
            lines.append("run stopped by request")
        return ok, "\n".join(lines)[:MAX_RESULT_CHARS], external_id

    def stop_external(self, external_task_id: str,
                      info_id: str = "sara-ultron") -> bool:
        """Ask the backend to actually stop a run; True only on a 2xx."""
        path = ("/ultron/stop" if info_id == "sara-ultron"
                else "/sara-auto/stop")
        try:
            resp = self._http.post(
                f"{self.registry.backend_url}{path}",
                json={"task_id": external_task_id},
                headers=self.registry.conn.headers(), timeout=10)
            return resp.status_code < 300
        except Exception:
            return False








