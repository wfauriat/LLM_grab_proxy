"""mitmproxy addon: log Claude Code <-> Anthropic API exchanges as JSONL.

Usage:
    .venv/bin/mitmdump --mode reverse:https://api.anthropic.com \
        --listen-host 127.0.0.1 -p 8080 -s llmgrab_addon.py
"""

import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import http
from mitmproxy.net import encoding

LOG_DIR = Path(__file__).resolve().parent / "logs"

# Keep every raw SSE event in the record (verbose, but lossless).
KEEP_SSE_EVENTS = True

# Successful health checks are not logged.
HEALTH_CHECK_PATHS = {"/api/hello"}

REDACTED_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
}


def redact_headers(headers) -> dict:
    """Header dict with secrets replaced. Repeated headers are joined with ', '."""
    out = {}
    for name in headers.keys():
        value = ", ".join(headers.get_all(name))
        if name.lower() in REDACTED_HEADERS:
            value = f"<redacted {len(value)} chars>"
        out[name.lower()] = value
    return out


def parse_text(text: str | None):
    """Parsed JSON if possible, else the text itself (None stays None)."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def decode_body(message: http.Message):
    """Body as JSON or text; `get_text()` undoes Content-Encoding (gzip/br/zstd)."""
    if not message.raw_content:
        return None
    return parse_text(message.get_text(strict=False))


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


def append_record(record: dict) -> None:
    LOG_DIR.mkdir(mode=0o700, exist_ok=True)
    path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
    # O_CREAT with 0o600: file is only readable by its owner.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class LLMGrab:
    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Stream SSE replies through to the client, keeping a copy of each chunk."""
        if "text/event-stream" not in flow.response.headers.get("content-type", ""):
            return
        chunks: list[bytes] = []
        flow.metadata["llmgrab_chunks"] = chunks

        def tee(data: bytes) -> bytes:
            if data:
                if not chunks:
                    flow.metadata["llmgrab_first_chunk_at"] = time.time()
                chunks.append(data)
            return data

        flow.response.stream = tee

    def response(self, flow: http.HTTPFlow) -> None:
        req, resp = flow.request, flow.response
        if req.path.split("?")[0] in HEALTH_CHECK_PATHS and resp.status_code == 200:
            return

        record = {
            "id": flow.id,
            "timestamp": datetime.fromtimestamp(
                req.timestamp_start, tz=timezone.utc
            ).isoformat(),
            "method": req.method,
            "path": req.path,
            "status": resp.status_code,
            "duration_s": round(resp.timestamp_end - req.timestamp_start, 3),
            "ttfb_s": None,
            "streamed": "llmgrab_chunks" in flow.metadata,
            "request": {
                "headers": redact_headers(req.headers),
                "body": decode_body(req),
            },
            "response": {
                "headers": redact_headers(resp.headers),
                "body": None,
            },
        }

        if record["streamed"]:
            raw = b"".join(flow.metadata["llmgrab_chunks"])
            content_encoding = resp.headers.get("content-encoding")
            if content_encoding:
                raw = encoding.decode(raw, content_encoding)
            events = parse_sse(raw.decode("utf-8", errors="replace"))
            record["response"]["body"] = reassemble_message(events)
            if KEEP_SSE_EVENTS:
                record["response"]["sse_events"] = events
            if "llmgrab_first_chunk_at" in flow.metadata:
                record["ttfb_s"] = round(
                    flow.metadata["llmgrab_first_chunk_at"] - req.timestamp_start, 3
                )
        else:
            record["response"]["body"] = decode_body(resp)

        append_record(record)


addons = [LLMGrab()]
