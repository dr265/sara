# actions/clipboard_processor.py
"""
Clipboard Processor for SARA.
Ported from Brahma-Echo (actions/clipboard_processor.py).

Lets the assistant instantly read whatever text the user has copied to the
Windows clipboard — "read my clipboard", "what did I copy", "check the
clipboard" — and use it as context for any follow-up request.
"""

import pyperclip


def process_clipboard(parameters: dict | None = None, player=None) -> str:
    """Reads the current text from the clipboard."""
    try:
        content = pyperclip.paste()
        content = (content or "").strip()

        if not content:
            return "The clipboard is empty or does not contain text."

        if player:
            try:
                player.write_log("SYS: Read clipboard contents.")
            except Exception:
                pass

        # Truncate very large clips so the session context is never flooded
        if len(content) > 30000:
            content = content[:30000] + "\n...[Truncated for length]"

        return f"Clipboard Contents:\n\n{content}"
    except Exception as e:
        return f"Failed to read clipboard: {e}"


TOOL = {
    "name": "clipboard_processor",
    "description": (
        "Reads the text the user currently has copied to their clipboard. "
        "Use when they say: read my clipboard, what did I copy, check the "
        "clipboard, or when they refer to 'this' text they just copied."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": [],
    },
    "handler": process_clipboard,
}
