#!/usr/bin/env bash
# Points Heimdall at a hosted model that speaks the OpenAI chat-completions
# protocol (Gemini, OpenRouter, Groq, Mistral, ...) instead of the local one.
#
#   HEIMDALL_LLM_API_KEY=... infra/llm-api.sh <base-url> <model> [seconds-between-requests]
#   infra/llm-api.sh --remove
#
# The key is read from the environment, never from the command line, and is
# stored only in the Secret `heimdall-llm` in the cluster. Run `make up`
# afterwards: it switches the provider and restarts Heimdall.
#
# Examples of <base-url> <model>:
#   https://generativelanguage.googleapis.com/v1beta/openai  gemini-2.5-flash
#   https://openrouter.ai/api/v1                             <vendor>/<model>:free
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

if [ "${1:-}" = "--remove" ]; then
  k -n heimdall delete secret heimdall-llm --ignore-not-found >/dev/null
  log "hosted model removed; run 'make up' to go back to the local one"
  exit 0
fi

[ $# -ge 2 ] || die "usage: HEIMDALL_LLM_API_KEY=... $0 <base-url> <model> [seconds-between-requests]"
[ -n "${HEIMDALL_LLM_API_KEY:-}" ] || die "set HEIMDALL_LLM_API_KEY in the environment"
case "$1" in https://*) ;; *) die "the base URL must start with https://" ;; esac

# The key goes in through a file descriptor, so it never appears in a process
# list or in the shell's trace output.
k -n heimdall create secret generic heimdall-llm \
  --from-literal=base_url="$1" \
  --from-literal=model="$2" \
  --from-literal=min_interval_seconds="${3:-0}" \
  --from-file=api_key=<(printf '%s' "$HEIMDALL_LLM_API_KEY") \
  --dry-run=client -o yaml | k apply -f - >/dev/null
log "hosted model set to $2 at $1; run 'make up' to switch Heimdall to it"
