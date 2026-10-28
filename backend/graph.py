"""LangGraph RAG 질의 처리: analyze → route → plan → read → answer (설계 노트 §4, RAG_FLOW_OVERVIEW §4).

- analyze: 범위 밖 판정 · 검색용 질의 재작성 (짧은 LLM 호출, thinking off)
- route: 문서 카탈로그 하이브리드 검색 → router LLM이 문서 최대 3개 선택
- plan: 선택 문서의 parent 요약 · 청크 적중 표시를 보고 planner LLM이 읽을 child 선택.
  근거가 없으면 고른 문서를 빼고 route로 1회 되돌아간다 (§4.1 3-1)
- read: child 원문 + 앞뒤 이웃 조회, 근거 번호 부여
- answer: 근거만으로 스트리밍 답변, 문장마다 [n] 인용
- no_reference: 참조문서 없음 안내

문서별 도구 · 역질문은 승인된 도구가 생기면 plan 뒤에 붙인다.
각 노드는 `get_stream_writer()`로 화면용 커스텀 이벤트를 내보낸다 (README 참고).
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from functools import wraps
from typing import Annotated, Literal

import httpx
import openai
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from backend import config as settings
from backend import search
from backend.llm import ChatVLLM

MAX_DOCS = 3
MAX_CHUNKS = 6
MAX_REROUTES = 1
HISTORY_MESSAGES = 6  # analyze · answer에 넘기는 이전 대화 (사용자 · 답변 합계)


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    queries: list[str]
    selected_docs: list[str]
    excluded_docs: list[str]
    reroutes: int
    chunk_ids: list[str]
    evidence: list[dict]
    no_reference: str | None  # 참조문서 없음 사유
    out_of_scope: bool


def _llm(thinking: bool) -> ChatVLLM:
    if not settings.LLM_BASE_URL:
        raise RuntimeError("LLM_BASE_URL이 설정되지 않았습니다 (worker-ai-endpoints.env 또는 환경변수)")
    return ChatVLLM(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        model=settings.MODEL,
        temperature=settings.TEMPERATURE,
        max_tokens=settings.MAX_TOKENS,
        timeout=settings.LLM_TIMEOUT,
        max_retries=0,  # 재시도는 _with_retry가 담당한다 (llm_retry 이벤트 발행)
        stream_usage=True,
        extra_body={"chat_template_kwargs": {"thinking": thinking}},
    )


def _emit(event_type: str, **data) -> None:
    get_stream_writer()({"type": event_type, "ts": time.time(), **data})


def _traced(name: str):
    """노드 시작/종료 이벤트와 소요 시간을 내보내는 데코레이터."""

    def deco(fn):
        @wraps(fn)
        async def wrapper(state: AgentState, config: RunnableConfig):
            _emit("node_start", node=name)
            started = time.perf_counter()
            try:
                return await fn(state, config)
            finally:
                _emit("node_end", node=name, ms=round((time.perf_counter() - started) * 1000))

        return wrapper

    return deco


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))


def _llm_stats(msg: AIMessage, ms: int) -> dict:
    usage = msg.usage_metadata or {}
    output_tokens = usage.get("output_tokens")
    return {
        "ms": ms,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": output_tokens,
        # vLLM은 생성 시간을 따로 주지 않으므로 첫 토큰 대기 시간을 포함한 전체 시간 기준이다.
        "tps": round(output_tokens / (ms / 1000), 1) if output_tokens and ms else None,
        # "length"면 max_tokens에서 잘린 응답이다
        "finish_reason": (msg.response_metadata or {}).get("finish_reason"),
    }


def _retryable(e: Exception) -> bool:
    """서버 측 일시 오류만 재시도한다. 4xx(모델 없음 등)는 재시도해도 소용없으므로 제외."""
    if isinstance(e, openai.APIStatusError):
        return e.status_code >= 500
    return isinstance(e, (openai.APIConnectionError, ConnectionError, httpx.TransportError))


async def _with_retry[T](node: str, call: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(settings.LLM_MAX_RETRIES + 1):
        try:
            return await call()
        except Exception as e:
            if attempt == settings.LLM_MAX_RETRIES or not _retryable(e):
                raise
            delay = settings.LLM_RETRY_DELAY * (attempt + 1)
            _emit(
                "llm_retry",
                node=node,
                attempt=attempt + 1,
                max_retries=settings.LLM_MAX_RETRIES,
                delay=delay,
                reason=str(e)[:300],
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _parse_json(text: str) -> dict:
    """응답에서 첫 JSON 객체를 꺼낸다. 앞뒤 설명 · 코드 펜스가 붙어도 되고, 실패하면 빈 dict."""
    start = text.find("{")
    if start < 0:
        return {}
    for end in range(len(text), start, -1):
        if text[end - 1] != "}":
            continue
        try:
            value = json.loads(text[start:end])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            continue
    return {}


async def _ask_json(node: str, system: str, messages: list[AnyMessage]) -> tuple[dict, dict]:
    """thinking off 짧은 호출. JSON 모드(response_format)는 쓰지 않는다 (seed32-think가 가끔 끝나지 않음)."""
    llm = _llm(thinking=False).bind(max_tokens=settings.PLANNER_MAX_TOKENS)
    started = time.perf_counter()
    msg = await _with_retry(node, lambda: llm.ainvoke([SystemMessage(system), *messages]))
    return _parse_json(_text(msg.content)), _llm_stats(msg, round((time.perf_counter() - started) * 1000))


def _history(state: AgentState) -> list[AnyMessage]:
    return [m for m in state["messages"] if isinstance(m, (HumanMessage, AIMessage))][-HISTORY_MESSAGES:]


def _short_header(header: str) -> str:
    """'문서 > 장 > 조' 머리말에서 문서명을 뺀 뒤쪽."""
    parts = [p.strip() for p in header.split(">")]
    return " > ".join(parts[1:]) if len(parts) > 1 else header


# ── 프롬프트 ─────────────────────────────────────────────────────────

ANALYZE_PROMPT = """당신은 한국산업단지공단 내부규정 Q&A의 질의 분석기입니다.
대화의 마지막 사용자 메시지를 보고, 규정 검색이 필요한지 판단하고 검색용 질의를 만드세요.

