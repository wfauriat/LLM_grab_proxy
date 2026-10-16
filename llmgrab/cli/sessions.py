"""Claude Code specifics of CLI captures: default files, session ids, call classification."""

import json
from pathlib import Path

from llmgrab.paths import CLI_LOGS
from llmgrab.records import req_body

SUGGESTION_MARKER = "[SUGGESTION MODE"
WEB_FETCH_MARKER = "Web page content:"
SESSION_HEADER = "x-claude-code-session-id"


def default_paths() -> list[Path]:
    return sorted(CLI_LOGS.glob("*.jsonl"))


def session_id(rec: dict) -> str | None:
    """Claude Code packs device/account/session ids as a JSON string in metadata.user_id.

    Calls without metadata (count_tokens) still carry the session id in a header.
    """
    raw = (req_body(rec).get("metadata") or {}).get("user_id")
    try:
        return json.loads(raw).get("session_id")
    except (TypeError, json.JSONDecodeError, AttributeError):
        return rec["request"].get("headers", {}).get(SESSION_HEADER)


def content_text(content) -> str:
    """Concatenated text of a message content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


def is_server_tool(tool: dict) -> bool:
    """Tools run by the API (web_search_20250305...) have a versioned type and no input_schema."""
    return tool.get("type", "custom") != "custom" and "input_schema" not in tool


def classify(rec: dict) -> str:
    """Heuristic role of a call within Claude Code."""
    if "/count_tokens" in rec.get("path", ""):
        return "count_tokens"
    req = req_body(rec)
    if req.get("max_tokens") == 1:
        return "quota_probe"
    messages = req.get("messages") or []
    last = content_text(messages[-1].get("content")).lstrip() if messages and messages[-1].get("role") == "user" else ""
    if last.startswith(SUGGESTION_MARKER):
        return "suggestion"
    tools = req.get("tools") or []
    if tools and all(is_server_tool(t) for t in tools):
        return "web_search"  # the WebSearch tool: a sub-call with only the API's web_search tool
    if tools:
        return "main"
    if last.startswith(WEB_FETCH_MARKER):
        return "web_fetch"   # the WebFetch tool: a sub-call summarizing the fetched page
    return "side"
