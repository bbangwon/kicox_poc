"""관문 검사(§7.1)와 적재 판정 (stale · 도구 승인 · 검증 표시)."""

import ast

from ingest.models import Artifacts, ToolEntry


def check(a: Artifacts) -> list[str]:
    """적재를 막는 사유 목록. 비어 있으면 통과. 필수 파일 · 형식 버전 · 형식 오류는 reader가 거른다."""
    reasons = []
    doc = a.summaries.doc
    if doc is None or doc.error or doc.fields is None or not doc.fields.summary.strip():
        reasons.append(f"문서 요약 없음{f': {doc.error}' if doc and doc.error else ''}")

    for p in a.chunks.parents:
        if not p.searchable:
            continue
        s = a.summaries.parents.get(p.id)
        if s is None:
            reasons.append(f"parent 요약 없음: {p.id}")
        elif s.error or s.fields is None:
            reasons.append(f"parent 요약 오류: {p.id} {s.error or ''}".rstrip())
        elif s.parent_hash != p.hash:
            reasons.append(f"parent 요약 stale: {p.id}")

    if a.parse and a.parse.truncated:
        reasons.append(f"잘린 쪽: {a.parse.truncated}")
    return reasons


def compact_header(header: str) -> str:
    """'보수규정 시행세칙 > 보수규정 시행세칙'처럼 연속으로 반복되는 항목을 하나로 줄인다."""
    parts: list[str] = []
    # 구분자는 " > ". 항목 안의 '>'(예: '부칙 <2011.09.14.>')는 자르지 않는다
    for part in (p.strip() for p in header.split(" > ")):
        if part and (not parts or parts[-1] != part):
            parts.append(part)
    return " > ".join(parts)


def is_verified(a: Artifacts, key: str) -> bool:
    mark = a.verify.get(key)
    return bool(mark and mark.verified)


def _lookups(spec: dict) -> set[str]:
    """식 도구가 부르는 표 이름 (lookup("표", ...))."""
    names = set()
    for step in spec.get("steps") or []:
        expr = step.get("expr") if isinstance(step, dict) else None
        try:
            tree = ast.parse(expr, mode="eval") if isinstance(expr, str) else None
        except SyntaxError:
            continue
        for n in ast.walk(tree) if tree else ():
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "lookup"
                and n.args
                and isinstance(n.args[0], ast.Constant)
            ):
                names.add(n.args[0].value)
    return names


def approved_tools(a: Artifacts) -> list[ToolEntry]:
    """전처리 preprocess.tools.approved와 같은 조건: 검증 표시 · stale 아님 · error 아님.

    승인되지 않은 표를 부르는 식 도구는 실행할 수 없으므로 함께 뺀다 (§4.2).
    """
    if not a.tools:
        return []
    hashes = {p.id: p.hash for p in a.chunks.parents}
    ok = [
        t
        for t in a.tools.tools
        if hashes.get(t.parent_id) == t.parent_hash
        and t.checks.status != "error"
        and is_verified(a, f"tool:{t.spec_hash}")
    ]
    tables = {t.spec.get("name") for t in ok if t.spec.get("kind") == "table_lookup"}
    return [t for t in ok if _lookups(t.spec) <= tables]
