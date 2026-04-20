#!/usr/bin/env bash
# Create github.com/Sriramjha/wpengine-coralogix-shipper (if missing) and push main.
# One-time setup: brew install gh && gh auth login
set -euo pipefail
cd "$(dirname "$0")"

export PATH="/opt/homebrew/bin:${PATH}"

OWNER="Sriramjha"
REPO="wpengine-coralogix-shipper"
REMOTE_SSH="git@github.com:${OWNER}/${REPO}.git"

git remote remove origin 2>/dev/null || true
git remote add origin "$REMOTE_SSH"

if git ls-remote "$REMOTE_SSH" >/dev/null 2>&1; then
  echo "Remote repo exists; pushing..."
elif [[ -n "${GH_TOKEN:-}" ]]; then
  echo "Creating ${OWNER}/${REPO} via GitHub API (GH_TOKEN)..."
  code="$(curl -sS -o /tmp/gh_create_repo.json -w '%{http_code}' -X POST \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    "https://api.github.com/user/repos" \
    -d "{\"name\":\"${REPO}\",\"description\":\"Poll WP Engine logs and ship to Coralogix\",\"private\":false,\"auto_init\":false}")"
  if [[ "$code" != "201" ]]; then
    echo "GitHub API returned HTTP $code: $(cat /tmp/gh_create_repo.json)" >&2
    exit 1
  fi
elif command -v gh >/dev/null 2>&1 && gh auth status -h github.com >/dev/null 2>&1; then
  echo "Creating ${OWNER}/${REPO} on GitHub (gh)..."
  gh repo create "${REPO}" --public --description "Poll WP Engine logs and ship to Coralogix"
else
  echo "Either:" >&2
  echo "  1) Run: gh auth login   (after: brew install gh)" >&2
  echo "  2) Or set GH_TOKEN (classic PAT: repo scope) and re-run this script" >&2
  exit 1
fi

GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new" git push -u origin main
echo "Done: https://github.com/${OWNER}/${REPO}"
