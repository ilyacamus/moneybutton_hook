#!/bin/bash
# Runs the private Telegram bot on this machine (and, with SCOUT_ENABLED=1, the scout inside it).
# Started by launchd (macOS), systemd (Linux) or by hand: ./scripts/run_bot.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
mkdir -p data/logs
exec .venv/bin/python -m telegram_bot >> data/logs/bot.log 2>&1
