#!/usr/bin/env bash
# jq recipes for exploring CLI captures (data/cli/*.jsonl).
#
#   llmgrab jq                    list recipes
#   llmgrab jq <recipe> [N] [ARG] run one recipe (N = record index, default -1 = latest)
#   llmgrab jq all [N]            run every recipe (output truncated to 25 lines each)
#
# Every recipe prints its command first, so you can copy-paste it (from the project root).
# Before pasting, set:
#   LOG='data/cli/*.jsonl'; N=-1; ARG=...
# (records across all files are indexed in order: 0 = oldest, -1 = latest)

SELF="$(realpath "$0")"
cd "$(dirname "$SELF")/../.." || exit 1  # project root
LOG=${LOG:-data/cli/*.jsonl}
N=${2:--1}
ARG=${3:-}

# ---------------------------------------------------------------- envelope / schema

envelope() { # top-level keys of every record, with counts
  jq -c 'keys' $LOG | sort | uniq -c
}

paths() { # every field path (depth <= 4), arrays shown as []
  jq -r 'paths | select(length <= 4) | map(if type == "number" then "[]" else tostring end) | join(".")' $LOG | sort -u
}

types() { # path -> value type, for record N (depth <= 3)
  jq -s ".[$N]" $LOG | jq -r 'paths(scalars or . == [] or . == {}) as $p | select($p | length <= 3)
    | "\($p | map(tostring) | join("."))\t\(getpath($p) | type)"' | sort -u
}

overview() { # one line per call
  jq -c '{t: .timestamp[11:19], path, status, streamed, ttfb_s, duration_s,
          model: .request.body.model, msgs: (.request.body.messages | length),
          tools: (.request.body.tools | length), stop: .response.body.stop_reason}' $LOG
}

sizes() { # bytes of each request part, per call
  jq -c '{t: .timestamp[11:19], system: (.request.body.system | tojson | length),
          tools: (.request.body.tools | tojson | length),
          messages: (.request.body.messages | tojson | length),
          sse_events: (.response.sse_events | length)}' $LOG
}

# ---------------------------------------------------------------- request

req_keys() { # request body keys and the scalar/config params, for record N
  jq -s ".[$N].request.body" $LOG | jq '{keys: keys, model, max_tokens, stream, thinking,
     output_config, context_management, temperature, tool_choice}'
}

headers() { # request + response headers of record N (secrets already redacted)
  jq -s ".[$N] | {request: .request.headers, response: .response.headers}" $LOG
}

betas() { # distinct anthropic-beta flags across all calls, with counts
  jq -r '.request.headers["anthropic-beta"] // empty | split(",")[]' $LOG | sort | uniq -c | sort -rn
}

session() { # session ids: packed in metadata.user_id, else the x-claude-code-session-id header (count_tokens)
  jq -r '(.request.body.metadata.user_id // empty | fromjson | .session_id)
         // .request.headers["x-claude-code-session-id"] // empty' $LOG | sort | uniq -c
}

system() { # system prompt blocks of record N: length, cache_control, preview
  jq -s ".[$N].request.body.system" $LOG |
    jq -c 'if type == "string" then [{type: "text", text: .}] else . end | to_entries[]
      | {i: .key, len: (.value.text | length), cache: .value.cache_control, preview: .value.text[0:100]}'
}

system_full() { # full system prompt text of record N
  jq -rs ".[$N].request.body.system | if type == \"string\" then . else map(.text) | join(\"\n\n----\n\n\") end" $LOG
}

tools() { # tool names + definition size of record N, largest first
  jq -s ".[$N].request.body.tools // []" $LOG |
    jq -r 'map({name, size: (tojson | length)}) | sort_by(-.size)[] | "\(.size)\t\(.name)"'
}

tool_def() { # full definition of one tool: llmgrab jq tool_def -1 Bash
  jq -s ".[$N].request.body.tools[]? | select(.name == \"$ARG\")" $LOG
}

messages() { # conversation structure of record N: role + content block types
  jq -s ".[$N].request.body.messages" $LOG |
    jq -c 'to_entries[] | {i: .key, role: .value.role,
      blocks: (.value.content | if type == "string" then ["string"] else map(.type) end),
      len: (.value.content | tojson | length)}'
}

last_user() { # text of the last user message in record N
  jq -rs ".[$N].request.body.messages | map(select(.role == \"user\")) | last.content
    | if type == \"string\" then . else map(.text // .content // empty | tostring) | join(\"\n---\n\") end" $LOG
}

tool_results() { # tool results sent back to the model in record N
  jq -s ".[$N].request.body.messages" $LOG |
    jq -c '.[] | .content | arrays | .[] | select(.type == "tool_result")
      | {id: .tool_use_id, is_error, len: (.content | tojson | length), preview: (.content | tojson)[0:120]}'
}

# ---------------------------------------------------------------- response

reply() { # reconstructed assistant reply of record N: text, thinking, tool calls
  jq -s ".[$N].response.body.content[]?" $LOG |
    jq -c 'if .type == "text" then {text}
      elif .type == "tool_use" then {tool_use: .name, input}
      elif .type == "thinking" then {thinking: (.thinking // "")[0:200]}
      else {type} end'
}

tool_calls() { # every tool call the model made, across all calls
  jq -c '.timestamp[11:19] as $t | .response.body.content[]? | select(.type == "tool_use")
    | {t: $t, name, input: (.input | tojson)[0:120]}' $LOG
}

usage() { # token usage per call
  jq -c '.response.body.usage // empty | {in: .input_tokens, cache_read: .cache_read_input_tokens,
     cache_write: .cache_creation_input_tokens, out: .output_tokens,
     thinking: .output_tokens_details.thinking_tokens}' $LOG
}

usage_total() { # summed usage over all calls (-s slurps all records into one array)
  jq -s 'map(.response.body.usage // empty) | {calls: length,
     in: (map(.input_tokens // 0) | add), cache_read: (map(.cache_read_input_tokens // 0) | add),
     cache_write: (map(.cache_creation_input_tokens // 0) | add), out: (map(.output_tokens // 0) | add)}' $LOG
}

sse_types() { # SSE event / delta type counts in record N
  jq -s ".[$N].response.sse_events // []" $LOG |
    jq -r '.[] | [.event, (.data.delta.type // "")] | join(" ") ' | sort | uniq -c
}

errors() { # non-2xx calls with their error body
  jq -c 'select(.status >= 400) | {t: .timestamp[11:19], status, path, error: .response.body}' $LOG
}

# ---------------------------------------------------------------- search / compare

grep_calls() { # calls whose request or reply contains ARG: llmgrab jq grep_calls -1 "plan mode"
  jq -c --arg q "$ARG" 'select(tojson | test($q; "i")) | {t: .timestamp[11:19], id, msgs: (.request.body.messages | length)}' $LOG
}

diff_system() { # diff system prompt of record N vs record ARG (default: previous)
  local other=${ARG:-$((N - 1))}
  diff <(jq -rs ".[$other].request.body.system | tojson" $LOG | jq -r '.[]?.text // .') \
       <(jq -rs ".[$N].request.body.system | tojson" $LOG | jq -r '.[]?.text // .') && echo "(identical)"
}

diff_tools() { # tool names added/removed between record ARG (default: previous) and N
  local other=${ARG:-$((N - 1))}
  diff <(jq -rs ".[$other].request.body.tools[]?.name" $LOG | sort) \
       <(jq -rs ".[$N].request.body.tools[]?.name" $LOG | sort) && echo "(identical)"
}

# ---------------------------------------------------------------- runner

RECIPES=(envelope paths types overview sizes req_keys headers betas session system system_full
         tools tool_def messages last_user tool_results reply tool_calls usage usage_total
         sse_types errors grep_calls diff_system diff_tools)

show() { # print a recipe's description and body
  echo "### $1 — $(grep -m1 "^$1() {" "$SELF" | sed 's/.*# //')"
  declare -f "$1" | sed '1,2d;$d' | sed 's/^    //'
  echo "---"
}

case "${1:-}" in
  "")
    echo "usage: llmgrab jq <recipe> [N] [ARG]   |   llmgrab jq all [N]"
    for r in "${RECIPES[@]}"; do printf "  %-14s %s\n" "$r" "$(grep -m1 "^$r() {" "$SELF" | sed 's/.*# //')"; done
    ;;
  all)
    for r in "${RECIPES[@]}"; do
      [[ $r == tool_def || $r == grep_calls ]] && continue  # need ARG
      show "$r"; "$r" 2>&1 | head -n 25; echo
    done
    ;;
  *)
    if [[ " ${RECIPES[*]} " == *" $1 "* ]]; then show "$1"; "$1"; else echo "unknown recipe: $1"; exit 1; fi
    ;;
esac
