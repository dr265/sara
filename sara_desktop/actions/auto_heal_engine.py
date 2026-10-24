# actions/auto_heal_engine.py
"""
Autonomous Self-Healing Engine for SARA.
Ported from Brahma-Echo (actions/auto_heal_engine.py), adapted to SARA's
LLM stack and action contract.

Pipeline: parse a traceback -> pinpoint the first-party file/line ->
LLM synthesizes a surgical patch -> AST validation in memory -> atomic
backup -> apply -> py_compile verification (auto-rollback on failure).

Deliberate differences from Brahma's version:
  * Healing only ever runs when invoked through this tool — no boot-time
    auto-patching and no background self-modification.
  * Patch synthesis uses SARA's own llm_client first, Gemini second.
  * Every patch is reversible with the 'rollback' action (backup kept in
    patch_backups/).
"""
from __future__ import annotations

import ast
import json
import logging
import py_compile
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("AutoHeal")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "memory"
BACKUPS_DIR = BASE_DIR / "memory" / "patch_backups"
PATCH_HISTORY_FILE = CONFIG_DIR / "patch_history.json"

# Boot-critical files the engine refuses to touch (protecting its own
# machinery + the loaders that would have to load a broken heal engine).
PROTECTED_CORE_FILES = {
    "auto_heal_engine.py",
    "action_loader.py",
    "plugin_loader.py",
    "installer.py",
    "setup.py",
    "requirements.txt",
    "__init__.py",
}


# ── 1. Traceback Analyzer ───────────────────────────────────────────────────

class TracebackAnalyzer:
    """Parses tracebacks and pinpoints the responsible first-party file/line."""

    @staticmethod
    def parse(tb_text: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "success": False,
            "target_file": None,
            "line_number": None,
            "function_name": None,
            "exception_type": None,
            "exception_message": None,
            "raw_traceback": tb_text,
        }
        if not tb_text:
            return result

        lines = [line.strip() for line in tb_text.strip().splitlines() if line.strip()]
        if lines:
            last_line = lines[-1]
            if ":" in last_line:
                parts = last_line.split(":", 1)
                result["exception_type"] = parts[0].strip()
                result["exception_message"] = parts[1].strip()
            else:
                result["exception_type"] = last_line
                result["exception_message"] = ""

        file_pattern = re.compile(
            r'File\s+["\']([^"\']+\.py)["\'],\s+line\s+(\d+)(?:,\s+in\s+([^\n\r]+))?',
            re.IGNORECASE,
        )
        matches = file_pattern.findall(tb_text)

        for raw_path, line_str, func_name in reversed(matches):
            if any(marker in raw_path.lower() for marker in
                   ("site-packages", "venv", ".venv", "lib\\python", "lib/python")):
                continue

            p = Path(raw_path)
            resolved: Optional[Path] = None
            if (BASE_DIR / p).exists():
                resolved = (BASE_DIR / p).resolve()
            elif p.is_absolute() and p.exists():
                resolved = p
            else:
                candidate = BASE_DIR / p.name
                if candidate.exists():
                    resolved = candidate
                else:
                    for sub in ("actions", "core", "memory", "dashboard"):
                        c2 = BASE_DIR / sub / p.name
                        if c2.exists():
                            resolved = c2
                            break

            if resolved and resolved.exists():
                if resolved.name in PROTECTED_CORE_FILES:
                    logger.warning(f"[AutoHeal] '{resolved.name}' is core-protected.")
                    continue
                result["success"] = True
                result["target_file"] = str(resolved.resolve())
                result["line_number"] = int(line_str)
                result["function_name"] = func_name.strip() if func_name else None
                break

        return result


# ── 2. Safety Sandbox & Rollback Manager ────────────────────────────────────

