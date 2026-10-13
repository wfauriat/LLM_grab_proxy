"""mitmproxy addon: log Claude Code <-> Anthropic API exchanges as JSONL in data/cli/.

Usage (or `llmgrab proxy`):
    .venv/bin/mitmdump --mode reverse:https://api.anthropic.com \
        --listen-host 127.0.0.1 -p 8080 -s llmgrab/cli/addon.py
"""

import json
import time
from datetime import datetime, timezone

from mitmproxy import http
from mitmproxy.net import encoding

from llmgrab.paths import CLI_LOGS, open_private
from llmgrab.records import parse_sse, parse_text, reassemble_message, redact_headers

# Keep every raw SSE event in the record (verbose, but lossless).
KEEP_SSE_EVENTS = True

# Successful health checks are not logged.
HEALTH_CHECK_PATHS = {"/api/hello"}


def decode_body(message: http.Message):
    """Body as JSON or text; `get_text()` undoes Content-Encoding (gzip/br/zstd)."""
    if not message.raw_content:
        return None
    return parse_text(message.get_text(strict=False))


def append_record(record: dict) -> None:
    with open_private(CLI_LOGS / f"{datetime.now():%Y-%m-%d}.jsonl", "a") as f:
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
                "headers": redact_headers(req.headers.items(multi=True)),
                "body": decode_body(req),
            },
            "response": {
                "headers": redact_headers(resp.headers.items(multi=True)),
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
