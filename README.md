# llmgrab

Observe what Claude clients send to and receive from Anthropic, then replay and
analyze the captures. Two capture paths share one record format and one replay stack:

| Path | Client | How traffic is obtained | Output |
|---|---|---|---|
| **CLI grab** | Claude Code | local reverse proxy (mitmproxy) set as `ANTHROPIC_BASE_URL` | `data/cli/YYYY-MM-DD.jsonl` |
| **Web grab** | claude.ai in Firefox | passive packet capture + TLS key log, decrypted offline | `data/web/claude.jsonl` |

```
CLI:  claude ──HTTP──▶ mitmproxy :8080 ──HTTPS──▶ api.anthropic.com
                         └─ cli/addon.py ──▶ data/cli/*.jsonl ─┐
                                                               ├─▶ transcript ─▶ HTML / terminal
Web:  Firefox ──HTTPS──▶ claude.ai                             │
        └─ keys.log    tcpdump ─▶ claude.pcap                  │
                         └─ web/pcap.py ──▶ data/web/claude.jsonl
```

Captures are personal data (prompts, email, conversation contents): everything under
`data/` and `render/` is owner-only and gitignored. Only auth headers and cookies are
redacted.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12. Everything runs from the
project's own `.venv`:

```bash
make install        # creates .venv, installs llmgrab in editable mode
make                # list make targets
.venv/bin/llmgrab   # list commands (`llmgrab <command> -h` for options)
```

Other tools: `claude` (CLI grab), `jq` (`cli-jq` recipes), Firefox, `tcpdump` and an
account with `sudo` (web grab).

## CLI grab

```bash
make cli-proxy                   # terminal 1: reverse proxy on 127.0.0.1:8080
make cli-claude                  # terminal 2: Claude Code through it (ARGS are passed to claude)
make -s cli-replay | less -R     # terminal replay; ARGS="--hide-side --full --session 8dc6"
make cli-html                    # render/replay.html
make cli-analyze                 # report (make analyze: both sources); ARGS="--csv calls.csv"
make cli-jq ARGS="messages -1"   # jq recipes; no ARGS lists them
make -s cli-flatten | less       # best-guess flattened model input; ARGS="--list", "--call 9 --template chatml --marks"
```

Claude Code honors `ANTHROPIC_BASE_URL`, so no TLS interception is needed, including
with a claude.ai subscription login. The variable must be set when the session
starts. The proxy only listens on `127.0.0.1` and never modifies traffic: streamed
replies are passed through chunk by chunk while a copy is kept.

## Web grab

```bash
make web-tcpdump    # prints the capture command; run it from an admin shell, keep it running
make web-browser    # dedicated Firefox profile; log in to claude.ai, chat, close the window
                    # then stop tcpdump (Ctrl+C)
make web-decode     # data/web/claude.pcap + keys.log -> data/web/claude.jsonl (+ connection summary)
make web-html       # render/web_replay.html (terminal: make web-replay)
make web-analyze    # report: turns, timings, usage levels, traffic, timeline polls
make web-clean      # delete pcap, keys.log and the Firefox profile; keeps claude.jsonl
```

