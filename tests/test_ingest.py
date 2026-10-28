"""ingest 단위 테스트 (DB · Qdrant · 임베딩 서버 없이)."""

import zlib

import pytest

from ingest import gate, ids, sparse
from ingest.models import Artifacts


def _artifacts(**over) -> Artifacts:
    data = {
        "meta": {"doc_id": "d1", "name": "여비규정", "pages": 1},
        "chunks": {
            "parents": [{"id": "p01", "title": "제1장", "child_ids": ["c001"], "searchable": True, "hash": "ph"}],
            "children": [
                {"id": "c001", "parent_id": "p01", "header": "여비규정 > 제1장", "text": "본문", "tokens": 3,
                 "has_numbers": False, "searchable": True, "hash": "ch"}
            ],
        },
        "summaries": {
            "parents": {"p01": {"parent_id": "p01", "parent_hash": "ph", "fields": {"covers": "범위"}}},
            "doc": {"fields": {"summary": "요약"}},
        },
        "content_hash": "x",
    }  # fmt: skip
    data.update(over)
    return Artifacts(**data)


def _tool(name, kind, spec_hash, status="ready", parent_hash="ph", **spec):
    return {"parent_id": "p01", "parent_hash": parent_hash, "spec_hash": spec_hash,
            "spec": {"name": name, "kind": kind, **spec}, "checks": {"status": status}}  # fmt: skip


# ── ids ──


def test_content_ids_stable_and_disambiguate_duplicates():
    a = ids.content_ids("d1", "child", ["h1", "h2", "h1"])
    assert a == ids.content_ids("d1", "child", ["h1", "h2", "h1"])
    assert len(set(a)) == 3
    # 앞에 청크가 끼어도(재청킹으로 순번이 밀려도) 내용이 같으면 ID가 같다
    assert ids.content_ids("d1", "child", ["h0", "h1"])[1] == a[0]
    assert ids.content_ids("d2", "child", ["h1"])[0] != a[0]


# ── gate ──


def test_compact_header_dedupes_and_keeps_inner_angle_brackets():
    assert gate.compact_header("보수규정 시행세칙 > 보수규정 시행세칙") == "보수규정 시행세칙"
    assert gate.compact_header("세칙 > 부칙 > 부칙 <2011.09.14.>") == "세칙 > 부칙 > 부칙 <2011.09.14.>"


def test_check_passes_complete_artifacts():
    assert gate.check(_artifacts()) == []


@pytest.mark.parametrize(
    "summaries, expected",
    [
        ({"parents": {}, "doc": {"fields": {"summary": "요약"}}}, "parent 요약 없음: p01"),
        ({"parents": {"p01": {"parent_id": "p01", "parent_hash": "old", "fields": {}}}, "doc": {"fields": {"summary": "요약"}}},
         "parent 요약 stale: p01"),
        ({"parents": {"p01": {"parent_id": "p01", "parent_hash": "ph", "fields": {}}}, "doc": {"error": "timeout"}},
         "문서 요약 없음: timeout"),
    ],
)  # fmt: skip
def test_check_rejects(summaries, expected):
    assert expected in gate.check(_artifacts(summaries=summaries))


def test_check_rejects_truncated_pages():
    assert gate.check(_artifacts(parse={"truncated": [3]})) == ["잘린 쪽: [3]"]


def test_approved_tools_requires_mark_not_stale_not_error_and_approved_tables():
    tools = {
        "tools": [
            _tool("ok", "formula", "s1", steps=[{"name": "r", "expr": "a * 2"}]),
            _tool("unmarked", "formula", "s2"),
            _tool("errored", "formula", "s3", status="error"),
            _tool("stale", "formula", "s4", parent_hash="old"),
            _tool("rate", "table_lookup", "s5"),
            _tool("uses_rate", "formula", "s6", steps=[{"name": "r", "expr": 'lookup("rate", g) * n'}]),
            _tool("uses_missing", "formula", "s7", steps=[{"name": "r", "expr": 'lookup("other", g)'}]),
        ]
    }
    verify = {f"tool:s{i}": {"verified": True} for i in (1, 3, 4, 5, 6, 7)}
    got = gate.approved_tools(_artifacts(tools=tools, verify=verify))
    assert [t.spec["name"] for t in got] == ["ok", "rate", "uses_rate"]


# ── sparse ──


def test_tokens_words_and_hangul_bigrams_without_stopwords():
    assert sparse.tokens("출장비를 및 KTX 2026") == ["출장비를", "출장", "장비", "비를", "ktx", "2026"]
    # 조사가 붙어도 2-gram을 공유한다
    assert {"출장", "장비"} <= set(sparse.tokens("출장비"))


def test_document_vector_bm25_saturation():
    v = sparse.document("여비 여비 숙박", avgdl=3)
    w = dict(zip(v.indices, v.values))
    k1, b = 1.2, 0.75
    assert w[zlib.crc32("여비".encode())] == pytest.approx(2 * (k1 + 1) / (2 + k1 * (1 - b + b * 3 / 3)))
    assert w[zlib.crc32("숙박".encode())] == pytest.approx(1.0)
    assert v.indices == sorted(v.indices)


def test_query_vector_is_binary():
    v = sparse.query("여비 여비 숙박")
    assert sorted(v.values) == [1.0, 1.0]
