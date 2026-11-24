#!/usr/bin/env bash
# Run a gateway RPC script with the bundled Hermes venv and sources.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: run_with_gateway.sh <script.py> [args...]" >&2
  exit 2
fi

resolve_payload() {
  if [[ -n "${HERMES_AGENT_PAYLOAD:-}" && -d "${HERMES_AGENT_PAYLOAD}" ]]; then
    echo "${HERMES_AGENT_PAYLOAD}"
    return
  fi
  local app
  for app in "/Applications/Hermes Agent Canary.app" "/Applications/Hermes Agent.app" "/Applications/Hermes.app"; do
    local p="$app/Contents/Resources/agent-payload"
    if [[ -d "$p/hermes-agent" ]]; then
      echo "$p"
      return
    fi
  done
  echo "Cannot find agent-payload; set HERMES_AGENT_PAYLOAD" >&2
  exit 1
}

PAYLOAD=$(resolve_payload)
PY="$PAYLOAD/venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "No executable Hermes venv Python at $PY" >&2
  exit 1
fi

export PYTHONPATH="$PAYLOAD/hermes-agent${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" "$@"
