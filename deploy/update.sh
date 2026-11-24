#!/usr/bin/env bash
# Runs as the non-root owner of the Git checkout, via a systemd --user timer.
# Never overwrites .env, data/, or virtualenv; those are ignored by Git.
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# Avoid concurrent updates, e.g. an organizer manually triggers the updater.
mkdir -p data
exec 9>data/.deployment.lock
if ! flock -n 9; then
  echo "Update already running; skipping."
  exit 0
fi

if [[ ! -d .git ]]; then
  echo "No .git directory found. Clone from GitHub before enabling the updater." >&2
  exit 2
fi
if [[ ! -x .venv/bin/python ]]; then
  echo "Missing virtual environment; create .venv first." >&2
  exit 2
fi

# Only GitHub commits should change tracked production code.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Tracked files modified on Oracle; refusing automatic update." >&2
  exit 1
fi

git fetch --quiet origin master
CURRENT="$(git rev-parse HEAD)"
LATEST="$(git rev-parse refs/remotes/origin/master)"
if [[ "$CURRENT" == "$LATEST" ]]; then
  echo "No update (commit $CURRENT)."
  exit 0
fi
if ! git merge-base --is-ancestor "$CURRENT" "$LATEST"; then
  echo "Remote history is not a fast-forward; refusing update." >&2
  exit 1
fi

# Change the source only after ensuring the branch is a forward update.
git merge --ff-only "$LATEST"

if ! ( .venv/bin/python -m pip install -r requirements.txt     && .venv/bin/python -m compileall -q bot.py core scripts     && .venv/bin/python -m unittest discover -s tests -v ); then
  echo "Tests/install failed; reverting tracked code to $CURRENT." >&2
  git reset --hard "$CURRENT"
  .venv/bin/python -m pip install -r requirements.txt || true
  exit 1
fi

systemctl --user restart guild-raid-bot.service
# Catch immediate startup failure before reporting a successful deployment.
sleep 3
if ! systemctl --user is-active --quiet guild-raid-bot.service; then
  echo "Bot did not stay active. Check journalctl --user -u guild-raid-bot.service." >&2
  # Roll back the source, then try to restore the previous working service.
  git reset --hard "$CURRENT"
  .venv/bin/python -m pip install -r requirements.txt || true
  systemctl --user restart guild-raid-bot.service || true
  exit 1
fi

echo "Deployed $LATEST and restarted guild-raid-bot.service."
