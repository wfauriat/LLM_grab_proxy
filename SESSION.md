# LLMgrab — session log (2026-10-12 → 2026-10-13)

## Intent

Observe exactly what Claude Code sends to and receives from the Anthropic API, by
putting a local proxy between the CLI and `api.anthropic.com`, logging every exchange,
and building tools to analyze and replay the captures.

## Context and decisions

- Claude Code honors `ANTHROPIC_BASE_URL`, so a **reverse proxy** on localhost needs no
  TLS interception. Verified that this also works with a **claude.ai subscription
  (OAuth)** login.
- The session must be *started* with the variable set; a running session can't be redirected.
- **mitmproxy** (reverse mode + Python addon) was chosen over a custom proxy.
- `/opt/venvs/pyDS` is not writable by `ai-user`, so mitmproxy lives in a
  **project-local `.venv`** (Python 3.12, mitmproxy 12.2.3, installed with `uv`).
  The analysis scripts run with pyDS (pandas, markdown-it).
- The proxy only listens on `127.0.0.1`, so it is not an open relay on the LAN.
- The proxy is **observe-only**: it never changes traffic. Rewriting `thinking.display`
  to get thinking summaries was discussed and deliberately skipped.
- Captures contain full prompts and personal data (e.g. the user's email inside a
  `<system-reminder>`). Logs and HTML are written with `600` permissions and the
  `logs/` directory with `700`. Auth headers are redacted. Nothing is published.

## What was built

| File | Purpose |
|---|---|
| `.venv/` | Project venv holding mitmproxy |
| `llmgrab_addon.py` | mitmproxy addon: one JSONL record per exchange in `logs/YYYY-MM-DD.jsonl` |
| `logtools.py` | Shared stdlib helpers: load records, session id, call classification |
| `analyze.py` | pandas report: calls, per-session totals, cache hit, latency, errors, tool usage, tool-definition sizes; `--csv` export |
| `jqx.sh` | 25 jq recipes to explore records (structure, request, response, search, diffs); prints each command so it can be copied |
| `replay.py` | Terminal replay of sessions, turn by turn, showing only what's new in each call |
| `replay_html.py` | Same replay as a self-contained, chat-style HTML page (`replay.html`) |

### `llmgrab_addon.py`
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

### `logtools.classify()` call kinds
`quota_probe` (`max_tokens == 1`), `suggestion` (last user message starts with
`[SUGGESTION MODE`), `main` (has tools), `side` (everything else: title generation, `-p` without tools).

### `replay.py` / `replay_html.py`
- `iter_events()` produces a renderer-independent event stream. Both renderers use it.
- Main calls: only messages after the common prefix with the previous main call are shown.
  The comparison ignores `cache_control` markers and string-vs-block content format,
  since Claude Code moves the cache marker each turn. The previous reply is collapsed
  when re-sent, compared on meaningful fields only, since the API adds `caller`. Real
  rewrites raise a "history rewritten" warning.
- Side calls show all their messages. Suggestion calls show only the last message.
- HTML: user bubbles, Markdown replies (raw HTML disabled), tool cards that contain
  their results (taken from the next request), collapsed system prompt, tools,
  reminders and side calls, a per-call usage line, light/dark mode.
- `group_sessions()` groups records by the `session_id` found inside `metadata.user_id`.

## How to use

```bash
# 1. proxy (terminal 1)
.venv/bin/mitmdump --mode reverse:https://api.anthropic.com \
    --listen-host 127.0.0.1 -p 8080 -s llmgrab_addon.py
# 2. Claude Code through it (terminal 2)
ANTHROPIC_BASE_URL=http://127.0.0.1:8080 claude
# 3. analysis
/opt/venvs/pyDS/bin/python analyze.py [--csv calls.csv]
./jqx.sh                     # list recipes; ./jqx.sh messages -1
/opt/venvs/pyDS/bin/python replay.py [--hide-side] [--full] | less -R
/opt/venvs/pyDS/bin/python replay_html.py [-o replay.html]
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

## Open ideas (not done)

- `--redact` option for `replay_html.py` (email, home path, username) before sharing.
- Separate `print` kind for `-p` runs. Exact per-tool token counts via `count_tokens`.
- Diff view of how the prompt/system/tools change across turns. Charts of
  tokens/cache per turn.
- Test `--bare` / `--strict-mcp-config` to see what context they remove.
