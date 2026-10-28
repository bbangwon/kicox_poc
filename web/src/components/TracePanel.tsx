import type { ReactNode } from 'react'
import type { DocCandidate, LlmStats, Run } from '../types'

const NODE_LABEL: Record<string, string> = {
  analyze: '질의 분석',
  route: '문서 선택',
  plan: '청크 선택',
  read: '원문 조회',
  answer: '답변 작성',
  no_reference: '참조문서 없음',
}

function Stats({ stats }: { stats?: LlmStats }) {
  if (!stats) return null
  return (
    <span className="stats">
      LLM {stats.ms.toLocaleString()}ms · 입력 {stats.input_tokens ?? '-'} · 출력 {stats.output_tokens ?? '-'}
      {stats.tps != null && ` · ${stats.tps} tok/s`}
      {stats.finish_reason === 'length' && <span className="tag" data-tone="warning">상한에서 잘림</span>}
    </span>
  )
}

function Block({ title, meta, children }: { title: string; meta?: ReactNode; children: ReactNode }) {
  return (
    <section className="trace-block">
      <div className="trace-head">
        <h4>{title}</h4>
        {meta && <span className="trace-meta">{meta}</span>}
      </div>
      {children}
    </section>
  )
}

/** 질의별 순위에서 신호별 최고 순위만 뽑는다: {"q1:dense": 3, "q2:dense": 1} → dense 1 */
function bestRank(c: DocCandidate, signal: string) {
  const ranks = Object.entries(c.ranks)
    .filter(([k]) => k.endsWith(`:${signal}`))
    .map(([, v]) => v)
  return ranks.length ? Math.min(...ranks) : null
}

export function TracePanel({ run }: { run: Run | null }) {
  if (!run) return <span className="evidence-empty">질문을 보내면 단계별 검색 결과와 LLM 판단이 여기에 나옵니다.</span>
  const a = run.analysis

  return (
    <div className="trace">
      <Block title="노드 실행" meta={run.totalMs != null ? `전체 ${(run.totalMs / 1000).toFixed(1)}초` : '실행 중'}>
        <ol className="spans">
          {run.spans.map((s, i) => (
            <li key={i} data-active={s.ms == null || undefined}>
              <span>{NODE_LABEL[s.node] ?? s.node}</span>
              <span className="num">{s.ms != null ? `${s.ms.toLocaleString()}ms` : '…'}</span>
            </li>
          ))}
        </ol>
        {run.retries.map((r, i) => (
          <div key={i} className="notice" data-tone="warning">
            {NODE_LABEL[r.node]} 재시도 {r.attempt}/{r.max} ({r.delay}초 후): {r.reason}
          </div>
        ))}
        {run.error && (
          <div className="notice" data-tone="danger">
            {run.error}
          </div>
        )}
      </Block>

      {a && (
        <Block title="질의 분석" meta={a.outOfScope ? <span className="tag" data-tone="warning">범위 밖</span> : '검색 필요'}>
          {a.queries.length > 0 && (
            <ul className="queries">
              {a.queries.map((q, i) => (
                <li key={i}>
                  <span className="qn">q{i + 1}</span> {q}
                </li>
              ))}
            </ul>
          )}
          <p className="reason">{a.reason}</p>
          <Stats stats={a.stats} />
        </Block>
      )}

      {run.routes.map((r, i) => (
        <Block
          key={i}
          title={run.routes.length > 1 ? `문서 검색 · ${r.attempt}회차` : '문서 검색'}
          meta={`${r.dense ? 'dense + BM25' : 'BM25만'} · RRF · ${r.ms}ms`}
        >
          <table className="tbl">
            <thead>
              <tr>
                <th>문서</th>
                <th className="num">RRF</th>
                <th className="num">dense</th>
                <th className="num">BM25</th>
              </tr>
            </thead>
            <tbody>
              {r.candidates.map((c) => (
                <tr key={c.doc_id} data-picked={r.docIds?.includes(c.doc_id) || undefined} data-excluded={c.excluded || undefined}>
                  <td title={c.summary}>
                    {r.docIds?.includes(c.doc_id) && <span className="pick">선택</span>}
                    {c.excluded && <span className="tag">제외</span>} {c.name}
                    <span className="sub">{c.hit === 'doc_question' ? '예상 질문 적중' : '요약 적중'}</span>
                  </td>
                  <td className="num">{c.score.toFixed(4)}</td>
                  <td className="num">{bestRank(c, 'dense') ?? '-'}</td>
                  <td className="num">{bestRank(c, 'bm25') ?? '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {r.docIds && (
            <>
              <p className="reason">
                <b>router:</b> {r.docIds.length ? r.reason : `해당 없음 · ${r.reason}`}
              </p>
              <Stats stats={r.stats} />
            </>
          )}
        </Block>
      ))}

      {run.plans.map((p, i) => (
        <Block key={i} title={run.plans.length > 1 ? `청크 검색 · ${i + 1}회차` : '청크 검색'} meta={`q1 기준 · ${p.ms ?? 0}ms`}>
          <table className="tbl">
            <thead>
              <tr>
                <th>절 (parent)</th>
                <th className="num">RRF</th>
                <th className="num">child</th>
              </tr>
            </thead>
            <tbody>
              {p.sections.map((s) => (
                <tr key={s.section_id}>
                  <td>
                    {s.title}
                    <span className="sub">
                      {s.doc_name}
                      {s.summary_hit && ' · 요약 적중'}
                    </span>
                  </td>
                  <td className="num">{s.score.toFixed(4)}</td>
                  <td className="num">{s.child_hits}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {p.chunkIds && (
            <>
              <p className="reason">
                <b>planner:</b> {p.chunkIds.length ? p.reason : `근거 없음 · ${p.reason}`}
              </p>
              {p.labels && p.labels.length > 0 && (
                <ul className="queries">
                  {p.labels.map((l, j) => (
                    <li key={j}>
                      <span className="qn">{j + 1}</span> {l}
                    </li>
                  ))}
                </ul>
              )}
              <Stats stats={p.stats} />
            </>
          )}
        </Block>
      ))}

      {(run.answerStats || run.thinking) && (
        <Block title="답변 생성" meta={run.thinkingEnabled ? 'Thinking on' : 'Thinking off'}>
          <Stats stats={run.answerStats} />
          {run.thinking && (
            <details className="thinking">
              <summary>Thinking ({run.thinking.length.toLocaleString()}자)</summary>
              <pre>{run.thinking}</pre>
            </details>
          )}
        </Block>
      )}
    </div>
  )
}
