"""Sara Bots browser — the bot's OWN durable browser, owned by the BACKEND.

Why it is built this way (this is the answer to "the browser is outside the
website and if the window closes?"):

* The backend owns the session — not the Sara web page, not the desktop
  agent. Closing the web UI changes nothing; the bot keeps working and the
  chat/history is there when you come back.
* Every bot gets its own PERSISTENT Chromium profile under
  ``backend/.sara_bot_profiles/<bot_id>``. Log into a site once inside the
  bot's window and the cookies survive service restarts and closed windows.
* If the window is closed, the next action relaunches the SAME profile and
  says so in its output — the task continues instead of dying with a window.
* All Playwright objects live on ONE dedicated worker thread (the sync API
  is thread-affine). Async callers await futures from the event loop, so the
  worker loop and the HTTP layer never block on the driver.
* Honest failures only: every error is reported with its real cause.

Modes: windowed by default (you can watch and log in). Set
``SARA_BOTS_BROWSER_MODE=headless`` for a no-window mode.
"""
from __future__ import annotations

import asyncio
import atexit
import base64
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("sara.bots.browser")

_PROFILE_ROOT = Path(__file__).resolve().parents[3] / ".sara_bot_profiles"
_SHOTS = _PROFILE_ROOT / "_screenshots"

NAV_TIMEOUT_MS = 45_000
OP_TIMEOUT_MS = 20_000
MAX_PAGE_TEXT = 6000
MAX_EMBED_PNG_B64 = 4_000_000

_URL_RE = re.compile(r"^https?://", re.I)
_SAFE_ID_RE = re.compile(r"[^a-z0-9_]")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sara-bot-browser")
_pw = None                     # sync_playwright instance (executor thread only)
_sessions: Dict[str, Dict[str, Any]] = {}   # bot_id -> {context, page, ...}


# ── small helpers ────────────────────────────────────────────────────────────
def _safe(bot_id: str) -> str:
    return _SAFE_ID_RE.sub("", (bot_id or "").lower()) or "bot"


def _profile_dir(bot_id: str) -> Path:
    return _PROFILE_ROOT / _safe(bot_id)


def _headless() -> bool:
    return (os.environ.get("SARA_BOTS_BROWSER_MODE") or "").strip().lower() == "headless"


def _describe(exc: Exception) -> str:
    first = str(exc).splitlines()[0][:400] if str(exc) else ""
    return f"{type(exc).__name__}: {first}" if first else type(exc).__name__


def _closed_err(exc: Exception) -> bool:
    text = str(exc).lower()
    return "targetclosed" in type(exc).__name__.lower() or \
        "closed" in text or "disconnected" in text


# ── availability probe (for the tool registry) ───────────────────────────────
_probe_cache: Dict[str, Any] = {"at": 0.0, "ok": False, "detail": ""}


