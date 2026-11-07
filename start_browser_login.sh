#!/bin/sh
set -eu

PLATFORM="${1:-淘宝搜索}"
# Runtime data dir. Defaults to $HOME; override with the GPU_WATCHER_BASE_DIR
# environment variable when the browser data lives elsewhere.
BASE_DIR="${GPU_WATCHER_BASE_DIR:-$HOME/astrbot_data/gpu-price-watcher-browser}"
LOG_FILE="$BASE_DIR/login_helper.log"

mkdir -p "$BASE_DIR"
pkill -f "browser_login_helper.py" 2>/dev/null || true

nohup docker exec astrbot sh -lc "cd /AstrBot/data/gpu-price-watcher-browser && xvfb-run -a python browser_login_helper.py --config browser_market_config.json --platform '$PLATFORM' --seconds 900 --screenshot login_status.png" >"$LOG_FILE" 2>&1 &

echo "started login helper for $PLATFORM"
echo "log: $LOG_FILE"
echo "screenshot: $BASE_DIR/login_status.png"
