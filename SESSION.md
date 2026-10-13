# LLMgrab — session log (2026-10-12 → 2026-10-13)

## Intent

Observe exactly what Claude clients send to and receive from Anthropic, and build
tools to analyze and replay the captures. Two paths:

1. **CLI grab**: Claude Code, through a local reverse proxy in front of `api.anthropic.com`.
2. **Web grab**: claude.ai in a browser, through a passive packet capture decrypted
   offline with the browser's TLS keys (added on 2026-10-13).

## Context and decisions

- Claude Code honors `ANTHROPIC_BASE_URL`, so a **reverse proxy** on localhost needs no
  TLS interception. Verified that this also works with a **claude.ai subscription
  (OAuth)** login.
- The session must be *started* with the variable set; a running session can't be redirected.
- **mitmproxy** (reverse mode + Python addon) was chosen over a custom proxy.
- Everything runs from the **project-local `.venv`** (Python 3.12, installed with `uv`).
  The repo is an installable package (`pyproject.toml`, `uv pip install -e .` /
  `make install`) exposing one `llmgrab` command; `requirements.txt` pins the exact
  versions in `.venv`.
- The proxy only listens on `127.0.0.1`, so it is not an open relay on the LAN.
- The proxy is **observe-only**: it never changes traffic. Rewriting `thinking.display`
  to get thinking summaries was discussed and deliberately skipped.
- Captures contain full prompts and personal data (e.g. the user's email inside a
  `<system-reminder>`, or in a claude.ai login URL). Captures and HTML are written with
  `600` permissions and the `data/` directories with `700`; `data/` and `render/` are
  gitignored. Auth headers and cookies are redacted (bodies are not). Nothing is published.

### Web grab
- claude.ai can't be pointed at a proxy URL, so the options were a forward MITM proxy
  (mitmproxy + installed CA), passive capture, or browser-side hooks. A reverse proxy
  was ruled out (cookies, origin, login redirects are tied to the `claude.ai` domain).
- **Passive capture** was chosen first: nothing is installed in the browser, traffic is
  untouched, and Cloudflare sees the browser's real TLS fingerprint (a MITM proxy's
  different fingerprint could trigger bot challenges).
