import { useState } from 'react'
import { SCENARIO_GROUPS, type Scenario } from '../scenarios'

interface Props {
  busy: boolean
  /** fresh=true면 새 대화(새 thread)로 시작한다. 답변이 끝나면 resolve */
  onSend: (text: string, fresh: boolean) => Promise<void>
}

export function ScenarioPanel({ busy, onSend }: Props) {
  const [active, setActive] = useState<string | null>(null)
  // 시나리오별로 보낸 단계 수
  const [sent, setSent] = useState<Record<string, number>>({})
  const [checked, setChecked] = useState<Record<string, boolean>>({})

  const sendStep = async (s: Scenario, i: number) => {
    setActive(s.id)
    setSent((m) => ({ ...m, [s.id]: i + 1 }))
    await onSend(s.steps[i], i === 0)
  }

  const runAll = async (s: Scenario) => {
    for (let i = 0; i < s.steps.length; i++) await sendStep(s, i)
  }

  const total = SCENARIO_GROUPS.flatMap((g) => g.scenarios).reduce((n, s) => n + s.checks.length, 0)
  const passed = Object.values(checked).filter(Boolean).length

  return (
    <div className="scenarios">
      <div className="column-head">
        <h2 className="column-title">테스트 시나리오</h2>
        <span className="trace-meta">
          확인 {passed}/{total}
        </span>
      </div>
      <div className="scenario-scroll">
        <p className="scenario-intro">
          <b>실행</b>을 누르면 새 대화로 질문을 보냅니다. 오른쪽 <b>처리 단계</b> 탭에서 재작성 질의 · 문서 후보 · 선택
          청크를 보고 확인 항목을 직접 체크하세요.
        </p>
        {SCENARIO_GROUPS.map((g) => (
          <section key={g.title} className="scenario-group">
            <div className="side-section">{g.title}</div>
            {g.scenarios.map((s) => {
              const done = sent[s.id] ?? 0
              const ok = s.checks.filter((_, i) => checked[`${s.id}:${i}`]).length
              return (
                <div key={s.id} className="scenario" data-active={active === s.id || undefined}>
                  <div className="scenario-head">
                    <span className="scenario-title">{s.title}</span>
                    {ok > 0 && (
                      <span className="tag" data-tone={ok === s.checks.length ? 'success' : undefined}>
                        {ok}/{s.checks.length}
                      </span>
                    )}
                    <button type="button" className="btn-sm" disabled={busy} onClick={() => runAll(s)}>
                      {done ? '다시 실행' : '실행'}
                    </button>
                  </div>
                  <div className="scenario-goal">{s.goal}</div>
                  <ol className="scenario-steps">
                    {s.steps.map((text, i) => (
                      <li key={i}>
                        <span className="step-text">“{text}”</span>
                        {s.steps.length > 1 && (
                          <button
                            type="button"
                            className="link"
                            disabled={busy || i > done}
                            title={i > done ? '이전 단계를 먼저 보내세요' : undefined}
                            onClick={() => sendStep(s, i)}
                          >
                            {i < done ? '다시' : i === 0 ? '새 대화로' : '이어서'}
                          </button>
                        )}
                      </li>
                    ))}
                  </ol>
                  <ul className="scenario-checks">
                    {s.checks.map((c, i) => {
                      const key = `${s.id}:${i}`
                      return (
                        <li key={key}>
                          <label>
                            <input
                              type="checkbox"
                              checked={!!checked[key]}
                              onChange={(e) => setChecked((m) => ({ ...m, [key]: e.target.checked }))}
                            />
                            {c}
                          </label>
                        </li>
                      )
                    })}
                  </ul>
                </div>
              )
            })}
          </section>
        ))}
      </div>
    </div>
  )
}
