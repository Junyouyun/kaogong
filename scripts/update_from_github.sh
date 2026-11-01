#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${KAOGONG_APP_DIR:-/root/autodl-tmp/kaogong}"
REPO_ZIP="${KAOGONG_REPO_ZIP:-https://codeload.github.com/Junyouyun/kaogong/zip/refs/heads/main}"
TEMP_DIR="$(mktemp -d /root/autodl-tmp/kaogong-update.XXXXXX)"
trap 'rm -rf "$TEMP_DIR"' EXIT

curl -fL --retry 3 --connect-timeout 20 "$REPO_ZIP" -o "$TEMP_DIR/kaogong.zip"
unzip -q "$TEMP_DIR/kaogong.zip" -d "$TEMP_DIR/source"
SRC="$(find "$TEMP_DIR/source" -mindepth 1 -maxdepth 1 -type d | head -n 1)"

cp -a "$SRC/backend" "$SRC/web" "$SRC/scripts" "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/DEVLOG.md" "$APP_DIR/"
if [ -f "$APP_DIR/server.pid" ]; then
  kill "$(cat "$APP_DIR/server.pid")" 2>/dev/null || true
  sleep 1
fi
cd "$APP_DIR"
if [ -f .secrets/setup_token ]; then
  export KAOGONG_SETUP_TOKEN="$(cat .secrets/setup_token)"
fi
nohup python -m uvicorn backend.kaogong_api.main:app --host 0.0.0.0 --port 6006 > server.log 2>&1 < /dev/null &
echo $! > server.pid
sleep 2
curl -fsS http://127.0.0.1:6006/api/health
