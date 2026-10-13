import type { AgentEvent, Run, Step } from './types'

export function newRun(id: string, prompt: string, thinkingEnabled: boolean): Run {
  return {
    id,
    prompt,
    thinkingEnabled,
    steps: [],
    activeNode: null,
    activeTool: null,
    lastNode: '__start__',
    traversed: [],
    nodeVisits: {},
    toolCalls: {},
  }
}

function updateLast<K extends Step['kind']>(
  steps: Step[],
  kind: K,
  fn: (s: Extract<Step, { kind: K }>) => Extract<Step, { kind: K }>,
  match?: (s: Extract<Step, { kind: K }>) => boolean,
): Step[] {
  for (let i = steps.length - 1; i >= 0; i--) {
    const s = steps[i]
    if (s.kind === kind && (!match || match(s as Extract<Step, { kind: K }>))) {
      const next = steps.slice()
      next[i] = fn(s as Extract<Step, { kind: K }>)
      return next
    }
  }
  return steps
}

export function applyEvent(run: Run, e: AgentEvent): Run {
  switch (e.type) {
    case 'run_start':
      return { ...run, startTs: e.ts }

    case 'node_start': {
      const steps = run.steps.slice()
      if (e.node === 'planner') steps.push({ kind: 'plan', id: `plan-${steps.length}`, t: e.ts, steps: [], retries: [] })
      if (e.node === 'agent')
        steps.push({
          kind: 'llm',
          id: `llm-${steps.length}`,
          t: e.ts,
          thinking: '',
          text: '',
          toolCalls: [],
          retries: [],
        })
      return {
        ...run,
        steps,
        activeNode: e.node,
        lastNode: e.node,
        traversed: [...run.traversed, `${run.lastNode}->${e.node}`],
        nodeVisits: { ...run.nodeVisits, [e.node]: (run.nodeVisits[e.node] ?? 0) + 1 },
      }
    }

    case 'node_end':
      return { ...run, activeNode: null }

    case 'plan':
      return {
        ...run,
        steps: updateLast(run.steps, 'plan', (s) => ({ ...s, steps: e.steps, stats: e.stats, endT: e.ts })),
      }

    case 'thinking':
      return { ...run, steps: updateLast(run.steps, 'llm', (s) => ({ ...s, thinking: s.thinking + e.delta })) }

    case 'token':
      return { ...run, steps: updateLast(run.steps, 'llm', (s) => ({ ...s, text: s.text + e.delta })) }

    case 'llm_end':
      return {
        ...run,
        steps: updateLast(run.steps, 'llm', (s) => ({ ...s, toolCalls: e.tool_calls, stats: e.stats, endT: e.ts })),
      }

    case 'tool_call':
      return {
        ...run,
        activeTool: e.name,
        toolCalls: { ...run.toolCalls, [e.name]: (run.toolCalls[e.name] ?? 0) + 1 },
        steps: [...run.steps, { kind: 'tool', id: e.id, t: e.ts, name: e.name, server: e.server, args: e.args }],
      }

    case 'tool_result':
      return {
        ...run,
        activeTool: null,
        steps: updateLast(
          run.steps,
          'tool',
          (s) => ({ ...s, result: e.content, error: e.error, ms: e.ms, endT: e.ts }),
          (s) => s.id === e.id,
        ),
      }

    case 'llm_retry': {
      const retry = { attempt: e.attempt, max: e.max_retries, delay: e.delay, reason: e.reason }
      if (e.node === 'planner')
        return { ...run, steps: updateLast(run.steps, 'plan', (s) => ({ ...s, retries: [...s.retries, retry] })) }
      // 실패한 시도의 부분 출력은 버리고 다시 받는다
      return {
        ...run,
        steps: updateLast(run.steps, 'llm', (s) => ({ ...s, thinking: '', text: '', retries: [...s.retries, retry] })),
      }
    }

    case 'error':
      return { ...run, error: e.message }

    case 'run_end':
      return {
        ...run,
        endTs: e.ts,
        activeNode: null,
        activeTool: null,
        traversed: run.error ? run.traversed : [...run.traversed, `${run.lastNode}->__end__`],
      }
  }
}

/** 최종 답변 텍스트: 마지막 LLM 단계의 출력 */
export function answerOf(run: Run): string {
  for (let i = run.steps.length - 1; i >= 0; i--) {
    const s = run.steps[i]
    if (s.kind === 'llm' && s.text) return s.text
  }
  return ''
}
