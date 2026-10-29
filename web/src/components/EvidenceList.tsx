import { useEffect, useRef, useState } from 'react'
import { citedNumbers } from '../runReducer'
import type { Run } from '../types'

interface Props {
  run: Run | null
  /** 답변의 [n] 칩을 누르면 바뀐다 (nonce로 같은 번호 재클릭도 반영) */
  focus: { n: number; nonce: number } | null
}

function pages(start: number | null, end: number | null) {
  if (start == null) return '쪽 정보 없음'
  return end != null && end !== start ? `p.${start}–${end}` : `p.${start}`
}

/** 원 프로젝트 EvidencePanel 모양: 카드 = 문서 태그 · 위치 · 머리말, 누르면 원문을 펼친다 (한 번에 하나) */
export function EvidenceList({ run, focus }: Props) {
  // 직접 펼친 카드와 그때의 focus. 그 뒤에 인용 칩(focus)을 누르면 그 카드를 펼친다
  const [manual, setManual] = useState<{ n: number | null; focus: number }>({ n: null, focus: 0 })
  const opened = focus && focus.nonce !== manual.focus ? focus.n : manual.n
  const setOpened = (n: number | null) => setManual({ n, focus: focus?.nonce ?? 0 })
  const refs = useRef<Record<number, HTMLLIElement | null>>({})

  useEffect(() => {
    if (focus) refs.current[focus.n]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [focus])

  if (!run) return <span className="evidence-empty">질문을 보내면 이 답변이 참고한 규정 원문이 여기에 나옵니다.</span>

  const cited = new Set(citedNumbers(run.answer))
  const evidence = run.evidence

  return (
    <section className="evidence" aria-label="근거">
      <div className="evidence-head">
        <h4 className="evidence-title">근거</h4>
        <span className="evidence-meta">
          {evidence.length ? `${evidence.length}건 · 인용 ${cited.size}건` : run.endTs ? '없음' : '조회 중…'}
        </span>
      </div>

      {run.noReference && (
        <div className="notice" data-tone="warning" role="alert">
          <strong>참조문서 없음</strong> {run.noReference.reason}
        </div>
      )}
      {run.endTs && evidence.length > 0 && cited.size === 0 && (
        <div className="notice" data-tone="info">
          답변에 [n] 인용이 없습니다. 근거 표시율 확인 대상입니다.
        </div>
      )}

      <ul className="evidence-list">
        {evidence.map((e) => {
          const open = opened === e.n
          return (
            <li
              key={e.chunk_id}
              ref={(el) => {
                refs.current[e.n] = el
              }}
              className="evidence-card"
              data-open={open}
              data-cited={cited.has(e.n)}
            >
              <button type="button" className="evidence-button" onClick={() => setOpened(open ? null : e.n)}>
                <span className="evidence-n">{e.n}</span>
                <span className="evidence-text">
                  <span className="evidence-name">{e.doc_name}</span>
                  <span className="evidence-line">
                    {e.header} · {pages(e.page_start, e.page_end)}
                  </span>
                  <span className="tags">
                    {cited.has(e.n) ? <span className="tag" data-tone="primary">인용</span> : <span className="tag">참고</span>}
                    {e.kind === 'tool' && <span className="tag">계산 도구</span>}
                    {e.neighbor && <span className="tag">앞뒤 이웃</span>}
                    {e.has_numbers && !e.verified && (
                      <span className="tag" data-tone="warning">
                        검증 전 수치
                      </span>
                    )}
                    {e.verified && (
                      <span className="tag" data-tone="success">
                        검증됨
                      </span>
                    )}
                  </span>
                </span>
                <span className="evidence-caret">{open ? '접기' : '원문'}</span>
              </button>
              {open && (
                <div className="evidence-quote">
                  <span className="evidence-quote-title">
                    {e.section_title} · {e.kind === 'tool' ? '승인된 계산 도구' : `chunk ${e.chunk_id.slice(0, 8)}`}
                  </span>
                  <span className="evidence-quote-text">{e.body}</span>
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
