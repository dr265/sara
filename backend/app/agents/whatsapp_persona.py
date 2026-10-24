"""Configurable agent persona for the WhatsApp agent.

The WhatsApp agent is NOT locked to hotels — any business (salon, restaurant,
clinic, real-estate office, or a general-purpose assistant) can define what the
agent is and does. Two layers:

  WA_AGENT_PERSONA (env)   — default persona, set in backend/.env
  persona.json (on disk)   — overrides env once changed at runtime via the API

The persona text is injected into the LLM system prompt as <agent_persona>,
and business specifics (services, prices, policies) live in business_context.json
which is hot-reloaded on change. The output schema stays generic so the same
pipeline works for any domain.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

PERSONA_FILE = Path(os.getenv("WA_AGENT_PERSONA_FILE", "whatsapp_persona.json"))
if not PERSONA_FILE.is_absolute():
    PERSONA_FILE = Path(__file__).resolve().parent.parent.parent / PERSONA_FILE

DEFAULT_PERSONA = os.getenv(
    "WA_AGENT_PERSONA",
    (
        "You are SARA, an AI assistant for a business that serves customers over "
        "WhatsApp. You help with the business's services, prices, availability, "
        "bookings and general questions. Read <business_context> for the exact "
        "services, prices and policies — never invent data."
    ),
)


class PersonaStore:
    """Runtime-configurable persona: file overrides env once edited."""

    def __init__(self, persona_file: Path = PERSONA_FILE, default: str = DEFAULT_PERSONA) -> None:
        self._file = persona_file
        self._default = default

    def load(self) -> str:
        """Persona: on-disk override > env default."""
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            persona = (data or {}).get("persona") or ""
            if persona.strip():
                return persona.strip()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return self._default

    def save(self, persona: str) -> str:
        persona = (persona or "").strip()
        if not persona:
            raise ValueError("persona cannot be empty")
        self._file.write_text(
            json.dumps({"persona": persona}, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return persona

    def reset(self) -> str:
        try:
            self._file.unlink()
        except FileNotFoundError:
            pass
        return self._default

    def info(self) -> Dict[str, Any]:
        return {
            "persona": self.load(),
            "source": "persona.json" if self._file.exists() else "env (WA_AGENT_PERSONA)",
            "file": str(self._file),
            "business_context_file": str(
                Path(__file__).resolve().parent.parent.parent / "business_context.json"
            ),
        }