#!/bin/sh
set -eu

# Runtime data dir. Defaults to $HOME; override with the GPU_WATCHER_BASE_DIR
# environment variable when the browser data lives elsewhere.
BASE_DIR="${GPU_WATCHER_BASE_DIR:-$HOME/astrbot_data/gpu-price-watcher-browser}"
PORT="${1:-8765}"
# Default bind is loopback only. Set GPU_WATCH_BIND_HOST (for example, your
# LAN or private-tunnel address) to reach the screenshot server from your own
# devices. Do not bind to a public interface.
BIND_IP="${GPU_WATCH_BIND_HOST:-127.0.0.1}"

mkdir -p "$BASE_DIR"
pkill -f "python3 -m http.server $PORT" 2>/dev/null || true

cd "$BASE_DIR"
nohup python3 -m http.server "$PORT" --bind "$BIND_IP" > screenshot_server.log 2>&1 &

echo "started screenshot server"
echo "url: http://$BIND_IP:$PORT/login_status.png"