규칙:
- out_of_scope=true: 인사말·잡담, 규정과 무관한 요청, 개인별 기록 조회(내 포인트 잔액, 내 이번 달 급여액처럼
  회사 시스템의 개인 데이터를 봐야만 답할 수 있는 것).
- 조건을 붙여 기준을 묻는 질문은 "제가", "저는"으로 시작해도 규정 질문입니다(out_of_scope=false).
  예: "변호사 자격증 있으면 연봉 더 받아요?", "제가 육아휴직하면 보수 나와요?" → 규정의 지급 기준을 묻는 것.
- 규정 내용(기준·절차·금액 기준표 등)을 묻는 질문은 어떤 규정인지 몰라도 out_of_scope=false입니다.
- queries: 검색용 질의 1~3개. 이전 대화 맥락을 반영해 완결된 질의로 다시 씁니다("그럼 1년 넘으면요?" → "질병 휴직 1년 초과 보수").
  구어는 규정 용어로 바꿉니다(월급 → 보수, 기름값 → 자동차 운임). 질문이 여러 개면 나눕니다.
- 반드시 JSON으로만 응답: {"out_of_scope": false, "reason": "한 문장", "queries": ["..."]}"""

ROUTE_PROMPT = """당신은 내부규정 Q&A의 라우터입니다. 검색 후보 문서 중에서 사용자 질문에 답할 근거가 있을 문서를 고르세요.

질문(검색용으로 다시 쓴 것): {queries}

후보 문서:
{candidates}

