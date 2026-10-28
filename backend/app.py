"""FastAPI 서버: 화면 정보 조회 + 실행 이벤트 SSE 스트리밍."""

import json
import time
import uuid

from fastapi import FastAPI, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from backend import config as settings
from backend import search
from backend.graph import build_graph

app = FastAPI(title="KICOX 내부규정 Q&A PoC")

graph = build_graph()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = None
    thinking: bool = False


@app.get("/api/info")
async def info():
    try:
        documents = await search.documents()
    except Exception as e:
        raise HTTPException(503, f"PostgreSQL 연결 실패: {e}") from e
    return {
        "model": settings.MODEL,
        "dense": search.dense_enabled(),
        "documents": documents,
        "nodes": [n for n in graph.get_graph().nodes if not n.startswith("__")],
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    thread_id = req.thread_id or uuid.uuid4().hex
    run_config = {"configurable": {"thread_id": thread_id, "thinking": req.thinking}, "recursion_limit": 25}

    async def events():
        started = time.time()
        yield {"data": json.dumps({"type": "run_start", "ts": started, "thread_id": thread_id})}
        try:
            async for event in graph.astream(
                {"messages": [HumanMessage(req.message)]}, run_config, stream_mode="custom"
            ):
                yield {"data": json.dumps(event, ensure_ascii=False, default=str)}
        except Exception as e:
            yield {"data": json.dumps({"type": "error", "ts": time.time(), "message": str(e)}, ensure_ascii=False)}
        yield {"data": json.dumps({"type": "run_end", "ts": time.time(), "ms": round((time.time() - started) * 1000)})}

    return EventSourceResponse(events())
