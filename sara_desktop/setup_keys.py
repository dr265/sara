"""sara_desktop/setup_keys.py — seed the desktop config from backend/.env.

Run from `jarvis_standalone.bat` (step [5/8]) or by hand:

    cd sara_desktop && venv\\Scripts\\python setup_keys.py

WHY

`sara_desktop` reads `sara_desktop/config/api_keys.json` while the rest of the
platform stores credentials in `backend/.env`. On a machine where only the
latter was ever filled in, the standalone assistant would start with no
credential at all and sit in a reconnect loop. This script copies what it
safely can, one direction only (backend/.env -> api_keys.json), and never
overwrites a value already set.

It never prints a secret — only provider NAMES and a pass/fail summary — and
exits non-zero when nothing usable was found, so the launcher can say so
plainly instead of starting an assistant that cannot talk.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DESKTOP_DIR.parent
ENV_FILES = [PROJECT_ROOT / "backend" / ".env", PROJECT_ROOT / ".env"]
CFG_PATH = DESKTOP_DIR / "config" / "api_keys.json"


def _load_dotenv() -> dict[str, str]:
    """Parse KEY=VALUE pairs from the env files (values may be quoted)."""
    merged: dict[str, str] = {}
    for env_file in ENV_FILES:
        if not env_file.exists():
            continue
        try:
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in merged:
                    merged[key] = value
        except Exception as e:
            print(f"[setup_keys] WARNING: could not read {env_file.name}: {e}")
    return merged


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _first(*names: str) -> str:
    """First non-empty value among `names` (env files merged, then os.environ)."""
    env = _load_dotenv()
    for name in names:
        value = (env.get(name) or os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def main() -> int:
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config = _load_json(CFG_PATH)
    had_file = CFG_PATH.is_file()

    filled: list[tuple[str, str]] = []

    def put(key: str, value: str, source: str) -> None:
        if not value:
            return
        if str(config.get(key) or "").strip():
            filled.append((key, "already set"))
            return
        config[key] = value
        filled.append((key, source))

    put("gemini_api_key", _first("GEMINI_API_KEY", "GOOGLE_API_KEY"), "backend/.env")
    put("llm_api_key", _first("LLM_API_KEY", "XKIRO_API_KEY"), "backend/.env")
    put("llm_url", _first("LLM_BASE_URL", "XKIRO_BASE_URL"), "backend/.env")
    put("llm_model", _first("LLM_MODEL", "XKIRO_MODEL"), "backend/.env")
    put("anymodel_api_key", _first("ANYMODEL_API_KEY"), "backend/.env")
    put("anymodel_base_url", _first("ANYMODEL_BASE_URL"), "backend/.env")

    # Only choose a provider when nothing has been chosen yet.
    if not str(config.get("llm_provider") or "").strip():
        if config.get("llm_api_key"):
            config["llm_provider"] = "xkiro"
        elif config.get("anymodel_api_key"):
            config["llm_provider"] = "anymodel"
        if config.get("llm_provider"):
            filled.append(("llm_provider", "inferred"))

    if not str(config.get("os_system") or "").strip():
        config["os_system"] = "windows"
        filled.append(("os_system", "default"))

    try:
        CFG_PATH.write_text(json.dumps(config, indent=4), encoding="utf-8")
        wrote = True
    except OSError as exc:
        print(f"[setup_keys] WARNING: could not write {CFG_PATH.name}: {exc}")
        wrote = False

    env_names = [k for k, v in _load_dotenv().items() if v]
    print(f"[setup_keys] project root : {PROJECT_ROOT}")
    print(f"[setup_keys] env files    : "
          f"{', '.join(str(f.name) for f in ENV_FILES if f.exists()) or 'none found'}")
    print(f"[setup_keys] api_keys.json: {'existing' if had_file else 'created'}"
          f"{'' if wrote else ' (NOT written)'}")

    if filled:
        print("[setup_keys] config entries:")
        for key, origin in sorted(filled):
            print(f"               {key:<18} <- {origin}")

    interesting = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY", "XKIRO_API_KEY",
                   "ANYMODEL_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY",
                   "TOGETHER_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
    present = [k for k in interesting if _first(k)]
    print(f"[setup_keys] provider keys found: {', '.join(present) if present else 'NONE'}")

    voice_ready = bool(config.get("gemini_api_key") or _first("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    text_ready = bool(config.get("llm_api_key") or config.get("anymodel_api_key") or present)

    if voice_ready and text_ready:
        print("[setup_keys] OK — voice and text providers are configured.")
        return 0
    if text_ready:
        print("[setup_keys] PARTIAL — text providers configured, but no Google key, "
              "so the Live voice session will not start. Set GEMINI_API_KEY in "
              "backend/.env (or api_keys.json) to enable voice.")
        return 0
    if voice_ready:
        print("[setup_keys] PARTIAL — voice configured, but no text provider; "
              "model-backed desktop actions will report a degradation.")
        return 0
    print("[setup_keys] FAIL — no provider credential in backend/.env or "
          "config/api_keys.json. Jarvis would start and never answer.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
