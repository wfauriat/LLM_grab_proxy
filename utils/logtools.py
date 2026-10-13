"""Shared helpers for reading llmgrab captures (logs/*.jsonl). Stdlib only."""

import json
from pathlib import Path

DEFAULT_GLOB = "logs/*.jsonl"

SUGGESTION_MARKER = "[SUGGESTION MODE"


def default_paths() -> list[Path]:
    return sorted(Path(__file__).resolve().parent.glob(DEFAULT_GLOB))


def load_records(paths: list[Path]) -> list[dict]:
    records = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"warning: {path}:{line_no} is not valid JSON, skipped")
    return records


def req_body(rec: dict) -> dict:
    body = rec["request"]["body"]
    return body if isinstance(body, dict) else {}


def resp_body(rec: dict) -> dict:
    body = rec["response"]["body"]
    return body if isinstance(body, dict) else {}


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
