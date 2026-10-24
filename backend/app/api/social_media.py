"""Thin re-export — the Social Media Agency lives in ONE file:
   app.agents.social_media   (engine + adapters + router + everything)

This module exists only so main.py's `from app.api import social_media` keeps
working. The actual router is defined in the engine module.
"""
from app.agents.social_media import router  # noqa: F401

__all__ = ["router"]
