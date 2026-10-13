"""사내 문서 검색 MCP 서버 (mock 데이터)."""

from fastmcp import FastMCP

mcp = FastMCP("docs", instructions="사내 문서 저장소를 검색하는 MCP 서버")

DOCUMENTS = [
    {
        "id": "DOC-2026-Q3-01",
        "title": "2026년 3분기 사업 실적 보고서",
        "content": "2026년 3분기(지난 분기) 신규 가입자 수는 50,000명으로 전 분기 대비 8% 증가했습니다. "
        "누적 가입자는 1,240,000명이며 월간 활성 사용자(MAU)는 410,000명입니다.",
    },
    {
        "id": "DOC-2026-Q3-02",
        "title": "2026년 3분기 매출 요약",
        "content": "3분기 매출은 182억 원, 영업이익은 21억 원입니다. 구독 매출 비중은 64%입니다.",
    },
    {
        "id": "DOC-ORG-01",
        "title": "조직 연락처",
        "content": "운영팀: ops@company.com / 마케팅팀: marketing@company.com / 재무팀: finance@company.com",
    },
    {
        "id": "DOC-POLICY-07",
        "title": "보고 메일 작성 가이드",
        "content": "보고 메일은 제목에 [보고] 머리말을 붙이고, 본문은 요약-근거-요청사항 순으로 작성합니다.",
    },
]


@mcp.tool
def search_internal_docs(query: str, top_k: int = 2) -> list[dict]:
    """사내 문서에서 질의와 관련된 문서를 검색합니다.

    Args:
        query: 검색할 한국어 질의 (예: "지난 분기 신규 가입자 수")
        top_k: 반환할 최대 문서 수
    """
    terms = [t for t in query.replace(",", " ").split() if len(t) > 1]

    def score(doc: dict) -> int:
        text = doc["title"] + " " + doc["content"]
        return sum(text.count(t) for t in terms)

    ranked = sorted(DOCUMENTS, key=score, reverse=True)
    return [d for d in ranked if score(d) > 0][:top_k] or ranked[:1]


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8101)
