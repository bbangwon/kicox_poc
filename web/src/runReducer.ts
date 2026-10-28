import type { AgentEvent, NodeName, Run } from './types'

export function newRun(id: string, prompt: string, thinkingEnabled: boolean): Run {
  return {
    id,
    prompt,
    thinkingEnabled,
    sentAt: Date.now() / 1000,
    spans: [],
    activeNode: null,
    routes: [],
    plans: [],
    evidence: [],
    thinking: '',
    answer: '',
    retries: [],
  }
}

function updateLast<T>(items: T[], fn: (item: T) => T): T[] {
  if (!items.length) return items
  return [...items.slice(0, -1), fn(items[items.length - 1])]
}

export function applyEvent(run: Run, e: AgentEvent): Run {
  switch (e.type) {
    case 'run_start':
      return { ...run, startTs: e.ts }

    case 'node_start':
      return { ...run, activeNode: e.node, spans: [...run.spans, { node: e.node, t: e.ts }] }

    case 'node_end':
      return { ...run, activeNode: null, spans: updateLast(run.spans, (s) => ({ ...s, ms: e.ms })) }

    case 'analysis':
      return {
        ...run,
        analysis: { outOfScope: e.out_of_scope, reason: e.reason, queries: e.queries, stats: e.stats },
      }

    case 'doc_candidates':
      return {
        ...run,
        routes: [...run.routes, { attempt: e.attempt, ms: e.ms, dense: e.dense, candidates: e.items }],
      }

    case 'route':
      return {
        ...run,
        routes: updateLast(run.routes, (r) => ({
          ...r,
          docIds: e.doc_ids,
          names: e.names,
          reason: e.reason,
          stats: e.stats,
        })),
      }

    case 'chunk_hits':
      return { ...run, plans: [...run.plans, { ms: e.ms, sections: e.sections }] }

    case 'plan':
      return {
        ...run,
        plans: updateLast(run.plans, (p) => ({
          ...p,
          chunkIds: e.chunk_ids,
          labels: e.labels,
          reason: e.reason,
          stats: e.stats,
        })),
      }

    case 'evidence':
      return { ...run, evidence: e.items }

    case 'no_reference':
      return { ...run, noReference: { reason: e.reason, outOfScope: e.out_of_scope } }

    case 'thinking':
      return { ...run, thinking: run.thinking + e.delta }

    case 'token':
      return { ...run, answer: run.answer + e.delta }

    case 'llm_end':
      return { ...run, answerStats: e.stats }

    case 'llm_retry': {
      const retry = { node: e.node, attempt: e.attempt, max: e.max_retries, delay: e.delay, reason: e.reason }
      // 답변 재시도면 실패한 시도의 부분 출력은 버리고 다시 받는다
      return e.node === 'answer'
        ? { ...run, thinking: '', answer: '', retries: [...run.retries, retry] }
        : { ...run, retries: [...run.retries, retry] }
    }

    case 'error':
      return { ...run, error: e.message }

    case 'run_end':
      return { ...run, endTs: e.ts, totalMs: e.ms, activeNode: null }
  }
}

export type StepState = 'done' | 'run' | 'wait' | 'skip'

export interface ProcessStep {
  key: string
  title: string
  detail: string
  state: StepState
}

function visited(run: Run, node: NodeName) {
  return run.spans.some((s) => s.node === node)
}

function stateOf(run: Run, node: NodeName): StepState {
  if (run.activeNode === node) return 'run'
  if (visited(run, node)) return 'done'
  return run.endTs ? 'skip' : 'wait'
}

/** 채팅의 "처리 단계" 상자에 보여 줄 단계. 서버 노드 이벤트에서 만든다. */
export function processSteps(run: Run): ProcessStep[] {
  const a = run.analysis
  const route = run.routes.at(-1)
  const plan = run.plans.at(-1)
  const cited = citedNumbers(run.answer)
  const steps: ProcessStep[] = [
    {
      key: 'analyze',
      title: '질의 분석',
      detail: !a ? '' : a.outOfScope ? `범위 밖 · ${a.reason}` : a.queries.map((q) => `“${q}”`).join(' · '),
      state: stateOf(run, 'analyze'),
    },
    {
      key: 'route',
      title: '문서 선택',
      detail: !route
        ? ''
        : `후보 ${route.candidates.length}건 → ${route.names?.length ? route.names.join(', ') : route.docIds ? '해당 없음' : '선택 중'}` +
          (run.routes.length > 1 ? ` (재검색 ${run.routes.length - 1}회)` : ''),
      state: stateOf(run, 'route'),
    },
    {
      key: 'plan',
      title: '청크 선택',
      detail: !plan?.chunkIds ? '' : plan.chunkIds.length ? `${plan.chunkIds.length}개 · ${plan.labels?.[0] ?? ''}` : '근거 없음',
      state: stateOf(run, 'plan'),
    },
    {
      key: 'read',
      title: '원문 조회',
      detail: run.evidence.length ? `근거 ${run.evidence.length}건 (이웃 포함)` : '',
      state: stateOf(run, 'read'),
    },
  ]
  if (run.noReference) {
    steps.push({ key: 'no_reference', title: '참조문서 없음', detail: run.noReference.reason, state: stateOf(run, 'no_reference') })
  } else {
    steps.push({
      key: 'answer',
      title: '답변 작성',
      detail: run.answerStats ? `인용 ${cited.length}건 · ${(run.answerStats.ms / 1000).toFixed(1)}초` : '',
      state: stateOf(run, 'answer'),
    })
  }
  return steps
}

/** 답변 본문의 [n] 인용 번호 (중복 제거, 등장 순) */
export function citedNumbers(text: string): number[] {
  const seen = new Set<number>()
  for (const m of text.matchAll(/\[(\d{1,2})\]/g)) seen.add(Number(m[1]))
  return [...seen]
}
