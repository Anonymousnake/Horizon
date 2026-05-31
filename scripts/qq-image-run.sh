#!/usr/bin/env bash
# Run Horizon and send the latest Chinese briefing to QQ as an image.
# Intended for cron on the deployment host.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="${PROJECT_DIR}/logs"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

GROUP_UMO="${HORIZON_QQ_GROUP_UMO:-napcat_onebot_v11:GroupMessage:951944306}"
HOURS="${HORIZON_RUN_HOURS:-24}"
FETCH_CONCURRENCY="${HORIZON_RSS_FETCH_CONCURRENCY:-16}"

if [[ -z "${ASTRBOT_BASE_URL:-}" ]]; then
  GATEWAY="$(docker network inspect horizon_default -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
  ASTRBOT_BASE_URL="http://${GATEWAY:-172.22.0.1}:6185"
fi

echo "$LOG_PREFIX Horizon QQ image run started."
echo "$LOG_PREFIX hours=${HOURS} group=${GROUP_UMO} astrobot=${ASTRBOT_BASE_URL}"

docker compose run --rm \
  -e HORIZON_RSS_FETCH_CONCURRENCY="$FETCH_CONCURRENCY" \
  horizon --hours "$HOURS"

docker compose run --rm --entrypoint uv horizon \
  run horizon-send-image \
  --send \
  --base-url "$ASTRBOT_BASE_URL" \
  --umo "$GROUP_UMO"

echo "$LOG_PREFIX Horizon QQ image run done."