- How: a **dedicated Firefox profile** (snap Firefox, `--new-instance` so a running
  Firefox doesn't take over) started with `SSLKEYLOGFILE`; **HTTP/3 disabled** in that
  profile so traffic stays on TCP + TLS 1.3 + HTTP/2. Packets are captured with
  `tcpdump`, which needs root: Claude runs as the restricted `ai-user`, so the user runs
  `sudo tcpdump -Z ai-user ...` from an admin shell (the capture file ends up owned by
  `ai-user`).
- The decoder is **pure Python** in `.venv` (dpkt, cryptography, hpack; no Wireshark):
  TCP reassembly, TLS 1.3 decryption from the key log, HTTP/2 + HPACK, body
  decompression, schemaless protobuf. Only `claude.ai` / `*.anthropic.com` hosts are kept.

### Code layout
- One installable package `llmgrab` (`cli/` and `web/` subpackages for the two paths,
  shared modules at the top), one `llmgrab` command, `Makefile` shortcuts (`cli-*`,
  `web-*`). Imports work everywhere through the editable install, including the
  mitmproxy addon.
- Both paths produce the same JSONL record shape, and both are turned into one
  source-neutral **transcript** model rendered by shared HTML and terminal renderers.
- The analysis is shared the same way: each source provides a calls / turns table
  (common columns + its own) and an HTTP exchanges table; the report sections are
  common, plus a few per source. Times are shown in local time, like the replays.

## What was built

| File | Purpose |
|---|---|
| `llmgrab/__main__.py` | `llmgrab <command>` entry point (also wrapped by `Makefile` targets) |
| `llmgrab/paths.py` | Project locations (`data/cli`, `data/web`, `render`) and owner-only file writing |
| `llmgrab/records.py` | Shared record helpers: loading, header redaction, SSE parsing, Messages-API reassembly |
| `llmgrab/transcript.py` | Source-neutral conversation model (conversations, messages, blocks, panels, notices, groups) |
| `llmgrab/render_html.py` | Transcript -> chat-style HTML page (Markdown, light/dark) |
| `llmgrab/render_text.py` | Transcript -> terminal text (ANSI colors, truncation) |
| `llmgrab/replay.py` | `llmgrab replay cli\|web [--html]`: builds the transcript for a source, renders it |
| `llmgrab/cli/addon.py` | mitmproxy addon: one JSONL record per exchange in `data/cli/YYYY-MM-DD.jsonl` |
| `llmgrab/cli/sessions.py` | Claude Code specifics: session id, call classification |
| `llmgrab/analyze.py` | `llmgrab analyze cli\|web\|all`: shared pandas report (calls / turns, per conversation, by kind, errors, HTTP endpoints); `--csv` export |
| `llmgrab/cli/analyze.py` | CLI rows (tokens, cache hit, latency, tools) + tools called / tool-definition sizes sections |
| `llmgrab/cli/jqx.sh` | 25 jq recipes to explore records (structure, request, response, search, diffs); prints each command so it can be copied |
| `llmgrab/cli/history.py` | Claude Code calls -> transcript, showing only what's new in each call |
| `llmgrab/web/launch_firefox.sh` | Dedicated Firefox profile (HTTP/3 off) writing TLS keys to `data/web/keys.log` |
| `llmgrab/web/pcap.py` | Passive capture decoder: TCP reassembly, TLS 1.3 decryption, HTTP/2 + HPACK -> `data/web/claude.jsonl` |
| `llmgrab/web/protobuf.py` | Schemaless protobuf + Connect-RPC framing (claude.ai chat API) |
| `llmgrab/web/timeline.py` | claude.ai `StreamTimeline` events -> transcript |
| `llmgrab/web/analyze.py` | Web rows (turn timings, usage levels) + traffic-by-category and timeline-poll sections |
| `llmgrab/web/clean.py` | Deletes pcap, key log and Firefox profile once decoded (checks, then asks) |

### `llmgrab/cli/addon.py`
- `responseheaders`: SSE replies (`text/event-stream`) are **streamed through unbuffered**
  using a `tee` callable that copies each chunk. Bytes are left unchanged (verified offline).
- `response`: builds the record: id, timestamp, method, path, status, `duration_s`,
  `ttfb_s`, `streamed`, request/response headers (redacted) and bodies (decompressed,
  parsed as JSON).
- Streamed replies are decompressed, split into SSE events (`response.sse_events`,
  toggle `KEEP_SSE_EVENTS`) and **rebuilt into the final message** (`response.body`):
  text, thinking, signatures, citations, tool_use inputs from `input_json_delta`,
  `stop_reason`, merged usage.
- Successful health checks (`/api/hello` with 200) are not logged.

### `sessions.classify()` call kinds
`quota_probe` (`max_tokens == 1`), `suggestion` (last user message starts with
`[SUGGESTION MODE`), `main` (has tools), `side` (everything else: title generation, `-p` without tools).

### Replays (`replay.py`, `cli/history.py`, `web/timeline.py`)
- Both sources are turned into one `Transcript` model, rendered by the same HTML and
  terminal renderers.
- CLI: `iter_events()` walks a session's calls and yields what is new; `SessionBuilder`
  turns that into transcript items.
- Main calls: only messages after the common prefix with the previous main call are shown.
  The comparison ignores `cache_control` markers and string-vs-block content format,
  since Claude Code moves the cache marker each turn. The previous reply is collapsed
  when re-sent, compared on meaningful fields only, since the API adds `caller`. Real
  rewrites raise a "history rewritten" warning.
- Side calls show all their messages. Suggestion calls show only the last message.
- Tool calls hold their results (taken from the next request), in HTML and in the terminal.
- HTML: user bubbles, Markdown replies (raw HTML disabled), expandable tool cards,
  collapsed system prompt, tools, reminders and side calls, a per-call usage line,
  light/dark mode.
- Web: one message per user / assistant turn; thinking shows only its status and title
  (the text is never sent to the browser); footer with timings and usage-limit levels.
- `group_sessions()` groups records by the `session_id` found inside `metadata.user_id`.

## How to use

See `README.md` for details.

```bash
make install                      # .venv + editable install (once)
make                              # list targets; `.venv/bin/llmgrab` lists commands

# CLI grab
make cli-proxy                    # terminal 1: reverse proxy on 127.0.0.1:8080
make cli-claude                   # terminal 2: Claude Code through it
make cli-analyze                  # ARGS="--csv calls.csv"; make analyze: both sources
make cli-jq ARGS="messages -1"    # jq recipes (no ARGS: list them)
make -s cli-replay | less -R      # or: llmgrab replay cli [--hide-side] [--full]
make cli-html                     # render/replay.html

# Web grab
make web-tcpdump                  # prints the sudo tcpdump line: run it from an admin shell
make web-browser                  # Firefox with TLS key logging; use claude.ai, then close it
make web-decode                   # data/web/claude.pcap + keys.log -> data/web/claude.jsonl
make web-html                     # render/web_replay.html (terminal: make web-replay)
make web-analyze                  # turns, timings, usage levels, traffic, timeline polls
make web-clean                    # after decode: delete pcap, keys, profile (asks; --keep-profile)
```

## Findings from the captures

- **Interactive turn**: ~65k input tokens. 48 tool definitions are ~154k chars
  (~38k tokens; `Artifact` alone is 23%). The system prompt has 3 blocks: a billing
  header, "You are Claude Code…", and the main prompt, all cached with a 1h TTL.
  The ~24k-char environment context is sent as a **mid-conversation `system` message**.
  The user's text arrives wrapped in `<system-reminder>` blocks.
- **Caching works**: the first main call writes ~65k tokens to the cache, and later
  calls read 99% from it.
- **Helper calls per interactive session**: a quota probe (`max_tokens: 1`, got a 429
  but the session continued normally), session-title generation (structured JSON
  output), and a prompt **suggestion** call that reuses the main history and cache.
- **Thinking**: Opus 5.5 always thinks. Claude Code sends `display: "updates"`
  (interactive) or `"omitted"` (`-p`), so thinking text arrives empty with only a
  signature, although thinking tokens are billed. The raw chain of thought is never
  returned by the API; `"summarized"` would give a summary.
- **`claude -p --system-prompt … --tools ""`**: a single call of ~550 tokens with 0 tools.
  Claude Code still prepends the billing block and a one-line identity sentence
  ("Claude agent, built on Anthropic's Claude Agent SDK"), and still adds the email
  reminder and a short environment message. There are no quota, title or suggestion calls.
- Direct custom requests are possible with a Console **API key** (`x-api-key`). The
  subscription OAuth token is meant for Claude Code and should not be reused for
  custom calls.

### Web grab (one capture, 2026-10-13, 3-turn conversation)
- The key log covered every Firefox connection; other programs' traffic in the pcap
  (VS Code, npm, this Claude Code session) stays encrypted, as expected.
- All claude.ai calls share **one long-lived HTTP/2 connection**. About 30 REST calls at
  page load (`/api/organizations/<uuid>/...`: conversations, projects, skills, MCP,
  subscription), Datadog RUM (`/api/v2/rum`) every ~30 s, `event_logging` batches,
  magic-link login, a Cloudflare challenge script, and the connector directory on
  `api.anthropic.com/api/directory/servers`.
- **Chat is Connect-RPC with protobuf**, not the JSON SSE `/completion` endpoint often
  described (Claude's own answer in that chat described the latter):
  - `anthropic.bard.api.v1alpha.ConversationService/PerformAction` sends a message:
    conversation / message / parent UUIDs, text, model (`claude-opus-5-5`), time zone,
    locale, a per-session counter; the response is empty.
  - `.../StreamTimeline` is a ~30 s long-poll streaming gzip-compressed protobuf
    events: heartbeats, upserts (conversation, message, block group, content block),
    text deltas, usage-limit levels (5h / 7d / overage, as doubles). Reconnects resume
    from an event id and re-send full snapshots of recent blocks. The first two polls
    returned "conversation not found" before the conversation existed.
- **Thinking** reaches the browser only as a status ("Réflexion en cours…" →
  "A réfléchi pendant 5s"), start/end times and a one-line title; never its text.
- Timings per turn: thinking 3–6 s, first text 7.6–18.5 s, done 16.6–35.2 s. In turn 2
  the assistant message was only created ~16 s after the user's (cause unknown).
- The conversation title and effort (`medium`) also arrive through timeline upserts.

## Open ideas (not done)

- Web grab: capture conversations with tools, web search, artifacts, attachments and
  edited / branched messages to map more of the timeline schema; TLS 1.2 / QUIC
  support if ever needed.
- `--redact` option for `llmgrab replay` (email, home path, username) before sharing.
- Separate `print` kind for `-p` runs. Exact per-tool token counts via `count_tokens`.
- Diff view of how the prompt/system/tools change across turns. Charts of
  tokens/cache per turn.
- Test `--bare` / `--strict-mcp-config` to see what context they remove.
