"""
Workspace opener — desktop action for Jarvis voice-controlled agent launch.

Voice phrases like "activate the backend architect", "open the music studio"
or "open sara muse" resolve to either:
  1. one of the 267 Agency specialists (aliases + inventory search)
     -> http://127.0.0.1:3000/agency/<id>
  2. a Hub service workspace such as Music Studio, Sara Muse, Sara Ultron,
     Game Forge or Sara Bots  ->  http://127.0.0.1:3000/hub/<slug>
and the page is opened in the browser (existing Hub window brought forward).

History: this action used to know only the agency catalog, so "open the
music studio agent" answered "Could not resolve ..." even though the Hub
page exists — the hub map below fixes exactly that.
"""
import time
import webbrowser
import ctypes
import sys
from pathlib import Path

# Reuse window management from existing desktop infra when available
try:
    from core.window_context import get_foreground_window_info  # type: ignore
except Exception:
    get_foreground_window_info = None  # type: ignore

import os
try:
    from core.backend_url import resolve_backend_url, resolve_frontend_url
    FRONTEND_URL = resolve_frontend_url()
    BACKEND_URL = resolve_backend_url()
except Exception:
    FRONTEND_URL = os.environ.get("SARA_FRONTEND_URL", "http://127.0.0.1:3000")
    BACKEND_URL = os.environ.get("SARA_BACKEND_URL", "http://127.0.0.1:8000")

_ALIAS_FILE = Path(__file__).resolve().parent.parent / "config" / "agency_aliases.json"

# Curated voice aliases — maps natural speech to agent ids
# (auto-generated + hand-tuned; inventory search is the fallback)
AGENCY_ALIASES: dict[str, str] = {
    # common shorthand people actually say
    "backend architect": "engineering-backend-architect",
    "frontend developer": "engineering-frontend-developer",
    "code reviewer": "engineering-code-reviewer",
    "security": "security-vulnerability-scanner",
    "trading": "finance-portfolio-manager",
    "marketing": "marketing-social-media-strategist",
    "email": "marketing-email-strategist",
    "seo": "marketing-seo-specialist",
    "tiktok": "marketing-tiktok-strategist",
    "finance": "finance-portfolio-manager",
}

# Hub service workspaces (frontend/src/app/hub/*) — these are NOT agency
# specialists. Music Studio, Sara Muse and friends live here. Spoken names
# are matched whole-phrase; the agency catalog keeps priority for its own
# names (e.g. "backend architect" stays an agency specialist).
HUB_ROUTES: dict[str, str] = {
    "music studio": "music-studio",
    "music-studio": "music-studio",
    "sara muse": "sara-muse",
    "saramuse": "sara-muse",
    "muse": "sara-muse",
    "sara ultron": "sara-ultron",
    "ultron": "sara-ultron",
    "sara bots": "sara-bots",
    "game forge": "game-forge",
    "gameforge": "game-forge",
    "sara auto": "sara-auto",
    "sara sales": "sara-sales",
    "voice agent": "voice-agent",
    "whatsapp agent": "whatsapp-agent",
    "whatsapp": "whatsapp-agent",
    "telegram assistant": "telegram-assistant",
    "telegram": "telegram-assistant",
    "trading agent": "trading-agent",
    "edu learner": "edulearner-ai",
    "edulearner": "edulearner-ai",
    "ai web scraper": "ai-web-scraper",
    "web scraper": "ai-web-scraper",
    "scraper agent": "ai-web-scraper",
    "email automation": "email-automation",
    "social media hub": "social-media",
    "backend architect hub": "backend-architect",
    "frontend developer hub": "frontend-developer",
}

#: Explicit slugs accepted as ids: agent_id="music-studio" opens the hub page.
HUB_SLUGS = set(HUB_ROUTES.values())


def _resolve_hub_route(query: str) -> str | None:
    """Exact spoken name or explicit slug -> hub slug, else None."""
    q = (query or "").strip().lower()
    if not q:
        return None
    if q in HUB_ROUTES:
        return HUB_ROUTES[q]
    if q in HUB_SLUGS:
        return q
    return None


def _resolve_hub_route_fuzzy(query: str) -> str | None:
    """Hub slug for names spoken inside a sentence ("open the music studio").

    Whole-phrase matches only, and only for keys of 5+ characters, so short
    keys cannot fire inside unrelated words ("amuse" must not match "muse").
    """
    slug = _resolve_hub_route(query)
    if slug:
        return slug
    import re as _re
    q = (query or "").strip().lower()
    for key, key_slug in HUB_ROUTES.items():
        if len(key) < 5:
            continue
        if _re.search(r"\b" + _re.escape(key) + r"\b", q):
            return key_slug
    return None


def _all_aliases() -> dict[str, str]:
    base = dict(AGENCY_ALIASES)
    # load user overrides
    try:
        import json
        if _ALIAS_FILE.exists():
            extra = json.loads(_ALIAS_FILE.read_text(encoding="utf-8"))
            if isinstance(extra, dict):
                base.update({k.lower(): v for k, v in extra.items()})
    except Exception:
        pass
    return base

