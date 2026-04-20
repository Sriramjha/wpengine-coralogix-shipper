#!/usr/bin/env bash
# Run shipper with variables from .env (if present).
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Run:  chmod +x setup.sh && ./setup.sh" >&2
  exit 1
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck source=/dev/null
  source .env
  set +a
fi

exec .venv/bin/python wpengine_logs_to_coralogix.py "$@"
