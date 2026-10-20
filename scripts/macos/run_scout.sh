#!/bin/bash
# Runs the autonomous scout on this Mac: own collector (home IP, no login) → first 3 s → Claude → learning.
# Started by launchd (com.moneybutton.scout.plist) or by hand: ./scripts/macos/run_scout.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
export IG_SOURCE="${IG_SOURCE:-web}"
mkdir -p data/logs
exec .venv/bin/hookbot scout run >> data/logs/scout.log 2>&1
