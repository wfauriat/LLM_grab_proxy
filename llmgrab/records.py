"""Capture records shared by both grab paths: JSONL loading, redaction, SSE parsing.

A record is one HTTP exchange: id, timestamp, method, path, status, timings,
request/response headers (secrets redacted) and bodies (JSON when possible).
Stdlib only, so the mitmproxy addon can import it.
"""

import copy
import json
from pathlib import Path

REDACTED_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
}


def redact_headers(pairs) -> dict:
    """(name, value) pairs -> dict with secrets replaced. Repeated headers are joined with ', '."""
    out = {}
    for name, value in pairs:
        name = name.lower()
        out[name] = f"{out[name]}, {value}" if name in out else value
    for name in out.keys() & REDACTED_HEADERS:
        out[name] = f"<redacted {len(out[name])} chars>"
    return out


def parse_text(text: str | None):
    """Parsed JSON if possible, else the text itself (None stays None)."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def parse_sse(text: str) -> list[dict]:
    """Split a server-sent-events stream into [{"event": ..., "data": ...}]."""
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        event, data_lines = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
        if event or data_lines:
            events.append({"event": event, "data": parse_text("\n".join(data_lines))})
    return events


def reassemble_message(events: list[dict]) -> dict | None:
    """Rebuild the final Messages API response from its stream events."""
    msg = None
    partial_json = {}  # content block index -> accumulated tool input JSON string
    for ev in events:
        data = ev["data"]
        if not isinstance(data, dict):
            continue
        kind = data.get("type")

        if kind == "message_start":
            msg = copy.deepcopy(data["message"])
            msg.setdefault("content", [])

        elif msg is None:
            if kind == "error":
                return {"type": "error", "error": data.get("error")}
            continue

        elif kind == "content_block_start":
            idx = data["index"]
            while len(msg["content"]) <= idx:
                msg["content"].append(None)
            msg["content"][idx] = copy.deepcopy(data["content_block"])

        elif kind == "content_block_delta":
            idx, delta = data["index"], data["delta"]
            block = msg["content"][idx]
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta["text"]
            elif dtype == "input_json_delta":
                partial_json[idx] = partial_json.get(idx, "") + delta["partial_json"]
            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + delta["signature"]
            elif dtype == "citations_delta":
                block.setdefault("citations", []).append(delta["citation"])

        elif kind == "content_block_stop":
            idx = data["index"]
            if idx in partial_json:
                msg["content"][idx]["input"] = parse_text(partial_json.pop(idx)) or {}

        elif kind == "message_delta":
            msg.update(data.get("delta", {}))
            msg.setdefault("usage", {}).update(data.get("usage") or {})

        elif kind == "error":
            msg["stream_error"] = data.get("error")

    return msg


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
