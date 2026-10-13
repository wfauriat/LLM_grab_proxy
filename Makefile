# Shortcuts for the llmgrab commands (`.venv/bin/llmgrab` lists them all).
# Extra arguments go through ARGS:  make cli-replay ARGS="--hide-side --full"
LLMGRAB := .venv/bin/llmgrab

.DEFAULT_GOAL := help
.PHONY: help install html \
        cli-proxy cli-claude cli-replay cli-html cli-analyze cli-jq \
        web-browser web-tcpdump web-decode web-replay web-html web-clean

help: ## list targets
	@awk 'BEGIN {FS = ":.*## "} \
	     /^## / {printf "\n%s\n", substr($$0, 4)} \
	     /^[a-z-]+:.*## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## create .venv and install llmgrab (editable) with its dependencies
	test -x .venv/bin/python || uv venv --python 3.12 .venv
	uv pip install --python .venv/bin/python -e .

html: cli-html web-html ## both HTML replays

## CLI grab: Claude Code through a local reverse proxy (data/cli/)
cli-proxy: ## terminal 1: logging proxy on 127.0.0.1:8080
	$(LLMGRAB) proxy $(ARGS)

cli-claude: ## terminal 2: Claude Code through the proxy
	$(LLMGRAB) claude $(ARGS)

cli-replay: ## terminal replay (make -s cli-replay | less -R)
	$(LLMGRAB) replay cli $(ARGS)

cli-html: ## HTML replay -> render/replay.html
	$(LLMGRAB) replay cli --html $(ARGS)

cli-analyze: ## report: calls, tokens, cache, latency, tools (ARGS="--csv calls.csv")
	$(LLMGRAB) analyze $(ARGS)

cli-jq: ## jq recipes (ARGS="<recipe> [N] [ARG]"; none: list them) ; use e.g. make -s cli-jq ARGS="overview"
	$(LLMGRAB) jq $(ARGS)

## Web grab: claude.ai in Firefox, passive packet capture (data/web/)
web-browser: ## Firefox profile logging its TLS keys
	$(LLMGRAB) browser $(ARGS)

web-tcpdump: ## print the sudo tcpdump line to run in parallel
	@$(LLMGRAB) tcpdump

web-decode: ## decrypt claude.pcap + keys.log -> claude.jsonl
	$(LLMGRAB) decode $(ARGS)

web-replay: ## terminal replay
	$(LLMGRAB) replay web $(ARGS)

web-html: ## HTML replay -> render/web_replay.html
	$(LLMGRAB) replay web --html $(ARGS)

web-clean: ## after decode: delete pcap, keys.log, Firefox profile (asks first; ARGS="--keep-profile")
	@$(LLMGRAB) clean $(ARGS)
