#!/usr/bin/env bash
set -euo pipefail

cd /root/autodl-tmp/kaogong
if [ -f .secrets/setup_token ]; then
  export KAOGONG_SETUP_TOKEN="$(cat .secrets/setup_token)"
fi
python -m uvicorn backend.kaogong_api.main:app \
  --host 0.0.0.0 \
  --port "${KAOGONG_PORT:-6006}"
