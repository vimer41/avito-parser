#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
TARGET_DIR=""
for candidate in /opt/avito_parser_bot /opt/avito-bot; do
    if [[ -d "$candidate/.git" ]]; then TARGET_DIR="$candidate"; break; fi
done
[[ -n "$TARGET_DIR" ]] || { echo "Project directory not found"; exit 1; }
cd "$TARGET_DIR"
echo "Installed SHA: $(git rev-parse HEAD)"
echo "Active environment: $(readlink -f venv)"
systemctl show avito-bot --property=ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp,MemoryCurrent,CPUUsageNSec,TasksCurrent
free -m
df -h "$TARGET_DIR"
du -sh .releases backups data logs 2>/dev/null || true
PID="$(systemctl show avito-bot --property=MainPID --value)"
[[ "$PID" =~ ^[1-9][0-9]*$ ]] || { echo "Bot process not running"; exit 1; }
ps -p "$PID" -o pid=,etime=,%cpu=,%mem=,rss=,nlwp=
./venv/bin/python -B tools/diagnose.py
INITIAL_RESTARTS="$(systemctl show avito-bot --property=NRestarts --value)"
for _ in $(seq 1 6); do
    sleep 10
    systemctl is-active --quiet avito-bot
    [[ "$(systemctl show avito-bot --property=NRestarts --value)" == "$INITIAL_RESTARTS" ]]
    ps -p "$PID" -o pid=,etime=,%cpu=,%mem=,rss=,nlwp=
done
echo "Observed active process for 60 seconds without new restarts"
./venv/bin/python -B tools/diagnose.py ${DIAGNOSTICS_NOTIFY_ADMIN:+--notify-admin}