def _probe_sync() -> Tuple[bool, str]:
    try:
        import playwright  # noqa: F401 — presence check only
    except Exception as exc:  # noqa: BLE001
        return False, ("playwright is not installed in the backend venv "
                       f"({_describe(exc)}) — run: "
                       "backend\\venv\\Scripts\\python -m pip install playwright")
    try:
        pw = _ensure_pw()
        path = str(pw.chromium.executable_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"playwright probe failed: {_describe(exc)}"
    if Path(path).exists():
        return True, ("Chromium ready — sessions use a per-bot saved profile "
                      f"({path})")
    return False, ("Chromium binary missing — run: "
                   "backend\\venv\\Scripts\\python -m playwright install chromium")


def probe() -> Tuple[bool, str]:
    """Is real browser automation possible right now? Cached for 60s.

    Runs on the browser worker thread (the sync driver must never be started
    inside the asyncio loop thread) and reuses the one shared driver."""
    now = time.time()
    if _probe_cache["at"] and now - _probe_cache["at"] < 60:
        return bool(_probe_cache["ok"]), str(_probe_cache["detail"])
    try:
        ok, detail = _executor.submit(_probe_sync).result(timeout=60)
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"playwright probe failed: {_describe(exc)}"
    _probe_cache.update({"at": now, "ok": ok, "detail": detail})
    return ok, detail


# ── playwright session plumbing (executor thread only) ───────────────────────
def _ensure_pw():
    global _pw
    if _pw is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
    return _pw


#: Minimal automation mask for sign-in pages (Google rejects browsers that
#: advertise ``navigator.webdriver``). Kept to ONE property on purpose — heavy
#: fingerprint spoofing breaks other things and is not honest behavior.
_STEALTH_INIT = """
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (e) {}
})();
"""


def _launch(bot_id: str) -> Dict[str, Any]:
    pw = _ensure_pw()
    profile = _profile_dir(bot_id)
    profile.mkdir(parents=True, exist_ok=True)
    headless = _headless()
    kwargs: Dict[str, Any] = {
        "headless": headless,
        "viewport": None,
        "args": [
            "--no-first-run",
            "--no-default-browser-check",
            # the classic automation flag providers look for
            "--disable-blink-features=AutomationControlled",
        ],
        # dropping --enable-automation removes the "controlled by automated
        # software" banner + flag; sign-in pages (Google) check both
        "ignore_default_args": ["--enable-automation"],
    }
    context = None
    errors: List[str] = []
    # Real Chrome first (provider sign-in pages trust it far more than the
    # bundled Chromium), bundled Chromium as fallback — same per-bot profile.
    channels = [None] if headless else ["chrome", None]
    used = "chromium"
    for channel in channels:
        try:
            if channel:
                context = pw.chromium.launch_persistent_context(
                    str(profile), channel=channel, **kwargs)
                used = channel
            else:
                context = pw.chromium.launch_persistent_context(
                    str(profile), **kwargs)
                used = "chromium"
            break
        except Exception as exc:  # noqa: BLE001 — try the next channel
            errors.append(f"{channel or 'chromium'}: {_describe(exc)}")
            context = None
    if context is None:
        raise RuntimeError("could not launch a browser — " + "; ".join(errors))
    try:
        context.add_init_script(_STEALTH_INIT)
    except Exception:  # noqa: BLE001 — masking is best-effort, never fatal
        log.warning("sara-bots browser: webdriver mask could not be attached")
    page = context.pages[0] if context.pages else context.new_page()
    log.info("sara-bots browser: launched %s session for %s (%s)",
             "headless" if headless else "windowed", bot_id, used)
    return {"bot_id": bot_id, "context": context, "page": page,
            "channel": used, "created_at": time.time()}


def _dead(sess: Dict[str, Any]) -> bool:
    try:
        page = sess.get("page")
        return page is None or page.is_closed()
    except Exception:  # noqa: BLE001
        return True


def _destroy(bot_id: str) -> None:
    sess = _sessions.pop(bot_id, None)
    if sess is None:
        return
    try:
        sess["context"].close()
    except Exception:  # noqa: BLE001 — best effort
        pass


def _last_url_file(bot_id: str) -> Path:
    return _profile_dir(bot_id) / "_last_url.txt"


def _remember_url(bot_id: str, sess: Dict[str, Any]) -> None:
    """Persist the page the bot is on so a relaunch can re-open it.

    Best-effort only — continuity must never fail an operation."""
    try:
        url = str(sess["page"].url or "")
        if url.startswith("http"):
            _last_url_file(bot_id).write_text(url[:2000], encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _restore_last_url(bot_id: str, sess: Dict[str, Any]) -> str:
    """Re-open the last known page after a cold/relaunched session.

    Returns the restored URL, or "" when nothing could be restored."""
    page = sess["page"]
    try:
        current = str(page.url or "")
        if current not in ("", "about:blank"):
            return current
        f = _last_url_file(bot_id)
        if not f.exists():
            return ""
        url = f.read_text(encoding="utf-8").strip()
        if not url.startswith("http"):
            return ""
        page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(400)
        return str(page.url or url)
    except Exception:  # noqa: BLE001 — worst case the page stays blank
        return ""


def _run_with_session(bot_id: str, fn: Callable[[Dict[str, Any], Dict[str, Any]],
                                               Dict[str, Any]],
                      params: Dict[str, Any], op: str = "") -> Dict[str, Any]:
    """Run an op against the bot's session; relaunch once if it died.

    A window the user closed (or a service restart) is NOT a failure: the same
    persistent profile is relaunched (logins intact), the page the bot was
    last on is re-opened from the saved profile, and the output says so."""
    sess = _sessions.get(bot_id)
    was_running = sess is not None
    fresh = False
    if sess is not None and _dead(sess):
        _destroy(bot_id)
        sess = None
        fresh = True
    if sess is None:
        sess = _launch(bot_id)
        _sessions[bot_id] = sess
    cold = not was_running
    restored = ""
    if (fresh or cold) and op != "open":
        # browser.open navigates anyway — no point restoring first
        restored = _restore_last_url(bot_id, sess)
    try:
        result = fn(sess, params)
    except Exception as exc:  # noqa: BLE001 — retry once on a closed session
        if not _closed_err(exc):
            raise
        _destroy(bot_id)
        sess = _launch(bot_id)
        _sessions[bot_id] = sess
        fresh = True
        restored = _restore_last_url(bot_id, sess) if op != "open" else ""
        result = fn(sess, params)
    _remember_url(bot_id, sess)
    if fresh or (cold and restored):
        result["output"] = (result.get("output") or "") + (
            "\n(Note: this bot's browser session had ended (closed window or "
            "service restart) — it was relaunched from the same saved profile, "
            "so logins and cookies are intact."
            + (f" The last page ({restored}) was re-opened automatically.)"
               if restored else
               " The page itself starts blank in this session.)"))
    return result


# ── page helpers ─────────────────────────────────────────────────────────────
def _visible_text(page) -> str:
    try:
        text = page.evaluate(
            "() => document.body ? document.body.innerText : ''") or ""
    except Exception:  # noqa: BLE001
        try:
            text = page.inner_text("body")
        except Exception:  # noqa: BLE001
            text = ""
    return re.sub(r"\n{3,}", "\n\n", str(text).strip())


_SELECTOR_HINT = re.compile(r"[#\[\]>]|^\.|^input\b|^textarea\b|^button\b")


def _looks_like_selector(text: str) -> bool:
    return bool(_SELECTOR_HINT.search(text))


def _first(page, getters):
    for getter in getters:
        try:
            loc = getter().first
            if loc.count() > 0:
                return loc
        except Exception:  # noqa: BLE001 — try the next strategy
            continue
    return None


def _locate(page, selector: str, target: str, for_typing: bool = False):
    if selector:
        loc = page.locator(selector).first
        if loc.count() == 0:
            raise RuntimeError(f"no element matched CSS selector '{selector}'")
        return loc
    label = (target or "").strip()
    if label:
        getters = []
        if _looks_like_selector(label):
            getters.append(lambda: page.locator(label))
        if for_typing:
            getters += [
                lambda: page.get_by_label(label, exact=False),
                lambda: page.get_by_placeholder(label, exact=False),
                lambda: page.get_by_role("searchbox", name=label,
                                         exact=False),
                lambda: page.get_by_role("textbox", name=label, exact=False),
                lambda: page.get_by_role("combobox", name=label, exact=False),
            ]
        else:
            getters += [
                lambda: page.get_by_label(label, exact=False),
                lambda: page.get_by_placeholder(label, exact=False),
                lambda: page.get_by_role("button", name=label, exact=False),
                lambda: page.get_by_role("link", name=label, exact=False),
                lambda: page.get_by_text(label, exact=False),
            ]
        loc = _first(page, getters)
        if loc is not None:
            return loc
    elif not for_typing:
        raise RuntimeError("no target given — pass 'selector' (CSS) or "
                           "'target'/'text' (visible label)")
    if for_typing:
        # last resort for typing: the first visible search/text box
        loc = _first(page, [lambda: page.locator(
            'input[type="search"]:visible, input[type="text"]:visible, '
            'input:not([type]):visible, textarea:visible')])
        if loc is not None:
            return loc
    if not label:
        raise RuntimeError("no target given — pass 'selector' (CSS) or "
                           "'target'/'text' (visible label)")
    raise RuntimeError(f"no element on the page matched {label!r} "
                       "(tried CSS selector, label, placeholder, roles, "
                       "visible text)")


def _adopt_new_tab(sess: Dict[str, Any], before_count: int) -> bool:
    """Follow a popup/new tab opened by the last action (Gmail does this)."""
    try:
        pages = [p for p in sess["context"].pages if not p.is_closed()]
    except Exception:  # noqa: BLE001
        return False
    if len(pages) > before_count and pages:
        new_page = pages[-1]
        changed = sess.get("page") is not new_page
        sess["page"] = new_page
        return changed
    return False


def _page_report(sess: Dict[str, Any], lead: str) -> Dict[str, Any]:
    page = sess["page"]
    try:
        title = page.title()
    except Exception:  # noqa: BLE001
        title = ""
    text = _visible_text(page)[:MAX_PAGE_TEXT]
    return {"ok": True, "error": "",
            "output": (f"{lead}\nNow at: {page.url}\nTitle: {title}\n"
                       f"--- visible text (truncated) ---\n{text}")}


# ── operations ───────────────────────────────────────────────────────────────
def _op_open(sess: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    url = str(params.get("url") or "").strip()
    page = sess["page"]
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    page.wait_for_timeout(600)
    return _page_report(sess, f"Opened {url}")


def _op_read(sess: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    return _page_report(sess, "Current page")


def _op_click(sess: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    page, ctx = sess["page"], sess["context"]
    selector = str(params.get("selector") or "").strip()
    target = str(params.get("text") or "").strip()
    before = len(ctx.pages)
    if selector:
        page.click(selector, timeout=OP_TIMEOUT_MS)
        used = selector
    else:
        _locate(page, "", target).click(timeout=OP_TIMEOUT_MS)
        used = f"{target!r}"
    page.wait_for_timeout(900)
    switched = _adopt_new_tab(sess, before)
    report = _page_report(sess, f"Clicked {used}."
                          + (" A new tab opened — the session followed it."
                             if switched else ""))
    report["output"] = report["output"][:3500]
    return report


def _type_like(loc, content: str) -> None:
    fn = getattr(loc, "press_sequentially", None)
    if fn is not None:
        fn(content, delay=30, timeout=OP_TIMEOUT_MS)
    else:  # older playwright
        loc.type(content, delay=30, timeout=OP_TIMEOUT_MS)


def _op_type(sess: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    page, ctx = sess["page"], sess["context"]
    selector = str(params.get("selector") or "").strip()
    target = str(params.get("target") or "").strip()
    content = str(params.get("text") or "")
    loc = _locate(page, selector, target, for_typing=True)
    if params.get("clear_first", True):
        loc.fill(content, timeout=OP_TIMEOUT_MS)
    else:
        _type_like(loc, content)
    label = selector or f"{target!r}"
    lead = f"Typed {len(content)} character(s) into {label}."
    if params.get("submit"):
        before = len(ctx.pages)
        loc.press("Enter")
        page.wait_for_timeout(1200)
        switched = _adopt_new_tab(sess, before)
        lead += " Pressed Enter." + (" A new tab opened — the session followed it."
                                     if switched else "")
    report = _page_report(sess, lead)
    report["output"] = report["output"][:3500]
    return report


def _op_press(sess: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    key = str(params.get("key") or "Enter").strip() or "Enter"
    selector = str(params.get("selector") or "").strip()
    target = str(params.get("target") or "").strip()
    page = sess["page"]
    if selector or target:
        loc = _locate(page, selector, target)
        loc.press(key, timeout=OP_TIMEOUT_MS)
        lead = f"Pressed {key} in {selector or f'{target!r}'}."
    else:
        page.keyboard.press(key)
        lead = f"Pressed {key}."
    page.wait_for_timeout(700)
    return _page_report(sess, lead)


def _op_scroll(sess: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    page = sess["page"]
    direction = str(params.get("direction") or "down").strip().lower()
    try:
        amount = max(50, min(int(params.get("amount") or 600), 5000))
    except (TypeError, ValueError):
        amount = 600
    dy = -amount if direction == "up" else amount
    page.evaluate("(dy) => window.scrollBy(0, dy)", dy)
    page.wait_for_timeout(400)
    return _page_report(sess, f"Scrolled {direction} {abs(dy)}px.")


def _op_screenshot(sess: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    page = sess["page"]
    data = page.screenshot(full_page=False)
    b64 = base64.b64encode(data).decode("ascii")
    path = _SHOTS / _safe(sess["bot_id"]) / f"{int(time.time())}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"ok": True, "error": "",
            "output": f"Saved a screenshot of the current view "
                      f"({len(data)} bytes) to {path}",
            "png_b64": b64 if len(b64) < MAX_EMBED_PNG_B64 else ""}


_OPS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = {
    "open": _op_open,
    "read": _op_read,
    "click": _op_click,
    "type": _op_type,
    "press": _op_press,
    "scroll": _op_scroll,
    "screenshot": _op_screenshot,
}


# ── validation (no browser needed — safe to unit-test) ───────────────────────
def _validate(op: str, params: Dict[str, Any]) -> Optional[str]:
    if op == "open":
        if not _URL_RE.match(str(params.get("url") or "").strip()):
            return "browser.open needs an http(s) URL"
    elif op == "click":
        if not (str(params.get("selector") or "").strip()
                or str(params.get("text") or "").strip()):
            return "browser.click needs 'text' (visible label) or 'selector' (CSS)"
    elif op == "type":
        if not str(params.get("text") or ""):
            return "browser.type needs 'text' (the content to type)"
        if not (str(params.get("selector") or "").strip()
                or str(params.get("target") or "").strip()):
            return "browser.type needs 'target' (field label) or 'selector' (CSS)"
    return None


# ── synchronous entry points (executor thread) ───────────────────────────────
def _run_sync(bot_id: str, op: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if op == "close":
        sess = _sessions.get(bot_id)
        if sess is None or _dead(sess):
            _destroy(bot_id)
            return {"ok": True, "error": "", "output":
                    "No live browser session for this bot — nothing to close."}
        _destroy(bot_id)
        return {"ok": True, "error": "", "output":
                "Browser session closed. The saved profile (logins) stays on "
                "disk and is reused by the next run."}
    fn = _OPS.get(op)
    if fn is None:
        return {"ok": False, "output": "",
                "error": f"unknown browser operation '{op}'"}
    try:
        return _run_with_session(bot_id, fn, params, op)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "output": "", "error": _describe(exc)}


def _status_sync(bot_id: str) -> Dict[str, Any]:
    profile = str(_profile_dir(bot_id))
    sess = _sessions.get(bot_id)
    if sess is None:
        return {"alive": False, "url": None, "title": None,
                "profile": profile, "created_at": None}
    if _dead(sess):
        _destroy(bot_id)
        return {"alive": False, "url": None, "title": None,
                "profile": profile, "created_at": None}
    page = sess["page"]
    try:
        return {"alive": True, "url": page.url, "title": page.title(),
                "profile": profile, "created_at": sess.get("created_at")}
    except Exception:  # noqa: BLE001
        _destroy(bot_id)
        return {"alive": False, "url": None, "title": None,
                "profile": profile, "created_at": None}


def _shot_sync(bot_id: str) -> Dict[str, Any]:
    sess = _sessions.get(bot_id)
    if sess is None or _dead(sess):
        return {"shot_error": "no live session"}
    try:
        data = sess["page"].screenshot(full_page=False)
    except Exception as exc:  # noqa: BLE001
        return {"shot_error": _describe(exc)}
    b64 = base64.b64encode(data).decode("ascii")
    if len(b64) > MAX_EMBED_PNG_B64:
        return {"shot_error": "screenshot too large to embed"}
    return {"png_b64": b64, "shot_at": time.time()}


def _close_sync(bot_id: str) -> bool:
    had = bot_id in _sessions
    _destroy(bot_id)
    return had


# ── async API (used by the worker engine and the HTTP layer) ─────────────────
async def run_op(bot_id: str, op: str,
                 params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Execute one browser operation for this bot. Never raises."""
    params = params or {}
    problem = _validate(op, params)
    if problem:
        return {"ok": False, "output": "", "error": problem}
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, _run_sync, bot_id, op, params)
    except Exception as exc:  # noqa: BLE001 — a dead driver must not crash tasks
        return {"ok": False, "output": "",
                "error": f"browser engine error: {_describe(exc)}"}


async def session_info(bot_id: str, with_shot: bool = False) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(_executor, _status_sync, bot_id)
    if with_shot and info.get("alive"):
        shot = await loop.run_in_executor(_executor, _shot_sync, bot_id)
        info.update(shot)
    info["mode"] = "headless" if _headless() else "windowed"
    return info


async def close_session(bot_id: str) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    had = await loop.run_in_executor(_executor, _close_sync, bot_id)
    return {"closed": had,
            "detail": ("session closed — the saved profile (logins) stays on "
                       "disk" if had else "no live session for this bot")}


# ── shutdown ─────────────────────────────────────────────────────────────────
def _shutdown_sync() -> None:
    for bot_id in list(_sessions):
        _destroy(bot_id)
    global _pw
    if _pw is not None:
        try:
            _pw.stop()
        except Exception:  # noqa: BLE001
            pass
        _pw = None


def _shutdown_best_effort() -> None:
    try:
        _executor.submit(_shutdown_sync)
    except Exception:  # noqa: BLE001
        pass


atexit.register(_shutdown_best_effort)
