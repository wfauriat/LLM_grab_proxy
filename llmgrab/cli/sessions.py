"""Claude Code specifics of CLI captures: default files, session ids, call classification."""

import json
from pathlib import Path

from llmgrab.paths import CLI_LOGS

SUGGESTION_MARKER = "[SUGGESTION MODE"


def default_paths() -> list[Path]:
    return sorted(CLI_LOGS.glob("*.jsonl"))


def session_id(req: dict) -> str | None:
    """Claude Code packs device/account/session ids as a JSON string in metadata.user_id."""
    raw = (req.get("metadata") or {}).get("user_id")
    try:
        return json.loads(raw).get("session_id")
    except (TypeError, json.JSONDecodeError, AttributeError):
        return None


def content_text(content) -> str:
    """Concatenated text of a message content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


def classify(req: dict) -> str:
    """Heuristic role of a call within Claude Code."""
    if req.get("max_tokens") == 1:
        return "quota_probe"
    messages = req.get("messages") or []
    if messages and messages[-1].get("role") == "user" and \
            content_text(messages[-1].get("content")).lstrip().startswith(SUGGESTION_MARKER):
        return "suggestion"
    if req.get("tools"):
        return "main"
    return "side"
