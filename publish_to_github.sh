#!/usr/bin/env bash
# Create github.com/Sriramjha/wpengine-coralogix-shipper (if missing) and push main.
# One-time setup: brew install gh && gh auth login
set -euo pipefail
cd "$(dirname "$0")"

export PATH="/opt/homebrew/bin:${PATH}"

if ! command -v gh >/dev/null 2>&1; then
  echo "Install GitHub CLI: brew install gh" >&2
  exit 1
fi

if ! gh auth status -h github.com >/dev/null 2>&1; then
  echo "Not logged in. Run: gh auth login" >&2
  exit 1
fi

OWNER="Sriramjha"
REPO="wpengine-coralogix-shipper"
REMOTE_SSH="git@github.com:${OWNER}/${REPO}.git"

git remote remove origin 2>/dev/null || true
git remote add origin "$REMOTE_SSH"

if git ls-remote "$REMOTE_SSH" >/dev/null 2>&1; then
  echo "Remote repo exists; pushing..."
else
  echo "Creating ${OWNER}/${REPO} on GitHub..."
  gh repo create "${REPO}" --public --description "Poll WP Engine logs and ship to Coralogix"
fi

GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new" git push -u origin main
echo "Done: https://github.com/${OWNER}/${REPO}"