class SafetySandbox:
    """Atomic backups, AST validation, and instant rollback."""

    @staticmethod
    def create_backup(file_path: Path) -> Path:
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        backup_name = f"{file_path.stem}.bak_{stamp}{file_path.suffix}"
        backup_path = BACKUPS_DIR / backup_name
        shutil.copy2(file_path, backup_path)
        return backup_path

    @staticmethod
    def validate_code(code_str: str, file_name: str = "<staging>") -> Tuple[bool, Optional[str]]:
        try:
            ast.parse(code_str, filename=file_name)
            return True, None
        except SyntaxError as e:
            return False, f"Syntax Error on line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, f"Validation Error: {e}"

    @staticmethod
    def rollback_patch(patch_id: str) -> Dict[str, Any]:
        history = SafetySandbox._load_history()
        for entry in reversed(history):
            if entry.get("patch_id") == patch_id or patch_id == "latest":
                if entry.get("status") != "applied":
                    continue
                target = Path(entry.get("target_file", ""))
                backup = Path(entry.get("backup_path", ""))
                if not backup.exists() or not target.exists():
                    return {"success": False, "message": f"Backup file '{backup}' missing."}
                try:
                    shutil.copy2(backup, target)
                    entry["status"] = "rolled_back"
                    entry["rolled_back_at"] = time.time()
                    SafetySandbox._save_history(history)
                    return {"success": True,
                            "message": f"Rolled back patch {entry.get('patch_id')} on '{target.name}'.",
                            "target_file": str(target)}
                except Exception as e:
                    return {"success": False, "message": f"Rollback failed: {e}"}
        return {"success": False, "message": f"No applied patch matching '{patch_id}' to roll back."}

    @staticmethod
    def _load_history() -> List[Dict[str, Any]]:
        if not PATCH_HISTORY_FILE.exists():
            return []
        try:
            with open(PATCH_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    @staticmethod
    def _save_history(history: List[Dict[str, Any]]) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(PATCH_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=4)
        except Exception as e:
            logger.error(f"[AutoHeal] Failed to save patch history: {e}")


# ── 3. Patch Synthesizer & Auto-Heal Controller ─────────────────────────────

def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    return t


class AutoHealEngine:
    """Error analysis, hotfix synthesis, verification, and application."""

    _last_error: Optional[str] = None

    @classmethod
    def record_last_error(cls, tb_str: str) -> None:
        cls._last_error = tb_str

    @classmethod
    def get_last_error(cls) -> Optional[str]:
        return cls._last_error

    @classmethod
    def get_patch_history(cls, limit: int = 5) -> List[Dict[str, Any]]:
        return list(reversed(SafetySandbox._load_history()))[:limit]

    @classmethod
    def heal_traceback(
        cls,
        traceback_text: str,
        context_notes: str = "",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        parsed = TracebackAnalyzer.parse(traceback_text)
        if not parsed.get("success"):
            return {"success": False,
                    "message": "Could not identify a modifiable first-party source file from the traceback.",
                    "parsed": {k: parsed.get(k) for k in
                               ("target_file", "line_number", "exception_type", "exception_message")}}

        target_path = Path(parsed["target_file"])
        line_num = parsed["line_number"]

        try:
            full_source = target_path.read_text(encoding="utf-8")
        except Exception as e:
            return {"success": False, "message": f"Unable to read '{target_path.name}': {e}"}

        source_lines = full_source.splitlines(keepends=True)
        start_idx = max(0, line_num - 25)
        end_idx = min(len(source_lines), line_num + 25)
        code_context = "".join(source_lines[start_idx:end_idx])

        patch_spec = cls._synthesize_patch_code(
            file_name=target_path.name,
            line_num=line_num,
            exception_type=parsed.get("exception_type", "Error"),
            exception_msg=parsed.get("exception_message", ""),
            code_context=code_context,
            context_notes=context_notes,
        )
        if not patch_spec.get("success"):
            return {"success": False, "message": f"Failed to synthesize patch: {patch_spec.get('error')}"}

        target_chunk = patch_spec["target_chunk"]
        replacement_chunk = patch_spec["replacement_chunk"]
        explanation = patch_spec.get("explanation", "Bug hotfix.")

        if target_chunk not in full_source:
            return {"success": False,
                    "message": "Target code chunk could not be matched precisely in the source file."}

        staged_source = full_source.replace(target_chunk, replacement_chunk, 1)

        valid, syntax_err = SafetySandbox.validate_code(staged_source, file_name=target_path.name)
        if not valid:
            return {"success": False,
                    "message": f"Safety Guard: patch rejected — {syntax_err}"}

        if dry_run:
            return {"success": True, "dry_run": True,
                    "target_file": str(target_path), "explanation": explanation,
                    "target_chunk": target_chunk, "replacement_chunk": replacement_chunk,
                    "message": f"Dry-run passed syntax validation for {target_path.name}."}

        backup_path = SafetySandbox.create_backup(target_path)

        try:
            target_path.write_text(staged_source, encoding="utf-8")
        except Exception as e:
            shutil.copy2(backup_path, target_path)
            return {"success": False, "message": f"Write failed, backup restored: {e}"}

        try:
            py_compile.compile(str(target_path), doraise=True)
        except Exception as pyc_err:
            shutil.copy2(backup_path, target_path)
            return {"success": False, "message": f"Post-write compilation failed — rolled back: {pyc_err}"}

        patch_id = str(uuid.uuid4())[:8]
        history = SafetySandbox._load_history()
        history.append({
            "patch_id": patch_id,
            "timestamp": time.time(),
            "target_file": str(target_path),
            "backup_path": str(backup_path),
            "line_number": line_num,
            "exception_fixed": f"{parsed.get('exception_type')}: {parsed.get('exception_message')}",
            "explanation": explanation,
            "status": "applied",
        })
        SafetySandbox._save_history(history)

        return {"success": True, "patch_id": patch_id,
                "target_file": str(target_path), "backup_path": str(backup_path),
                "explanation": explanation,
                "message": f"Patched '{target_path.name}' line {line_num} (patch {patch_id}). "
                           f"Backup kept. Say 'rollback' to undo."}

    @classmethod
    def _synthesize_patch_code(
        cls,
        file_name: str,
        line_num: int,
        exception_type: str,
        exception_msg: str,
        code_context: str,
        context_notes: str = "",
    ) -> Dict[str, Any]:
        prompt = f"""You are an elite Python debugging engineer.
A Python bug occurred in file '{file_name}' around line {line_num}.
Exception: {exception_type}: {exception_msg}
Additional context: {context_notes}

Relevant source code context:
```python
{code_context}
```

Task: Provide a surgical, minimal fix to eliminate the exception (None-checks, KeyError handling, safe casting, boundary checks — NOT a rewrite).
Output ONLY a strict JSON object with these exact keys:
{{
    "explanation": "One sentence explaining what was fixed",
    "target_chunk": "Exact verbatim string from the code context to replace (characters and whitespace must match exactly)",
    "replacement_chunk": "Replacement code substituted in place of target_chunk"
}}
No markdown fences. Return only the valid JSON object.
"""
        # 1. Primary: SARA's own LLM client (xkiro/openai-compatible/ollama)
        try:
            from core.llm_client import call_llm_text
            raw = call_llm_text(
                prompt,
                system="You are a precise auto-patching engine. Return strict JSON only.",
                timeout=90,
            )
            data = json.loads(_strip_fences(raw))
            if "target_chunk" in data and "replacement_chunk" in data:
                data["success"] = True
                return data
        except Exception as e:
            logger.warning(f"[AutoHeal] llm_client synthesis failed: {e}")

        # 2. Fallback: Gemini via the configured key
        try:
            import json as _json
            cfg = _json.loads((BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8"))
            key = (cfg.get("gemini_api_key") or "").strip()
            if key:
                from google import genai
                g_client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
                for model_name in ("gemini-2.5-flash", "gemini-2.0-flash"):
                    try:
                        resp = g_client.models.generate_content(
                            model=model_name,
                            contents=prompt,
                            config={"temperature": 0.1, "response_mime_type": "application/json"},
                        )
                        raw_text = (getattr(resp, "text", "") or "").strip()
                        if raw_text:
                            data = json.loads(_strip_fences(raw_text))
                            if "target_chunk" in data and "replacement_chunk" in data:
                                data["success"] = True
                                return data
                    except Exception as model_err:
                        logger.warning(f"[AutoHeal] Gemini {model_name} failed: {model_err}")
        except Exception as g_err:
            logger.warning(f"[AutoHeal] Gemini fallback failed: {g_err}")

        return {"success": False,
                "error": "All LLM synthesis backends failed (llm_client + Gemini)."}


# Capture uncaught tracebacks so "heal the last error" has real material.
def _install_error_recorder() -> None:
    _orig = sys.excepthook

    def _hook(tp, val, tb):
        try:
            import traceback
            AutoHealEngine.record_last_error(traceback.format_exception(tp, val, tb))
        except Exception:
            pass
        _orig(tp, val, tb)

    sys.excepthook = _hook


_install_error_recorder()


# ── 4. Tool dispatcher ───────────────────────────────────────────────────────

def auto_heal(parameters: dict, player=None) -> str:
    p = parameters or {}
    action = (p.get("action") or "status").lower().strip()

    if action in ("status",):
        last = AutoHealEngine.get_last_error()
        return ("Last recorded error: " + (last.strip().splitlines()[-1] if last else "(none)")
                + ". Use 'analyze' for a dry-run fix or 'heal' to apply.")

    elif action in ("analyze", "dry_run", "preview"):
        tb = (p.get("traceback") or "").strip() or AutoHealEngine.get_last_error() or ""
        if not tb:
            return "No traceback given and none recorded yet."
        res = AutoHealEngine.heal_traceback(tb, dry_run=True)
        return (res.get("message", "")
                + (f"\nExplanation: {res.get('explanation', '')}"
                   f"\nReplace:\n{res.get('target_chunk', '')[:400]}\nWith:\n{res.get('replacement_chunk', '')[:400]}"
                   if res.get("dry_run") else ""))

    elif action in ("heal", "fix", "apply"):
        tb = (p.get("traceback") or "").strip() or AutoHealEngine.get_last_error() or ""
        if not tb:
            return "No traceback given and none recorded yet."
        res = AutoHealEngine.heal_traceback(tb, context_notes=p.get("notes", ""))
        if player:
            try:
                player.write_log(f"SYS: auto_heal — {res.get('message', '')[:200]}")
            except Exception:
                pass
        return res.get("message", "Heal returned no message.")

    elif action in ("rollback", "undo_patch", "revert"):
        pid = (p.get("patch_id") or "latest").strip()
        res = SafetySandbox.rollback_patch(pid)
        return res.get("message", "Rollback returned no message.")

    elif action in ("history", "patches"):
        hist = AutoHealEngine.get_patch_history(10)
        if not hist:
            return "No patches applied yet."
        lines = [f"{h.get('patch_id')} [{h.get('status')}] {Path(h.get('target_file','')).name} "
                 f"line {h.get('line_number')}: {h.get('explanation','')[:80]}"
                 for h in hist]
        return "Patch history (newest first):\n" + "\n".join(lines)

    return f"Unknown auto_heal action: '{action}'."


TOOL = {
    "name": "auto_heal",
    "description": (
        "Self-healing: analyzes a Python traceback from this codebase, "
        "synthesizes a minimal surgical fix, validates it (AST + compile), "
        "backs up the file, and applies it. Actions: 'analyze' (dry-run, "
        "shows the proposed fix), 'heal' (applies it — needs 'traceback' or "
        "uses the last recorded crash), 'rollback' (undo last/patch_id), "
        "'history', 'status'. Use when the user says: fix yourself, heal "
        "that error, self-heal, or after SARA reports a crash."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "analyze | heal | rollback | history | status",
            },
            "traceback": {
                "type": "STRING",
                "description": "Optional full traceback text. Defaults to the last recorded crash.",
            },
            "notes": {
                "type": "STRING",
                "description": "Optional context for the fix.",
            },
            "patch_id": {
                "type": "STRING",
                "description": "Patch ID to roll back (default: latest).",
            },
        },
        "required": ["action"],
    },
    "handler": auto_heal,
}
