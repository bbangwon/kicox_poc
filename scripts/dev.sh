#!/usr/bin/env bash
# MCP 서버 2개 + FastAPI 백엔드 + Vite 프론트엔드를 한 번에 실행한다. Ctrl+C로 모두 종료.
set -euo pipefail
cd "$(dirname "$0")/.."

trap 'kill 0' EXIT

uv run python -m mcp_servers.docs_server &
uv run python -m mcp_servers.ops_server &
sleep 2
uv run uvicorn backend.app:app --port 8000 --reload --reload-dir backend &
(cd web && npm run dev -- --port 5173) &

wait
