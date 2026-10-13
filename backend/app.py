"""FastAPI 서버: 그래프 구조 조회 + 실행 이벤트 SSE 스트리밍."""

import json
import time
import uuid

from fastapi import FastAPI, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend import config as settings
from backend.graph import build_graph, load_mcp_tools

app = FastAPI(title="DGX PoC Agent")

_runtime: dict = {}


async def _get_runtime() -> dict:
    """MCP 도구를 최초 요청 시 로드한다 (MCP 서버가 늦게 떠도 백엔드가 기동되도록)."""
    if not _runtime:
        try:
            tools, servers = await load_mcp_tools()
        except Exception as e:
            raise HTTPException(503, f"MCP 서버 연결 실패: {e}") from e
        _runtime.update(tools=tools, servers=servers, graph=build_graph(tools, servers))
    return _runtime


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None
    thinking: bool = True


@app.get("/api/graph")
async def graph_info():
    rt = await _get_runtime()
    g = rt["graph"].get_graph()
    return {
        "model": settings.MODEL,
        "nodes": [{"id": n} for n in g.nodes],
        "edges": [{"source": e.source, "target": e.target, "conditional": e.conditional} for e in g.edges],
        "mermaid": g.draw_mermaid(),
        "servers": [{"name": name, "url": c["url"]} for name, c in settings.MCP_SERVERS.items()],
        "tools": [
            {"name": t.name, "server": rt["servers"][t.name], "description": t.description.splitlines()[0]}
            for t in rt["tools"]
        ],
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    rt = await _get_runtime()
    thread_id = req.thread_id or uuid.uuid4().hex
    run_config = {"configurable": {"thread_id": thread_id, "thinking": req.thinking}, "recursion_limit": 25}

    async def events():
        started = time.time()
        yield {"data": json.dumps({"type": "run_start", "ts": started, "thread_id": thread_id})}
        try:
            async for event in rt["graph"].astream(
                {"messages": [HumanMessage(req.message)]}, run_config, stream_mode="custom"
            ):
                yield {"data": json.dumps(event, ensure_ascii=False, default=str)}
        except Exception as e:
            yield {"data": json.dumps({"type": "error", "ts": time.time(), "message": str(e)}, ensure_ascii=False)}
        yield {"data": json.dumps({"type": "run_end", "ts": time.time(), "ms": round((time.time() - started) * 1000)})}

    return EventSourceResponse(events())