규칙:
- 질문에 직접 답할 수 있는 문서만 최대 {max_docs}개 고릅니다. 두 문서에 걸친 질문이면 둘 다 고릅니다.
- "다루지 않음"에 해당하면 고르지 않습니다. 이름이 비슷하다는 이유만으로 고르지 않습니다.
- 맞는 문서가 없으면 빈 목록을 냅니다. 억지로 고르지 마세요.
- 반드시 JSON으로만 응답: {{"doc_ids": ["d1"], "reason": "한 문장"}}"""

PLAN_PROMPT = """당신은 내부규정 Q&A의 planner입니다. 선택된 문서의 목차에서 질문에 답하는 데 읽어야 할 청크를 고르세요.

질문(검색용으로 다시 쓴 것): {queries}

목차 (★ = 검색 적중, 절 아래 c번호가 청크):
{sections}

규칙:
- 답의 근거가 들어 있을 청크를 최대 {max_chunks}개 고릅니다. ★가 붙지 않은 청크도 고를 수 있습니다.
- 각 절의 "다루지 않음"에 해당하는 내용이면 그 절에서 고르지 않습니다.
- 목차 어디에도 질문에 답할 내용이 없으면 빈 목록을 냅니다.
- 반드시 JSON으로만 응답: {{"chunks": ["c1", "c2"], "reason": "한 문장"}}"""

ANSWER_PROMPT = """당신은 한국산업단지공단 직원의 내부규정 질문에 답하는 한국어 상담 에이전트입니다.

## 근거
{evidence}

## 원칙
- 위 근거에 있는 내용만 말하세요. 기억이나 일반 상식으로 규정을 보충하지 마세요.
- 수치(금액·비율·일수·포인트)는 근거에 적힌 그대로 옮기세요. 근거에 없는 수치를 계산하거나 추정하지 마세요.
- 근거로 답할 수 없는 부분은 "규정에서 확인되지 않습니다"라고 말하세요. 개인별 데이터는 조회할 수 없습니다.

