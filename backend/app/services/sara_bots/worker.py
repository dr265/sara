"""Sara Bots headless action worker — runs ONE approved action, then exits.

Launched by runner.py as::

    python -I worker.py --root <approved-actions-dir> --tool <tool_name>

with a JSON object on stdin (the action's ``parameters``) and a strict JSON
reply on stdout::

    {"ok": true,  "output": "<handler return value>"}
    {"ok": false, "error": "<sanitized reason>"}

Isolation properties (within a same-user Windows process model — see the
delivery report for the documented limits):
  * ``-I`` strips user site-packages and ambient PYTHONPATH; the interpreter
    is the sara_desktop venv when present, so the action's real dependencies
    resolve exactly as they do inside the desktop app;
  * only the ONE requested module is imported, selected by a validated name
    (regex + the file must live inside the approved root — verified again
    here, defense in depth against a forged command line);
  * handlers that declare live-session context (player/speak/response/
    session_memory) without defaults are refused instead of running half-dead;
  * nothing from this process can reach the backend's memory or registry.
"""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import os
import re
import sys
import traceback
from pathlib import Path

_SAFE_TOOL_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
_CTX_KEYS = ("player", "speak", "response", "session_memory")
_MAX_STDIN_BYTES = 256 * 1024


def _fail(message: str) -> None:
    sys.stdout.write(json.dumps({"ok": False, "error": message[:2000]}))
    sys.stdout.flush()
    sys.exit(0)            # protocol failure, not a crash: parent reads JSON


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--tool", required=True)
    args = parser.parse_args()

    if not _SAFE_TOOL_RE.match(args.tool):
        _fail(f"invalid tool name '{args.tool}'")
        return

    root = Path(os.path.realpath(args.root))
    path = root / f"{args.tool}.py"
    candidate = Path(os.path.realpath(path))
    try:
        candidate.relative_to(root)
    except ValueError:
        _fail("tool file resolves outside the approved root")
        return
    if not candidate.is_file():
        _fail(f"tool file not found: {args.tool}.py")
        return

    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(raw) > _MAX_STDIN_BYTES:
        _fail("input exceeds the 256 KiB limit")
        return
    try:
        parameters = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"input is not valid JSON: {exc}")
        return
    if not isinstance(parameters, dict):
        _fail("input must be a JSON object of named parameters")
        return

    # Actions may import sibling packages (core, memory) from the desktop app
    # tree — the parent of the actions dir. Adding it keeps imports honest.
    app_root = root.parent
    if app_root.is_dir():
        sys.path.insert(0, str(app_root))

    try:
        spec = importlib.util.spec_from_file_location(f"sb_action_{args.tool}", candidate)
        if spec is None or spec.loader is None:
            raise ImportError("could not build import spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — reported, never crashes the parent
        _fail(f"import failed: {type(exc).__name__}: {exc}")
        return

    tool = getattr(module, "TOOL", None)
    if not isinstance(tool, dict):
        _fail("module has no TOOL dict — not an approved action")
        return
    handler = tool.get("handler")
    if not callable(handler):
        _fail("TOOL['handler'] is not callable")
        return

    # Only headless-safe signatures run: context kwargs must carry defaults.
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        _fail("handler signature could not be inspected")
        return
    kwargs: dict = {}
    for key in _CTX_KEYS:
        if key in sig.parameters:
            param = sig.parameters[key]
            if param.default is inspect.Parameter.empty:
                _fail(f"handler requires live desktop context '{key}' — "
                      "run it from the sara_desktop session instead")
                return
            kwargs[key] = None

    try:
        result = handler(parameters=parameters, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the failure IS the output
        sys.stdout.write(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback_tail": traceback.format_exc()[-1500:],
        }))
        sys.stdout.flush()
        return

    output = result if isinstance(result, str) else json.dumps(
        result, ensure_ascii=False, default=str)
    sys.stdout.write(json.dumps({"ok": True, "output": output}, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
