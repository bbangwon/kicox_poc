"""개인교육훈련 판정·계산 MCP 서버."""

from fastmcp import FastMCP

mcp = FastMCP("training", instructions="개인교육훈련 이수학점 계산을 처리하는 MCP 서버")

ACTIVITY_TYPES = ("사내외교육", "사회공헌활동", "학위과정")


@mcp.tool
def calculate_training_credits(
    activity_type: str | None = None,
    hours: float | None = None,
    semesters: int | None = None,
    credit_category: str | None = None,
    pre_approved: bool | None = None,
) -> dict:
    """개인교육훈련 이수학점을 학점지급기준에 따라 계산합니다.

    사용자가 말하지 않은 값은 추측하지 말고 생략하세요. 판단에 필요한 정보가 없으면
    status="need_info"와 함께 사용자에게 되물어야 할 질문(questions)을 반환합니다.

    Args:
        activity_type: 학점 유형. "사내외교육", "사회공헌활동", "학위과정" 중 하나
        hours: 교육·활동 총 시간 (여러 날이면 합산, 식사·화합행사 등 교육과 무관한 시간 제외). 사내외교육·사회공헌활동에 필요, 모르면 생략
        semesters: 이수 학기 수. 학위과정에 필요, 모르면 생략
        credit_category: 학점구분. "직무직급" 또는 "공통"(어학, 엑셀, OA 등). 모르면 생략
        pre_approved: 교육훈련담당부서의 사전승인(협의)을 받았는지 여부. 모르면 생략
    """
    if activity_type not in ACTIVITY_TYPES:
        return {
            "status": "need_info",
            "missing": ["activity_type"],
            "questions": ["학점을 인정받으려는 활동이 사내외교육, 사회공헌활동, 학위과정(대학·대학원) 중 어디에 해당하나요?"],
        }

    if activity_type == "학위과정":
        if not semesters:
            return {"status": "need_info", "missing": ["semesters"], "questions": ["학위과정을 몇 학기 이수하셨나요?"]}
        return {
            "status": "ok",
            "activity_type": activity_type,
            "credits": semesters * 2,
            "basis": f"학위과정 1학기당 2학점 × {semesters}학기",
        }

    if not hours:
        return {
            "status": "need_info",
            "missing": ["hours"],
            "questions": ["교육(활동) 시간은 총 몇 시간인가요? 여러 날이면 일자별 시간을 알려주세요 (식사·화합행사 시간 제외)."],
        }

    if activity_type == "사회공헌활동":
        credits = min(int(hours // 6), 2)
        return {
            "status": "ok",
            "activity_type": activity_type,
            "hours": hours,
            "credits": credits,
            "basis": "사회공헌활동 6시간당 1학점 (최대 2학점)",
            "note": "3시간 이상 0.5학점 규정은 사내외교육에만 명시되어 있습니다.",
        }

    # 사내외교육
    if credit_category == "공통":
        return {
            "status": "ok",
            "activity_type": activity_type,
            "credits": 0,
            "basis": "공통 교육(어학, 엑셀, OA 등)은 학점신청 불가",
        }
    if pre_approved is False:
        return {
            "status": "ok",
            "activity_type": activity_type,
            "credits": 0,
            "basis": "사내외교육 학점은 교육훈련담당부서의 사전승인(협의)을 받은 교육과정에 한함",
        }
    full, rest = divmod(hours, 6)
    credits = min(int(full) + (0.5 if rest >= 3 else 0), 3)
    result = {
        "status": "ok",
        "activity_type": activity_type,
        "hours": hours,
        "credits": credits,
        "basis": "사내외교육 6시간당 1학점, 나머지 3시간 이상 0.5학점 (최대 3학점)",
    }
    conditions = []
    if credit_category != "직무직급":
        conditions.append("직무직급 교육이어야 합니다 (어학·OA 등 공통 교육은 학점신청 불가)")
    if pre_approved is None:
        conditions.append("교육훈련담당부서의 사전승인(협의)을 받은 과정이어야 합니다")
    if conditions:
        result["conditions"] = conditions
    return result


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8102)
