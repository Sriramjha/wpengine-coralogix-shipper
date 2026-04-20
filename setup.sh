#!/usr/bin/env bash
# One-time local setup: Python venv + dependencies + .env template.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env — edit it with your WP Engine and Coralogix credentials."
else
  echo ".env already exists; not overwriting."
fi

echo ""
echo "Next:"
echo "  1. Edit .env"
echo "  2. ./run.sh --dry-run"
echo "  3. ./run.sh"
