import { useState } from 'react'
import { SCENARIO_GROUPS, type Scenario } from '../scenarios'

interface Props {
  busy: boolean
  /** fresh=true면 새 대화(새 thread)로 시작한다 */
  onSend: (text: string, fresh: boolean) => void
}

export function ScenarioPanel({ busy, onSend }: Props) {
  const [active, setActive] = useState<string | null>(null)
  // 시나리오별로 보낸 단계 수
  const [sent, setSent] = useState<Record<string, number>>({})
  const [checked, setChecked] = useState<Record<string, boolean>>({})

  const sendStep = (s: Scenario, i: number) => {
    setActive(s.id)
    setSent((m) => ({ ...m, [s.id]: i + 1 }))
    onSend(s.steps[i], i === 0)
  }

  return (
    <div className="scenarios">
      <p className="scenarios-intro">
        질문을 보내고 <b>실행 타임라인</b>에서 도구 호출·인자·결과를 확인한 뒤, 확인 항목을 직접 체크하세요. 멀티턴은 순서대로
        보냅니다.
      </p>
      {SCENARIO_GROUPS.map((g) => (
        <section key={g.title} className="scenario-group">
          <div className="scenario-group-title">{g.title}</div>
          {g.scenarios.map((s) => {
            const done = sent[s.id] ?? 0
            const passed = s.checks.filter((_, i) => checked[`${s.id}:${i}`]).length
            return (
              <div key={s.id} className={`scenario ${active === s.id ? 'active' : ''}`}>
                <div className="scenario-head">
                  <span className="scenario-title">{s.title}</span>
                  {s.unverified && <span className="badge warn">검증 전</span>}
                  {passed > 0 && (
                    <span className="scenario-score">
                      {passed}/{s.checks.length}
                    </span>
                  )}
                </div>
                <div className="scenario-goal">{s.goal}</div>
                <ol className="scenario-steps">
                  {s.steps.map((text, i) => (
                    <li key={i}>
                      <span className="step-text">“{text}”</span>
                      <button
                        className={i < done ? 'ghost' : ''}
                        disabled={busy || i > done}
                        title={i > done ? '이전 단계를 먼저 보내세요' : undefined}
                        onClick={() => sendStep(s, i)}
                      >
                        {i < done ? '다시' : i === 0 ? '새 대화로 보내기' : '이어서 보내기'}
                      </button>
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
  )
}
