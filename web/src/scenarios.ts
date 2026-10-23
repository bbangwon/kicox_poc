// 테스트 시나리오 가이드. 기대 결과는 backend 프롬프트·MCP 도구의 현재 동작 기준이다.

export interface Scenario {
  id: string
  title: string
  goal: string
  /** 순서대로 보낼 메시지. 첫 메시지는 새 대화로 시작하고, 이후는 같은 대화에 이어 보낸다. */
  steps: string[]
  checks: string[]
  /** 아직 평가셋으로 검증하지 않은 시나리오 */
  unverified?: boolean
}

export interface ScenarioGroup {
  title: string
  scenarios: Scenario[]
}

export const SCENARIO_GROUPS: ScenarioGroup[] = [
  {
    title: '이수학점 계산',
    scenarios: [
      {
        id: 'credit-multiturn',
        title: '역질문 멀티턴',
        goal: '정보가 부족하면 추측하지 않고 되묻고, 답을 받으면 앞 정보와 합쳐 계산하는지',
        steps: ['교육을 이틀 동안 3시간씩 들었는데 몇 학점이에요?', '사내외교육이에요. 사전승인도 받았어요'],
        checks: [
          '1턴: calculate_training_credits(hours=6) → need_info',
          '1턴: 교육 유형(사내외교육·사회공헌활동·학위과정)을 되물음',
          '2턴: hours=6을 다시 묻지 않고 activity_type과 함께 넘김',
          '2턴: 1학점 + "근거: … › 이수학점"',
        ],
      },
      {
        id: 'credit-cap',
        title: '최대 학점 한도',
        goal: '사회공헌활동 최대 2학점 한도를 적용하는지',
        steps: ['사회공헌활동 13시간 했는데 몇 학점 받아요?'],
        checks: ['도구 결과 credits=2', '답변 첫 문장이 "2학점"', '"최대 2학점" 근거 안내'],
      },
      {
        id: 'credit-common',
        title: '공통 교육 학점 불가',
        goal: '엑셀·OA·어학 같은 공통 교육은 학점신청이 안 된다고 답하는지',
        steps: ['엑셀 교육 6시간 들었는데 학점 받을 수 있어요?'],
        checks: ['학점 인정 안 됨', '이유: 공통 교육은 학점신청 불가'],
      },
    ],
  },
  {
    title: '지원 여부·과세',
    scenarios: [
      {
        id: 'support-taxed',
        title: '"과세" 해석',
        goal: '"공통 / 과세"를 지원 불가로 오해하지 않는지',
        steps: ['토익 학원비 지원돼요? 과세예요?'],
        checks: ['get_training_policy로 지원범위·제외대상 조회', '결론: 지원됨, 다만 과세', '"근거: … › 지원범위·제외대상"'],
      },
      {
        id: 'support-excluded',
        title: '제외대상',
        goal: '개인 취미 분야를 제외대상으로 판단하는지',
        steps: ['필라테스 수강료도 교육비 지원되나요?'],
        checks: ['결론: 지원 불가', '이유: 건강·개인 취미 관련 분야 제외'],
      },
      {
        id: 'support-proviso',
        title: '단서 조항',
        goal: '취업·이직준비 목적 제외 단서를 찾아내는지',
        steps: ['공무원 시험 준비 강의 지원돼요?'],
        checks: ['결론: 지원 불가', '이유: 취업·이직준비 관련 교육(NCS·PSAT·공무원 시험) 제외'],
      },
      {
        id: 'support-relevance',
        title: '업무 연관성 역질문',
        goal: '현직무·IDP 희망직무와의 관련성에 따라 직무/공통·과세가 갈릴 때 되묻는지',
        steps: ['데이터 분석 교육 지원돼요?'],
        checks: ['추측해서 단정하지 않음', '현직무·IDP 희망직무 관련 여부를 선택지로 되물음'],
        unverified: true,
      },
    ],
  },
  {
    title: '안내·범위',
    scenarios: [
      {
        id: 'guide-apply',
        title: '신청 경로',
        goal: '규정 원문의 MIS 메뉴 경로를 그대로 안내하는지',
        steps: ['교육신청은 어디서 해요?'],
        checks: ['MIS → 인사관리 → 교육관리 → 교육신청목록 → 신규', '"근거: … › 신청방법"'],
      },
      {
        id: 'gap-balance',
        title: '규정 공백 (잔액)',
        goal: '규정·데이터에 없는 내용을 지어내지 않는지',
        steps: ['제 교육훈련비 얼마 남았어요?'],
        checks: ['도구 호출 없음', '사원번호 등을 물어 조회해 주겠다고 하지 않음', '인사팀 문의 안내'],
      },
      {
        id: 'out-of-scope',
        title: '범위 밖 (여비)',
        goal: '아직 지원하지 않는 규정에 금액을 추정하지 않는지',
        steps: ['내일 대전 당일출장인데 출장비 얼마 나와요?'],
        checks: ['도구 호출 없음', '금액을 말하지 않음', '상생복지팀 문의 안내'],
      },
    ],
  },
]
