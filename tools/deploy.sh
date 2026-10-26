#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

: "${DEPLOY_SHA:?DEPLOY_SHA is required}"
: "${DEPLOY_BRANCH:?DEPLOY_BRANCH is required}"
[[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid commit SHA"; exit 1; }
[[ "$DEPLOY_BRANCH" == main || "$DEPLOY_BRANCH" == feature/json-api ]] || { echo "Invalid deploy branch"; exit 1; }

TARGET_DIR=""
for candidate in "${DEPLOY_TARGET_DIR:-}" /opt/avito_parser_bot /opt/avito-bot; do
    if [[ -d "$candidate/.git" ]]; then
        TARGET_DIR="$candidate"
        break
    fi
done
[[ -n "$TARGET_DIR" ]] || { echo "Project directory not found"; exit 1; }
cd "$TARGET_DIR"

# Prevent overlapping remote installs/restarts, including manually started workflows.
exec 9>"$TARGET_DIR/.deploy.lock"
flock -w 900 9 || { echo "Deploy lock timeout"; exit 1; }
git diff --quiet && git diff --cached --quiet || { echo "Tracked server changes found; refusing overwrite"; exit 1; }
git fetch --no-tags origin "$DEPLOY_BRANCH"
git cat-file -e "$DEPLOY_SHA^{commit}"
git merge-base --is-ancestor "$DEPLOY_SHA" FETCH_HEAD || { echo "Commit is not on requested branch"; exit 1; }
[[ "$(git rev-parse FETCH_HEAD)" == "$DEPLOY_SHA" ]] || { echo "Newer branch commit found; skipping stale deploy"; exit 0; }

PREVIOUS_SHA="$(git rev-parse HEAD)"
PREVIOUS_BRANCH="$(git symbolic-ref --short -q HEAD || true)"
NEW_ENV=""
STAGE_DIR="$(mktemp -d)"
ENV_CHANGED=0
PREVIOUS_ENV=""
WAS_ACTIVE=0
CHANGED=0
systemctl is-active --quiet avito-bot && WAS_ACTIVE=1

cleanup() {
    rm -rf "$STAGE_DIR"
}

rollback() {
    status="${1:-$?}"
    trap - ERR INT TERM
    if [[ "$CHANGED" == 1 ]]; then
        echo "Deploy failed; restoring previous code/environment"
        systemctl stop avito-bot || true
        if [[ -n "$PREVIOUS_BRANCH" ]]; then
            git checkout -B "$PREVIOUS_BRANCH" "$PREVIOUS_SHA" || true
        else
            git checkout --detach "$PREVIOUS_SHA" || true
        fi
        if [[ "$ENV_CHANGED" == 1 ]]; then
            rm -f "$TARGET_DIR/venv"
            if [[ -n "$PREVIOUS_ENV" ]]; then
                mv "$PREVIOUS_ENV" "$TARGET_DIR/venv" || true
            fi
        fi
        if [[ "$WAS_ACTIVE" == 1 ]]; then
            systemctl start avito-bot || true
        fi
    fi
    exit "$status"
}
trap cleanup EXIT
trap rollback ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM

# Build and retest in isolation. The running bot keeps its existing environment.
git archive "$DEPLOY_SHA" | tar -x -C "$STAGE_DIR"
mkdir -p "$TARGET_DIR/.releases"
NEW_ENV="$(mktemp -d "$TARGET_DIR/.releases/venv-$DEPLOY_SHA-XXXXXX")"
python3.11 -m venv "$NEW_ENV" 2>/dev/null || python3 -m venv "$NEW_ENV"
"$NEW_ENV/bin/python" -m pip install --disable-pip-version-check -r "$STAGE_DIR/requirements.txt"
"$NEW_ENV/bin/python" -m pip check
"$NEW_ENV/bin/python" -B "$STAGE_DIR/tools/check_syntax.py"
AVITO_RADAR_TESTING=1 PYTHONDONTWRITEBYTECODE=1 timeout 180 "$NEW_ENV/bin/python" -B "$STAGE_DIR/tools/run_offline_tests.py"

# No automatic deletion or log scrubbing. Backup is read-only and precedes migrations.
CHANGED=1
systemctl stop avito-bot
mkdir -p "$TARGET_DIR/backups"
BACKUP_PATH="$TARGET_DIR/backups/bot-before-$DEPLOY_SHA-$(date -u +%Y%m%dT%H%M%SZ).db"
export BACKUP_PATH TARGET_DIR
PYTHONPATH="$STAGE_DIR" "$NEW_ENV/bin/python" -B - <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.environ['TARGET_DIR'])
import config

database_path = Path(config.DB_PATH)
if database_path.is_file():
    with sqlite3.connect(database_path.resolve().as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(os.environ['BACKUP_PATH']) as destination:
            source.backup(destination)
    print('SQLite backup created')
else:
    print('No existing database; first deployment')
PY

git checkout -B "$DEPLOY_BRANCH" "$DEPLOY_SHA"
if [[ -e "$TARGET_DIR/venv" || -L "$TARGET_DIR/venv" ]]; then
    PREVIOUS_ENV="$TARGET_DIR/.releases/previous-venv-$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$TARGET_DIR/venv" "$PREVIOUS_ENV"
fi
ENV_CHANGED=1
ln -s "$NEW_ENV" "$TARGET_DIR/venv"
systemctl reset-failed avito-bot
systemctl start avito-bot

# A successful process start alone is not a successful deployment.
for _ in $(seq 1 10); do
    sleep 2
    systemctl is-active --quiet avito-bot
    [[ "$(systemctl show avito-bot --property=NRestarts --value)" == 0 ]]
done
[[ "$(git rev-parse HEAD)" == "$DEPLOY_SHA" ]]
echo "Deployed tested commit $DEPLOY_SHA; service stayed active for 20 seconds"