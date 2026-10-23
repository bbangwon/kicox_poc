#!/usr/bin/env bash
# MCP 서버 2개 + FastAPI 백엔드 + Vite 프론트엔드를 한 번에 실행한다. Ctrl+C로 모두 종료.
set -euo pipefail
cd "$(dirname "$0")/.."
# 다른 프로젝트의 가상환경이 활성화되어 있어도 이 프로젝트의 .venv를 쓰도록 한다.
unset VIRTUAL_ENV

# 이전 실행이 남아 있으면 포트를 점유한 프로세스를 종료하고 다시 띄운다.
PORTS=(8101 8102 8000 5173)
for port in "${PORTS[@]}"; do
  if pids=$(lsof -ti "tcp:$port" -sTCP:LISTEN); then
    echo "포트 $port 사용 중인 프로세스 종료: $pids"
    kill $pids
  fi
done
for _ in $(seq 1 20); do
  busy=0
  for port in "${PORTS[@]}"; do
    lsof -ti "tcp:$port" -sTCP:LISTEN >/dev/null && busy=1
  done
  [ "$busy" = 0 ] && break
  sleep 0.5
done

trap 'kill 0' EXIT

uv run python -m mcp_servers.docs_server &
uv run python -m mcp_servers.training_server &
sleep 2
uv run uvicorn backend.app:app --port 8000 --reload --reload-dir backend &
(cd web && npm run dev -- --port 5173) &

wait