- **Start tcpdump before Firefox.** A connection can only be decoded from its first
  packet (see [Fragile parts](#fragile-parts)).
- The Firefox profile lives in `data/web/firefox-profile/`. It has HTTP/3 disabled and
  writes its TLS secrets to `data/web/keys.log`. Your normal Firefox profile is not
  touched.
- `keys.log` decrypts the whole capture: treat it like a password.
- `make web-clean` deletes the raw data once decoded. It lists what it will delete and
  asks first. It refuses while tcpdump or that Firefox is running, or if
  `claude.jsonl` is missing or older than the pcap.
  - `ARGS="--keep-profile"` keeps the login for the next capture.
  - Log out of claude.ai in that window first: deleting the profile doesn't end the
    session on the server.
  - Once the pcap is gone, the capture can't be decoded again, for example by an
    improved decoder.

## Components

```
llmgrab/
  __main__.py      `llmgrab <command>`: dispatches to the modules below (lazy imports)
  paths.py         data/, render/ locations; owner-only file writing
  records.py       record format helpers: loading, header redaction, SSE parsing,
                   Messages-API stream reassembly (stdlib only)
  transcript.py    source-neutral conversation model
  render_html.py   transcript -> standalone chat-style page (Markdown, light/dark)
  render_text.py   transcript -> terminal text (colors, truncation)
  replay.py        `llmgrab replay cli|web [--html]`
  analyze.py       `llmgrab analyze cli|web|all`: shared report sections
  cli/
    addon.py       mitmproxy addon: one record per exchange
    sessions.py    Claude Code specifics: session id, call classification
    history.py     calls -> transcript, keeping only what is new in each call
    analyze.py     CLI rows (tokens, cache, tools) + tool sections for the report
    flatten.py     request -> best-guess flat text the model reads (guessed Anthropic or ChatML template)
    jqx.sh         jq recipes over the CLI records
  web/
    launch_firefox.sh  Firefox with SSLKEYLOGFILE, HTTP/3 off
    pcap.py        pcap + key log -> records (TCP, TLS 1.3, HTTP/2, HPACK, bodies)
    protobuf.py    schemaless protobuf + Connect-RPC stream framing
    timeline.py    claude.ai StreamTimeline events -> transcript
    analyze.py     web rows (turn timings, usage levels) + traffic / stream sections
    clean.py       delete the raw capture data once decoded
```

### Records (JSONL, one HTTP exchange per line)

`id`, `timestamp`, `method`, `path`, `status`, `duration_s`, `ttfb_s`, `streamed`,
`request` / `response` with `headers` (secrets redacted) and `body`:

- **JSON:** parsed.
- **SSE replies:** split into `sse_events` and rebuilt into the final message in `body`.
- **Protobuf:** decoded without a schema; Connect streams become `{"connect_messages": [...]}`.

Web records add `host`, `reset`, `response.size` and `response.trailers`. Large or
binary bodies are replaced by a short placeholder.

### Transcript

Both builders, `cli/history.py` and `web/timeline.py`, produce `Conversation`s made of:

| Item | Content |
|---|---|
| `Divider` | a call boundary |
| `Message` | user or assistant, made of `Block`s: text, thinking, tool call with its result, collapsed text, other |
| `Panel` | folded reference text: system prompt, tool list |
| `Notice` | a one-line remark |
| `Group` | a folded sub-sequence, e.g. a side call |

Both renderers handle every source, so new sources only need a builder.

### What each replay shows

- **CLI:**
  - Each request re-sends the whole history, so only what's new since the previous main
    call is shown. The comparison ignores moving `cache_control` markers and API-added
    fields.
  - The system prompt and tool list appear when they change.
  - Side calls (session title, prompt suggestion, quota probe) are folded.
  - Tool calls hold their results, taken from the next request.
  - Each reply has a footer with stop reason, tokens, cache share and timings.
- **Web:**
  - User and assistant turns.
  - Thinking shows only its status and title, because the text is never sent to the
    browser.
  - Each reply has a footer with timings (start, thinking, first text, done), the
    number of deltas, and usage-limit levels.

### Analysis

`llmgrab analyze cli|web|all [--csv calls.csv]` (`make cli-analyze`, `web-analyze`,
`analyze`) prints one report over one or both sources. Each source provides:

- **A calls table:** one row per Messages API call (CLI) or assistant turn (web).
  - Common columns: source, local time, conversation, kind, model, status, `ttfb_s`
    (until the first output), `duration_s`, `out_chars`.
  - CLI only: messages, tokens, cache hit, stop reason, tools called.
  - Web only: start delay, thinking time, deltas, 5h / 7d usage levels.
  - Columns a source lacks stay empty, never 0.
- **An exchanges table:** one row per HTTP exchange. Ids in paths (UUIDs, hex,
  numbers) and content-hashed file names are replaced, so similar calls group as one
  endpoint.

Shared sections: calls / turns, per conversation, by kind, errors, HTTP endpoints.
Then each source adds its own:
- **CLI:** tools called, tool-definition sizes.
- **Web:** traffic by category (chat RPC, REST API, telemetry, Cloudflare, assets), and
  `StreamTimeline` polls with their event counts and how each ended.

`--csv` writes the calls table.

## Fragile parts

Most of this rests on observed behavior, not documented interfaces. It was checked
against a handful of captures (2026-10-12/13) and may break when the clients change.

### CLI grab: Claude Code internals

- **Call classification** (`cli/sessions.py`) relies on heuristics:
  - `max_tokens == 1` means a quota probe.
  - A last user message starting with `[SUGGESTION MODE` means a prompt suggestion.
  - A call with tools is a main call; anything else is a side call (title generation,
    `-p` runs).
- **Session ids** are read from the JSON string inside `metadata.user_id`.
- **History diffing** (`cli/history.py`) assumes each main call re-sends the previous
  history unchanged. It ignores `cache_control` (Claude Code moves the marker every
  turn) and string-vs-block content. It compares the previous reply on a few fields
  only, because the API adds e.g. `caller`. Anything else shows as "history rewritten",
  which is also what a compaction would look like.
- **System reminders** are recognized by a `<system-reminder>` prefix.

### Web grab: capture and decryption (`web/pcap.py`)

- **The whole connection must be captured from its first packet.** claude.ai uses one
  long-lived HTTP/2 connection. Without the ClientHello, the keys can't be matched.
  Without the start of the stream, HPACK header compression state is missing. If
  tcpdump starts after the page is open, that connection is lost.
- **Protocols decoded:**
  - Decoded: TLS 1.3 with AES-GCM or ChaCha20, and HTTP/2.
  - Skipped (listed as such in the decode summary): TLS 1.2, HTTP/1.1, QUIC/HTTP/3.
    This is why the profile disables HTTP/3.
- **Packet loss:** a missing TCP segment stops that direction of the stream at the gap.
  The summary flags it as "TCP gap".
- **Key logging:** depends on Firefox honoring `SSLKEYLOGFILE`; the snap package did.
  A Firefox already running with the same profile would ignore it, hence
  `--new-instance`.
- **Root:** the capture needs root; the decoding and everything after it don't.

### Web grab: claude.ai's private API (`web/protobuf.py`, `web/timeline.py`)

- **The API is private, alpha and can change without notice:**
  - The chat ran on Connect-RPC (`anthropic.bard.api.v1alpha.ConversationService`:
    `PerformAction` to send, `StreamTimeline` to receive).
  - It did not use the JSON SSE `/completion` endpoint that many descriptions (and an
    answer from Claude in that very chat) mention.
- **There's no schema.** The field numbers in `timeline.py`'s docstring were inferred
  from one 3-turn conversation. Tools, web search, artifacts, attachments, edits and
  branches were never captured, so their events are unknown. Unknown text blocks still
  show as text; other unknown events are ignored.
- **Schemaless decoding is ambiguous.** The wire format doesn't say whether a
  length-delimited field is text, bytes or a nested message:
  - The decoder treats valid UTF-8 without control characters as text, and otherwise
    tries a nested message.
  - A nested message that happens to look like text stays a string. The thinking title
    arrives as `"\n;Explaining…"`, and `unwrap_text()` strips that prefix.
  - The opposite rule (prefer a message when the text starts with `\n`) was tried and
    reverted: real text deltas starting with a newline were parsed as messages.
  - `fixed64` values are kept as integers. Usage levels are doubles and are converted
    where known (`as_double`).
  - A field becomes a list only when it repeats within one message, hence `as_list()`.
- **Stream merging:**
  - Events from all `StreamTimeline` polls are merged by event id (`<ms>-<seq>`) and
    de-duplicated.
  - A block snapshot replaces the text and a delta appends to it. This relies on
    reconnects re-sending snapshots, as observed.
- **Thinking** text never reaches the browser, so the web replay can't show it.
