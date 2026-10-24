"""Application adapters: turning a raw window into meaningful context.

A window title is a string the operating system happens to agree to give us.
``"Saiyaara - YouTube - Google Chrome"`` means a browser, a tab called
"Saiyaara", a site called YouTube. ``"app.ts - my-project - Visual Studio
Code"`` means an editor, a file, a project. ``"Downloads"`` means a folder.

Hard-coding those three shapes into Ultron Core is how you end up with a system
that works on YouTube and mysteriously fails on a PDF reader. So the knowledge
lives here, in small adapters, and Core never learns what a browser is.

The contract is deliberately tiny:

* an adapter *claims* a window by ``process name`` (exact, then substring),
* it turns that window into an :class:`AdapterContext` - a category plus a few
  typed facts,
* it declares a ``confidence``.

That is all. Adding support for a new application is one class and one
``register()`` call; no core file is touched. That property is locked by
``test_registry_extends_without_core_changes``.

Where a fact cannot be read from the title alone, the adapter says so by
leaving it ``None`` - it never invents a URL. A missing URL is useful
("I know it is Chrome but not what page") in a way that a made-up one is not.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("sara.ultron.adapters")

#: Categories an adapter may report. Extensible on purpose: the HUD renders
#: unknown categories gracefully rather than dropping them.
CAT_BROWSER = "browser"
CAT_EDITOR = "editor"
CAT_FILE_MANAGER = "file_manager"
CAT_TERMINAL = "terminal"
CAT_DOCUMENT = "document"
CAT_MEDIA = "media"
CAT_SETTINGS = "settings"
CAT_CHAT = "chat"
CAT_UNKNOWN = "unknown"


@dataclass
class RawWindow:
    """What the OS told us, before interpretation."""

    title: str = ""
    process: str = ""
    pid: int = 0
    hwnd: int = 0
    #: Filled in by adapters that can do better than the title (e.g. a browser
    #: extension or UI Automation reading the real address bar).
    url: Optional[str] = None
    tab_title: Optional[str] = None

    @property
    def identity(self) -> str:
        """What counts as "the same window" for change detection."""
        return f"{self.process}|{self.title}|{self.url or ''}"


@dataclass
class AdapterContext:
    """A window, interpreted."""

    category: str = CAT_UNKNOWN
    app_name: str = ""
    #: Tab title, document name, open file, folder - whatever this app calls it.
    item: Optional[str] = None
    url: Optional[str] = None
    site: Optional[str] = None
    project: Optional[str] = None
    folder: Optional[str] = None
    #: 0..1. Below ~0.4 the HUD shows the raw title instead of trusting this.
    confidence: float = 0.0
    #: What the adapter could not determine, so the UI can be honest about it.
    unavailable: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "app_name": self.app_name,
            "item": self.item,
            "url": self.url,
            "site": self.site,
            "project": self.project,
            "folder": self.folder,
            "confidence": round(self.confidence, 2),
            "unavailable": list(self.unavailable),
            "extra": dict(self.extra),
        }


class AppAdapter:
    """Base class. Subclasses override :meth:`match` and :meth:`interpret`."""

    #: Executable names, lower-cased, without ".exe".
    processes: Tuple[str, ...] = ()
    #: Human name shown in the HUD.
    label: str = ""
    category: str = CAT_UNKNOWN

    def match(self, window: RawWindow) -> bool:
        process = (window.process or "").lower()
        if not process:
            # Last resort: some window managers give no process name at all.
            return bool(self.label) and self.label.lower() in (window.title or "").lower()
        return any(name == process or name in process for name in self.processes)

    def interpret(self, window: RawWindow) -> AdapterContext:  # pragma: no cover
        raise NotImplementedError

    # -- helpers available to every adapter ----------------------------
    @staticmethod
    def _split_title(title: str, marker: str) -> List[str]:
        """Split a window title on its trailing application marker.

        ``"a - b - Visual Studio Code"`` with marker ``"Visual Studio Code"``
        -> ``["a - b", "Visual Studio Code"]``. Only the *last* occurrence
        counts: a document called "notes - Chrome" inside VS Code must not be
        split at the wrong place.
        """
        text = (title or "").strip()
        index = text.lower().rfind(marker.lower())
        if index <= 0:
            return [text]
        return [text[:index].strip().rstrip("-–— ").strip(), text[index:].strip()]

    @staticmethod
    def _site_from_url(url: str) -> Optional[str]:
        match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:?#]+)", url or "")
        return match.group(1).lower() if match else None

    def _no_url(self, ctx: AdapterContext) -> AdapterContext:
        ctx.unavailable.append("url")
        return ctx


class BrowserAdapter(AppAdapter):
    """Chromium and Firefox-family browsers.

    Browsers put the tab title and the site name in the window title, so most
    of this is parsing rather than guessing. The *real* URL is not in the
    title - it comes from UI Automation or an extension, and until one of those
    is wired up this adapter reports ``url: None`` rather than assembling a
    plausible-looking ``https://youtube.com``.
    """

    processes = ("chrome", "msedge", "edge", "firefox", "brave", "opera", "vivaldi")
    category = CAT_BROWSER

    def match(self, window: RawWindow) -> bool:
        process = (window.process or "").lower()
        if any(name in process for name in self.processes):
            return True
        # Firefox and some Chromium builds put the browser in the title even
        # when the process name is unhelpful.
        return bool(re.search(r"\b(Google Chrome|Mozilla Firefox|Microsoft Edge|Brave)\b",
                              window.title or "", re.I))

    def interpret(self, window: RawWindow) -> AdapterContext:
        title = (window.title or "").strip()
        ctx = AdapterContext(
            category=CAT_BROWSER,
            app_name=self._browser_name(title),
            confidence=0.55,
        )

        # "Tab title - Site - Browser" is the Chromium convention.
        for marker in (" - Google Chrome", " - Chromium", " - Microsoft Edge",
                       " - Brave", " - Opera", " - Vivaldi", " - Mozilla Firefox"):
            if title.lower().endswith(marker.lower()):
                ctx.app_name = marker.strip(" -")
                body = title[: -len(marker)].strip()
                parts = [p.strip() for p in body.split(" - ") if p.strip()]
                ctx.tab_title = parts[0] if parts else None
                if len(parts) >= 2:
                    ctx.item = parts[0]
                    ctx.site = parts[-1]
                    ctx.confidence = 0.8
                elif parts:
                    ctx.item = parts[0]
                    ctx.site = None
                    ctx.confidence = 0.6
                break
        else:
            # Firefox: "Tab title — Mozilla Firefox" (em dash, no site segment).
            ctx.app_name = self._browser_name(title) or "Browser"
            ctx.tab_title = title or None
            ctx.item = ctx.tab_title

        # A real URL, when an observer managed to read the address bar, always
        # wins over title parsing.
        if window.url:
            ctx.url = window.url
            ctx.site = self._site_from_url(window.url) or ctx.site
            ctx.confidence = min(0.95, ctx.confidence + 0.15)
        else:
            self._no_url(ctx)
        if not ctx.site:
            ctx.unavailable.append("site")
        return ctx

    @staticmethod
    def _browser_name(title: str) -> str:
        for name in ("Google Chrome", "Microsoft Edge", "Mozilla Firefox",
                     "Brave", "Opera", "Vivaldi", "Chromium"):
            if name.lower() in (title or "").lower():
                return name
        return "Browser"


class EditorAdapter(AppAdapter):
    """VS Code and the Visual Studio family: file, then project, then editor."""

    processes = ("code", "devenv", "code - insiders")
    label = "Visual Studio Code"
    category = CAT_EDITOR

    def match(self, window: RawWindow) -> bool:
        process = (window.process or "").lower()
        return "code" in process or "devenv" in process or \
            bool(re.search(r"Visual Studio( Code)?$", window.title or "", re.I))

    def interpret(self, window: RawWindow) -> AdapterContext:
        title = (window.title or "").strip()
        ctx = AdapterContext(
            category=CAT_EDITOR,
            app_name="Visual Studio Code" if "devenv" not in (window.process or "").lower()
            else "Visual Studio",
            confidence=0.5,
        )
        for marker in (" - Visual Studio Code", " - Code", " - Visual Studio"):
            if title.lower().endswith(marker.lower()):
                body = title[: -len(marker)].strip()
                parts = [p.strip() for p in body.split(" - ") if p.strip()]
                if parts:
                    ctx.item = parts[0]
                    ctx.confidence = 0.85
                if len(parts) >= 2:
                    # "file - folder - editor": the project is the LAST segment,
                    # not the second-to-last (which is the file itself).
                    ctx.project = parts[-1]
                    ctx.folder = parts[-1]
                elif len(parts) == 1 and "/" in parts[0]:
                    ctx.project = parts[0].rsplit("/", 1)[0].rsplit("\\", 1)[0] or None
                break
        else:
            ctx.item = title or None
        ctx.unavailable.extend(["url", "site"])
        return ctx


class ExplorerAdapter(AppAdapter):
    """File Explorer. The title is the folder; the folder is the whole context."""

    processes = ("explorer",)
    label = "File Explorer"
    category = CAT_FILE_MANAGER

    def interpret(self, window: RawWindow) -> AdapterContext:
        title = (window.title or "").strip()
        # File Explorer titles are either the folder name or the file name.
        ctx = AdapterContext(
            category=CAT_FILE_MANAGER,
            app_name="File Explorer",
            item=title or None,
            folder=title or None,
            confidence=0.8,
        )
        ctx.unavailable.extend(["url", "site", "project"])
        return ctx


class TerminalAdapter(AppAdapter):
    """Windows Terminal, PowerShell, cmd, and the common Unix shells."""

    processes = ("windowsterminal", "powershell", "pwsh", "cmd", "bash",
                 "wt", "conhost")
    category = CAT_TERMINAL

    def interpret(self, window: RawWindow) -> AdapterContext:
        title = (window.title or "").strip()
        # Strip the shell suffix first: "C:\Users\HP\proj - Windows Terminal".
        # Splitting on the colon *before* stripping would cut at "C:" and leave
        # a mangled path as the folder.
        body = title
        for marker in (" - Windows Terminal", " - PowerShell", " - Command Prompt",
                       " - cmd", " - bash", " - zsh", " - wsl"):
            if body.lower().endswith(marker.lower()):
                body = body[: -len(marker)].strip()
                break
        # Windows Terminal shows the running program, then the cwd:
        # "pwsh - C:\Users\HP\proj".
        if " - " in body:
            head, _, tail = body.rpartition(" - ")
            item = tail.strip() if (":" in tail or "/" in tail or "\\" in tail) else body
        else:
            item = body
        ctx = AdapterContext(
            category=CAT_TERMINAL,
            app_name=self._shell_name(title),
            item=item or None,
            folder=item if item and any(s in item for s in (":", "/", "\\")) else None,
            confidence=0.75,
        )
        ctx.unavailable.extend(["url", "site", "project"])
        return ctx

    @staticmethod
    def _shell_name(title: str) -> str:
        tail = (title or "").lower()
        for marker, name in (("windows terminal", "Windows Terminal"),
                             ("powershell", "PowerShell"), ("cmd", "Command Prompt"),
                             ("bash", "bash"), ("zsh", "zsh"), ("wsl", "WSL")):
            if marker in tail:
                return name
        return "Terminal"


class DocumentAdapter(AppAdapter):
    """PDF and office viewers: the document name is the whole context."""

    processes = ("acrobat", "acrobat64", "sumatrapdf", "winword", "excel",
                 "powerpnt", "notepad", "wordpad")
    category = CAT_DOCUMENT

    def interpret(self, window: RawWindow) -> AdapterContext:
        title = (window.title or "").strip()
        name, _, tail = title.rpartition(" - ")
        ctx = AdapterContext(
            category=CAT_DOCUMENT,
            app_name=self._app_name(title),
            item=(name or title) or None,
            folder=tail or None,
            confidence=0.75 if name else 0.55,
        )
        ctx.unavailable.extend(["url", "site", "project"])
        return ctx

    @staticmethod
    def _app_name(title: str) -> str:
        tail = (title or "").rsplit(" - ", 1)[-1].lower()
        for process, name in (("acrobat", "Adobe Acrobat"), ("sumatra", "SumatraPDF"),
                              ("winword", "Word"), ("excel", "Excel"),
                              ("powerpnt", "PowerPoint"), ("notepad", "Notepad")):
            if process in tail:
                return name
        return tail.title() or "Document"


class MediaAdapter(AppAdapter):
    """Media players. Title parsing only; playback state needs a real API."""

    processes = ("vlc", "mpv", "spotify", "wmplayer", "music", "itunes")
    category = CAT_MEDIA

    def interpret(self, window: RawWindow) -> AdapterContext:
        title = (window.title or "").strip()
        parts = [p.strip() for p in title.split(" - ") if p.strip()]
        ctx = AdapterContext(
            category=CAT_MEDIA,
            app_name=self._app_name(title),
            item=parts[0] if parts else None,
            confidence=0.7 if parts else 0.4,
        )
        # "is it playing?" is not in the title - say so rather than assume.
        ctx.unavailable.extend(["playing", "position", "url", "site"])
        if len(parts) >= 2:
            ctx.extra["track_guess"] = parts[0]
            ctx.extra["source_guess"] = parts[-1]
        return ctx

    @staticmethod
    def _app_name(title: str) -> str:
        tail = (title or "").lower()
        for process, name in (("vlc", "VLC"), ("mpv", "mpv"), ("spotify", "Spotify"),
                              ("wmplayer", "Media Player")):
            if process in tail:
                return name
        return "Media Player"


class GenericAdapter(AppAdapter):
    """The fallback. Never claims anything it cannot see.

    Registered last, so it always runs only when nothing else matched.
    """

    category = CAT_UNKNOWN

    def match(self, window: RawWindow) -> bool:
        return True

    def interpret(self, window: RawWindow) -> AdapterContext:
        title = (window.title or "").strip()
        return AdapterContext(
            category=CAT_UNKNOWN,
            app_name=self._pretty_process(window.process) or "Unknown application",
            item=title or None,
            confidence=0.3,
            unavailable=["url", "site", "project", "folder"],
        )

    @staticmethod
    def _pretty_process(process: str) -> str:
        name = (process or "").replace(".exe", "").strip()
        return name[:1].upper() + name[1:] if name else ""


class AdapterRegistry:
    """Ordered list of adapters; first match wins."""

    def __init__(self) -> None:
        self._adapters: List[AppAdapter] = []

    def register(self, adapter: AppAdapter) -> "AdapterRegistry":
        """Add an adapter, ahead of the generic fallback.

        Inserting rather than appending is the whole point: ``GenericAdapter``
        matches everything, so an adapter registered after it would never run
        and the caller would see their new application quietly resolve to
        "Unknown application".

        Returns the registry, not the adapter, so a caller can build one in a
        single expression (``AdapterRegistry().register(MyAdapter())``).
        Returning the adapter here is the kind of mistake that reads fine and
        then blows up three lines later on an unrelated-looking call.
        """
        fallback = [a for a in self._adapters if isinstance(a, GenericAdapter)]
        for existing in fallback:
            self._adapters.remove(existing)
        self._adapters.extend(fallback)
        self._adapters.insert(max(0, len(self._adapters) - len(fallback)), adapter)
        return self

    def unregister(self, label_or_type: type) -> None:
        self._adapters = [a for a in self._adapters if not isinstance(a, label_or_type)]

    @property
    def adapters(self) -> List[AppAdapter]:
        return list(self._adapters)

    def names(self) -> List[str]:
        out = []
        for adapter in self._adapters:
            label = adapter.label or adapter.__class__.__name__
            processes = getattr(adapter, "processes", ())
            out.append(f"{label} [{', '.join(processes) or 'fallback'}]")
        return out

    def interpret(self, window: RawWindow) -> Tuple[AdapterContext, str]:
        """Return ``(context, adapter_name)`` for one window.

        Order matters: the generic adapter matches everything, so it must stay
        last. :meth:`register` enforces that for adapters added later.
        """
        for adapter in self._adapters:
            try:
                if adapter.match(window):
                    context, name = adapter.interpret(window), (
                        adapter.label or adapter.__class__.__name__
                    )
                    return self._annotate(context, window), name
            except Exception as exc:  # noqa: BLE001 - a broken adapter must not
                # take awareness down with it; fall through to the next one.
                logger.warning("Adapter %s failed: %s: %s",
                               type(adapter).__name__, type(exc).__name__, exc)
                continue
        fallback = GenericAdapter().interpret(window)
        return self._annotate(fallback, window), "GenericAdapter"

    @staticmethod
    def _annotate(ctx: AdapterContext, window: RawWindow) -> AdapterContext:
        """Attach facts that are true of every adapter, not just the fallback.

        Without this, "the OS would not name the owning process" is only
        reported when nothing matched - so an unnamed Chrome window would be
        reported as a perfectly good browser context with no hint that the
        process was never actually read.
        """
        if not window.process:
            # Typically a higher-integrity (elevated) window that a normal
            # process cannot open. A real limit of the current privilege
            # level, so it is stated rather than papered over.
            if "process_name" not in ctx.unavailable:
                ctx.unavailable.append("process_name")
            ctx.extra["process_unreadable"] = True
        return ctx

    def register_default_set(self) -> "AdapterRegistry":
        """The shipped adapters, generic last."""
        self.register(BrowserAdapter())
        self.register(EditorAdapter())
        self.register(ExplorerAdapter())
        self.register(TerminalAdapter())
        self.register(DocumentAdapter())
        self.register(MediaAdapter())
        self.register(GenericAdapter())
        return self


#: Process-wide registry. Extending it is a one-liner from anywhere:
#:     registry.register(MyAppAdapter())
#: with no change to Ultron Core.
registry = AdapterRegistry().register_default_set()
