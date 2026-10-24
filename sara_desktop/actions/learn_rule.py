# actions/learn_rule.py
"""
Voice control for the learned-rules engine (core/learned_rules.py).
Ported concept from Brahma-Echo's learning system.

When the user corrects SARA ("never do X again", "always answer me in
Hindi", "from now on, use Chrome"), the model stores the rule here instead
of forgetting it when the session ends. Active rules are injected into the
system prompt on every session build.
"""

from core.learned_rules import LearnedRulesEngine


def learn_rule(parameters: dict, player=None) -> str:
    p = parameters or {}
    action = (p.get("action") or "add").lower().strip()
    rule_text = (p.get("rule") or "").strip()
    rule_id = (p.get("rule_id") or "").strip()

    if action in ("add", "learn", "save", "remember"):
        if not rule_text:
            return "The rule text is empty — nothing to learn."
        res = LearnedRulesEngine.add_rule(rule_text, origin="voice_command")
        if player:
            try:
                player.write_log(f"SYS: {res.get('message', 'Rule learned.')}")
            except Exception:
                pass
        return res.get("message", "Rule learned.")

    elif action in ("list", "show", "all"):
        rules = LearnedRulesEngine.list_rules()
        if not rules:
            return "No learned rules yet."
        lines = [f"{i}. [{'ON' if r.get('active', True) else 'OFF'}] "
                 f"{r.get('rule')} (id: {r.get('id')})"
                 for i, r in enumerate(rules, 1)]
        return "Learned rules:\n" + "\n".join(lines)

    elif action in ("toggle", "enable", "disable"):
        if not rule_id:
            return "Provide the rule ID to toggle."
        res = LearnedRulesEngine.toggle_rule(rule_id)
        return res.get("message", "Could not toggle rule.")

    elif action in ("delete", "remove", "forget"):
        if not rule_id and rule_text:
            # Allow deleting by matching text
            for r in LearnedRulesEngine.list_rules():
                if rule_text.lower() in r.get("rule", "").lower():
                    rule_id = r.get("id")
                    break
        if not rule_id:
            return "Provide the rule ID or text to delete."
        res = LearnedRulesEngine.delete_rule(rule_id)
        return res.get("message", "Could not delete rule.")

    return f"Unknown learn_rule action: '{action}'."


TOOL = {
    "name": "learn_rule",
    "description": (
        "Learns, lists, or removes persistent behavioral rules. "
        "Use 'add' whenever the user corrects you or states a lasting "
        "preference: 'never open Edge again', 'always reply in Hindi', "
        "'from now on call me boss', 'stop asking before deleting'. "
        "Rules are injected into your system prompt on every session. "
        "Use 'list' to show rules, 'toggle'/'delete' with a rule_id to manage them."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "add | list | toggle | delete",
            },
            "rule": {
                "type": "STRING",
                "description": "The rule text to learn (for add), or to match (for delete).",
            },
            "rule_id": {
                "type": "STRING",
                "description": "Rule ID for toggle/delete (from list).",
            },
        },
        "required": ["action"],
    },
    "handler": learn_rule,
}