## 답변 형식
- 첫 문장에 결론을 쓰세요.
- 이어서 조건·예외를 2~5개의 짧은 글머리표로 쓰세요.
- 근거를 쓴 문장 끝에 [1], [2]처럼 근거 번호를 붙이세요. 여러 개면 [1][3]. 근거 목록에 없는 번호는 쓰지 마세요.
- 처리 과정 설명, 태그·HTML, "추가 질문이 있으면…" 같은 맺음말은 쓰지 마세요."""


def _no_reference_text(out_of_scope: bool, reason: str, docs: list[dict]) -> str:
    names = "\n".join(f"- {d['name']}" for d in docs) or "- (없음)"
    head = (
        "내부규정 내용에 대한 질문에 답변드리고 있습니다. 개인별 데이터는 조회할 수 없습니다."
        if out_of_scope
        else f"관련 규정을 찾지 못해 답변드리기 어렵습니다. ({reason})"
    )
    return (
        f"{head}\n\n지금 답변할 수 있는 규정은 다음과 같습니다.\n{names}\n\n"
        "개인별 데이터나 위 목록에 없는 규정은 담당 부서에 문의해 주세요."
    )


def build_graph():
    @_traced("analyze")
    async def analyze(state: AgentState, config: RunnableConfig):
        parsed, stats = await _ask_json("analyze", ANALYZE_PROMPT, _history(state))
        queries = [str(q).strip() for q in parsed.get("queries") or [] if str(q).strip()][:3]
        last = _text(state["messages"][-1].content)
        out = bool(parsed.get("out_of_scope"))
        if not out and not queries:
            queries = [last]  # 파싱 실패 시 원문으로 검색한다
        reason = str(parsed.get("reason") or "")
        _emit("analysis", out_of_scope=out, reason=reason, queries=queries, stats=stats)
        # 턴마다 라우팅 상태를 새로 시작한다
        return {
            "queries": queries,
            "selected_docs": [],
            "excluded_docs": [],
            "reroutes": 0,
            "chunk_ids": [],
            "evidence": [],
            "no_reference": (reason or "규정 질문이 아닙니다") if out else None,
            "out_of_scope": out,
        }

    @_traced("route")
    async def route(state: AgentState, config: RunnableConfig):
        candidates, ms = await search.search_docs(state["queries"])
        excluded = set(state["excluded_docs"])
        shown = [c for c in candidates if c.doc_id not in excluded]
        _emit(
            "doc_candidates",
            ms=ms,
            dense=search.dense_enabled(),
            attempt=state["reroutes"] + 1,
            items=[{**asdict(c), "excluded": c.doc_id in excluded} for c in candidates],
        )
        if not shown:
            return {"selected_docs": [], "no_reference": "검색 후보 문서가 없습니다"}

        # 후보는 d1, d2… 짧은 별칭으로 보여 준다 (doc_id를 그대로 베끼다 틀리는 일을 막는다)
        alias = {f"d{i}": c for i, c in enumerate(shown, 1)}
        listing = "\n".join(
            f"{a}. {c.name}\n   요약: {c.summary}\n   다루지 않음: {c.not_for or '-'}" for a, c in alias.items()
        )
        parsed, stats = await _ask_json(
            "route",
            ROUTE_PROMPT.format(queries=" / ".join(state["queries"]), candidates=listing, max_docs=MAX_DOCS),
            _history(state)[-1:],
        )
        picked = [alias[a].doc_id for a in parsed.get("doc_ids") or [] if a in alias][:MAX_DOCS]
        _emit(
            "route",
            doc_ids=picked,
            names=[c.name for c in shown if c.doc_id in picked],
            reason=str(parsed.get("reason") or ""),
            stats=stats,
        )
        if not picked:
            return {"selected_docs": [], "no_reference": "질문에 맞는 규정 문서가 없습니다"}
        return {"selected_docs": picked}

    @_traced("plan")
    async def plan(state: AgentState, config: RunnableConfig):
        docs = state["selected_docs"]
        sections = await search.list_sections(docs)
        hits_list = []
        hit_ms = 0
        for q in state["queries"]:
            hits, ms = await search.search_chunks(q, docs)
            hits_list.append(hits)
            hit_ms += ms
        hit_sections = {h.section_id for hits in hits_list for h in hits if h.summary_hit or h.child_ids}
        hit_children = {c for hits in hits_list for h in hits for c in h.child_ids}
        by_id = {s.section_id: s for s in sections}
        _emit(
            "chunk_hits",
            ms=hit_ms,
            sections=[
                {
                    "section_id": h.section_id,
                    "doc_name": by_id[h.section_id].doc_name if h.section_id in by_id else "",
                    "title": by_id[h.section_id].title if h.section_id in by_id else h.section_id,
                    "score": h.score,
                    "summary_hit": h.summary_hit,
                    "child_hits": len(h.child_ids),
                }
                for h in hits_list[0][:10]
            ],
        )

        # 청크 머리말 · 본문 앞부분을 목차에 붙여 보여 준다
        chunks = {c.chunk_id: c for c in await search.get_chunks([c for s in sections for c in s.chunk_ids], 0)}
        alias: dict[str, str] = {}
        lines = []
        doc_name = None
        for s in sections:
            if s.doc_name != doc_name:
                doc_name = s.doc_name
                lines.append(f"# {doc_name}")
            mark = " ★" if s.section_id in hit_sections else ""
            lines.append(f"## {s.title} (p.{s.page_start}-{s.page_end}){mark}")
            lines.append(f"   다룸: {s.covers or '-'}")
            if s.not_covers:
                lines.append(f"   다루지 않음: {s.not_covers}")
            for cid in s.chunk_ids:
                c = chunks.get(cid)
                if not c:
                    continue
                a = f"c{len(alias) + 1}"
                alias[a] = cid
                preview = re.sub(r"\s+", " ", c.body)[:50]
                lines.append(f"   {a}{' ★' if cid in hit_children else ''} {_short_header(c.header)[-40:]} | {preview}")
        parsed, stats = await _ask_json(
            "plan",
            PLAN_PROMPT.format(queries=" / ".join(state["queries"]), sections="\n".join(lines), max_chunks=MAX_CHUNKS),
            _history(state)[-1:],
        )
        picked = [alias[a] for a in parsed.get("chunks") or [] if a in alias][:MAX_CHUNKS]
        _emit(
            "plan",
            chunk_ids=picked,
            labels=[_short_header(chunks[c].header) for c in picked],
            reason=str(parsed.get("reason") or ""),
            stats=stats,
        )
        if picked:
            return {"chunk_ids": picked}
        if state["reroutes"] < MAX_REROUTES:
            return {"chunk_ids": [], "excluded_docs": [*state["excluded_docs"], *docs], "reroutes": state["reroutes"] + 1}
        return {"chunk_ids": [], "no_reference": "선택한 문서에서 근거를 찾지 못했습니다"}

    @_traced("read")
    async def read(state: AgentState, config: RunnableConfig):
        chunks = await search.get_chunks(state["chunk_ids"], neighbors=1)
        evidence = [
            {
                "n": i,
                "chunk_id": c.chunk_id,
                "doc_name": c.doc_name,
                "section_title": c.section_title,
                "header": _short_header(c.header),
                "body": c.body,
                "page_start": c.page_start,
                "page_end": c.page_end,
                "has_numbers": c.has_numbers,
                "verified": c.verified,
                "neighbor": c.neighbor,
            }
            for i, c in enumerate(chunks, 1)
        ]
        _emit("evidence", items=evidence)
        return {"evidence": evidence}

    @_traced("answer")
    async def answer(state: AgentState, config: RunnableConfig):
        thinking = config["configurable"].get("thinking", True)
        llm = _llm(thinking)
        blocks = "\n\n".join(
            f"[{e['n']}] {e['doc_name']} > {e['header']} (p.{e['page_start']})\n{e['body']}" for e in state["evidence"]
        )
        messages = [SystemMessage(ANSWER_PROMPT.format(evidence=blocks)), *_history(state)]

        async def stream_once():
            # 재시도 시 이전 시도의 부분 출력은 버린다 (프론트엔드도 llm_retry 수신 시 초기화)
            full = None
            async for chunk in llm.astream(messages):
                if reasoning := chunk.additional_kwargs.get("reasoning_content"):
                    _emit("thinking", delta=reasoning)
                if delta := _text(chunk.content):
                    _emit("token", delta=delta)
                full = chunk if full is None else full + chunk
            return full

        started = time.perf_counter()
        full = await _with_retry("answer", stream_once)
        # reasoning_content(Thinking)는 다음 호출에 보내지 않는다 (backend/llm.py 참고)
        ai = AIMessage(content=full.content, usage_metadata=full.usage_metadata, id=full.id)
        _emit("llm_end", stats=_llm_stats(full, round((time.perf_counter() - started) * 1000)))
        return {"messages": [ai]}

    @_traced("no_reference")
    async def no_reference(state: AgentState, config: RunnableConfig):
        reason = state.get("no_reference") or "근거 없음"
        out = state.get("out_of_scope", False)
        text = _no_reference_text(out, reason, await search.documents())
        _emit("no_reference", reason=reason, out_of_scope=out)
        _emit("token", delta=text)
        return {"messages": [AIMessage(text)]}

    def after_analyze(state: AgentState) -> Literal["route", "no_reference"]:
        return "no_reference" if state.get("no_reference") else "route"

    def after_route(state: AgentState) -> Literal["plan", "no_reference"]:
        return "plan" if state["selected_docs"] else "no_reference"

    def after_plan(state: AgentState) -> Literal["read", "route", "no_reference"]:
        if state["chunk_ids"]:
            return "read"
        return "no_reference" if state.get("no_reference") else "route"

    g = StateGraph(AgentState)
    for name, fn in [
        ("analyze", analyze),
        ("route", route),
        ("plan", plan),
        ("read", read),
        ("answer", answer),
        ("no_reference", no_reference),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "analyze")
    g.add_conditional_edges("analyze", after_analyze, ["route", "no_reference"])
    g.add_conditional_edges("route", after_route, ["plan", "no_reference"])
    g.add_conditional_edges("plan", after_plan, ["read", "route", "no_reference"])
    g.add_edge("read", "answer")
    g.add_edge("answer", END)
    g.add_edge("no_reference", END)
    return g.compile(checkpointer=InMemorySaver())
