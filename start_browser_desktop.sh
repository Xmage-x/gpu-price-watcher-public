#!/bin/sh
set -eu

# Runtime data dir. Defaults to $HOME; override with the GPU_WATCHER_BASE_DIR
# environment variable when the browser data lives elsewhere.
BASE_DIR="${GPU_WATCHER_BASE_DIR:-$HOME/astrbot_data/gpu-price-watcher-browser}"
# Repository checkout that contains tcp_forward.py.
REPO_DIR="${GPU_WATCHER_REPO_DIR:-$HOME/gpu-price-watcher}"
# Bind address for the noVNC bridge. Defaults to loopback only; set BIND_HOST
# (for example, a private-tunnel or LAN address) to reach it from your other
# devices. Do not bind to a public interface.
BIND_HOST="${BIND_HOST:-127.0.0.1}"
HOST_PORT="${1:-8766}"
PLATFORM="${2:-淘宝搜索}"
CONTAINER_PORT="6080"
PASS_FILE="$BASE_DIR/vnc_password.txt"
DISPLAY_NUM="100"
DISPLAY_VALUE=":$DISPLAY_NUM"

echo "BIND_HOST is $BIND_HOST"

docker exec astrbot mkdir -p /AstrBot/data/gpu-price-watcher-browser
pkill -f "flock /tmp/browser-market-watcher.lock sleep" 2>/dev/null || true
nohup sh -c 'flock /tmp/browser-market-watcher.lock sleep 7300' > "$BASE_DIR/desktop_profile_lock.log" 2>&1 &

docker exec -i astrbot sh -s <<'SH'
for proc in /proc/[0-9]*; do
  pid="${proc##*/}"
  cmd="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
  case "$cmd" in
    *browser_desktop.py*|*browser_login_helper.py*|*browser_market_watcher.py*|*websockify*|*x11vnc*|*Xtigervnc*|*Xvnc*|*'Xvfb :'*|*chrome-linux64/chrome*|*chrome-headless-shell*)
      kill -9 "$pid" 2>/dev/null || true
      ;;
  esac
done
SH
sleep 2
docker exec astrbot chown -R 1000:1000 /AstrBot/data/gpu-price-watcher-browser
docker exec astrbot sh -lc "rm -f /tmp/.X${DISPLAY_NUM}-lock /tmp/.X11-unix/X${DISPLAY_NUM} /AstrBot/data/gpu-price-watcher-browser/browser_profile/SingletonLock /AstrBot/data/gpu-price-watcher-browser/browser_profile/SingletonCookie /AstrBot/data/gpu-price-watcher-browser/browser_profile/SingletonSocket 2>/dev/null || true"

if [ ! -s "$PASS_FILE" ]; then
  umask 077
  tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16 > "$PASS_FILE"
  printf '\n' >> "$PASS_FILE"
fi
VNC_PASS="$(tr -d '\r\n' < "$PASS_FILE")"
docker exec astrbot sh -lc "x11vnc -storepasswd '$VNC_PASS' /AstrBot/data/gpu-price-watcher-browser/x11vnc.pass >/dev/null"

docker exec -d astrbot sh -lc "cd /AstrBot/data/gpu-price-watcher-browser && Xtigervnc $DISPLAY_VALUE -localhost -rfbport 5900 -geometry 1024x720 -depth 24 -SecurityTypes VncAuth -PasswordFile /AstrBot/data/gpu-price-watcher-browser/x11vnc.pass > desktop_tigervnc.log 2>&1"
sleep 2
docker exec -d astrbot sh -lc 'cd /AstrBot/data/gpu-price-watcher-browser && websockify --web /usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 > desktop_novnc.log 2>&1'
sleep 2
docker exec -d astrbot sh -lc "cd /AstrBot/data/gpu-price-watcher-browser && DISPLAY=$DISPLAY_VALUE python browser_desktop.py --config browser_market_config.json --platform '$PLATFORM' --limit 1 --seconds 7200 > desktop_browser.log 2>&1"

CONTAINER_IP="$(docker inspect astrbot --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')"
pkill -f "tcp_forward.py --listen-host $BIND_HOST --listen-port $HOST_PORT" 2>/dev/null || true
nohup python3 "$REPO_DIR/tcp_forward.py" --listen-host "$BIND_HOST" --listen-port "$HOST_PORT" --target-host "$CONTAINER_IP" --target-port "$CONTAINER_PORT" > "$BASE_DIR/desktop_forward.log" 2>&1 &

echo "noVNC desktop started"
echo "platform: $PLATFORM"
echo "url: http://$BIND_HOST:$HOST_PORT/vnc.html?host=$BIND_HOST&port=$HOST_PORT&autoconnect=true&resize=scale"
echo "password: $VNC_PASS"
echo "logs: $BASE_DIR/desktop_*.log"