def _resolve_agent_id(query: str) -> str | None:
    """Resolve a free-text query to an agency agent id."""
    q = (query or "").strip().lower()
    if not q:
        return None
    aliases = _all_aliases()
    # 1) exact alias
    if q in aliases:
        return aliases[q]
    # 2) substring alias match
    for k, v in aliases.items():
        if k in q or q in k:
            return v
    # 3) search inventory (local file)
    try:
        import json
        inv_path = Path(__file__).resolve().parent.parent.parent / "backend" / "agency_inventory.json"
        if not inv_path.exists():
            inv_path = Path(__file__).resolve().parent.parent / "backend" / "agency_inventory.json"
        data = json.loads(inv_path.read_text(encoding="utf-8"))
        ql = q.lower()
        # score by name/division/id
        best = None
        best_score = -1
        for entry in data:
            score = 0
            name = (entry.get("name","") or "").lower()
            div = (entry.get("division","") or "").lower()
            aid = (entry.get("id","") or "").lower()
            desc = (entry.get("description","") or "").lower()
            if ql == name: score = 100
            elif name in ql or ql in name: score = 80
            elif ql in aid: score = 70
            elif ql in div: score = 60
            elif ql in desc: score = 40
            # also token match
            tokens = ql.split()
            for tok in tokens:
                if len(tok) >= 3 and tok in name: score += 10
                if len(tok) >= 3 and tok in div: score += 5
            if score > best_score:
                best_score = score; best = entry["id"]
        if best and best_score >= 40:
            return best
    except Exception as e:
        print(f"[agency_opener] inventory search failed: {e}")
    return None

def _open_frontend_route(path: str) -> str:
    url = f"{FRONTEND_URL.rstrip('/')}{path}"
    try:
        webbrowser.open(url)
        # Try to foreground browser
        if sys.platform == "win32":
            try:
                import time as _t
                _t.sleep(0.6)
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                ctypes.windll.user32.ShowWindow(hwnd, 9)
            except Exception:
                pass
        return f"Opened {url} in browser."
    except Exception as e:
        return f"Failed to open browser: {e}"

def _bring_hub_forward() -> None:
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        # Find SARA Hub / browser window by title substring
        EnumWindows = user32.EnumWindows
        GetWindowTextW = user32.GetWindowTextW
        GetWindowTextLengthW = user32.GetWindowTextLengthW
        IsWindowVisible = user32.IsWindowVisible
        found = []
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _cb(hwnd, _):
            if not IsWindowVisible(hwnd): return True
            l = GetWindowTextLengthW(hwnd)
            if l == 0: return True
            buf = ctypes.create_unicode_buffer(l+1)
            GetWindowTextW(hwnd, buf, l+1)
            t = buf.value or ""
            if any(k in t.lower() for k in ("sara", "nexus", "chrome", "msedge", "firefox")):
                found.append((hwnd, t))
            return True
        EnumWindows(_cb, 0)
        if found:
            hwnd = found[0][0]
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def open_agency_workspace(parameters=None, **ctx) -> str:
    params = parameters or {}
    query = (params.get("agent") or params.get("query") or params.get("name") or params.get("agent_id") or "").strip()
    agent_id = (params.get("agent_id") or "").strip()

    resolved_agency: str | None = None
    resolved_hub: str | None = None

    if agent_id and "-" in agent_id:
        # An explicit id/slug. Hub slugs open the hub page directly.
        if agent_id in HUB_SLUGS:
            resolved_hub = agent_id
        else:
            resolved_agency = agent_id
    else:
        spoken = query or agent_id
        # Unambiguous hub phrases ("music studio", "sara muse", "game forge")
        # resolve first; the agency catalog keeps its own names untouched
        # ("backend architect" is an agency specialist, not a hub page).
        resolved_hub = _resolve_hub_route_fuzzy(spoken)
        if not resolved_hub:
            resolved_agency = _resolve_agent_id(spoken)
            if not resolved_agency and query and "-" in query:
                if query in HUB_SLUGS:
                    resolved_hub = query
                else:
                    resolved_agency = query  # assume they gave an agency id

    if resolved_hub:
        return _open_frontend_route(f"/hub/{resolved_hub}")
    if resolved_agency:
        return _open_frontend_route(f"/agency/{resolved_agency}")

    return (
        f"Could not resolve '{query or agent_id}' to an agency specialist or a Hub workspace. "
        "Try 'backend architect', 'tiktok strategist', 'music studio', 'sara muse' or 'game forge'."
    )

def switch_agency(parameters=None, **ctx) -> str:
    """Alias with slightly different phrasing for model routing."""
    return open_agency_workspace(parameters, **ctx)


TOOL = {
    "name": "open_agency_workspace",
    "description": (
        "Opens a workspace in SARA Hub in the browser. Covers BOTH catalogs: "
        "the 267 Agency specialists ('backend architect', 'tiktok strategist', 'finance portfolio manager') "
        "and the Hub service workspaces ('music studio', 'sara muse', 'sara ultron', 'game forge', 'sara bots', 'voice agent'). "
        "Pass the spoken name as `agent` (e.g. 'music studio') or the exact id/slug as `agent_id` "
        "(e.g. 'engineering-backend-architect', 'music-studio'). "
        "This opens http://127.0.0.1:3000/agency/<id> or http://127.0.0.1:3000/hub/<slug> and brings it to the foreground. "
        "Always call this tool when the user wants to open, browse, activate, or switch to a specific agent or hub workspace. "
        "Note: to CREATE music (not open the page) use the music_studio tool instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "agent": {"type": "STRING", "description": "Spoken agent name, e.g. 'backend architect', 'tiktok strategist', 'security vulnerability scanner'"},
            "agent_id": {"type": "STRING", "description": "Exact agency id, e.g. 'engineering-backend-architect' (alternative to agent)"},
            "query": {"type": "STRING", "description": "Alias for agent"},
        },
        "required": []
    },
    "handler": open_agency_workspace,
}
