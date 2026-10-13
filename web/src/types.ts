export interface LlmStats {
  ms: number
  input_tokens: number | null
  output_tokens: number | null
  tps: number | null
}

export type AgentEvent =
  | { type: 'run_start'; ts: number; thread_id: string }
  | { type: 'node_start'; ts: number; node: string }
  | { type: 'node_end'; ts: number; node: string; ms: number }
  | { type: 'plan'; ts: number; steps: string[]; stats: LlmStats }
  | { type: 'thinking'; ts: number; delta: string }
  | { type: 'token'; ts: number; delta: string }
  | { type: 'llm_end'; ts: number; tool_calls: { id: string; name: string }[]; stats: LlmStats }
  | { type: 'tool_call'; ts: number; id: string; name: string; server: string; args: Record<string, unknown> }
  | {
      type: 'tool_result'
      ts: number
      id: string
      name: string
      server: string
      content: string
      error: boolean
      ms: number
    }
  | {
      type: 'llm_retry'
      ts: number
      node: 'planner' | 'agent'
      attempt: number
      max_retries: number
      delay: number
      reason: string
    }
  | { type: 'run_end'; ts: number; ms: number }
  | { type: 'error'; ts: number; message: string }

export interface GraphInfo {
  model: string
  nodes: { id: string }[]
  edges: { source: string; target: string; conditional: boolean }[]
  mermaid: string
  servers: { name: string; url: string }[]
  tools: { name: string; server: string; description: string }[]
}

export interface Retry {
  attempt: number
  max: number
  delay: number
  reason: string
}

export type Step =
  | { kind: 'plan'; id: string; t: number; endT?: number; steps: string[]; stats?: LlmStats; retries: Retry[] }
  | {
      kind: 'llm'
      id: string
      t: number
      endT?: number
      thinking: string
      text: string
      toolCalls: { id: string; name: string }[]
      stats?: LlmStats
      retries: Retry[]
    }
  | {
      kind: 'tool'
      id: string
      t: number
      endT?: number
      name: string
      server: string
      args: Record<string, unknown>
      result?: string
      error?: boolean
      ms?: number
    }

export interface Run {
  id: string
  prompt: string
  thinkingEnabled: boolean
  startTs?: number
  endTs?: number
  steps: Step[]
  /** 현재 실행 중인 그래프 노드 */
  activeNode: string | null
  /** 실행 중인 MCP 도구 이름 */
  activeTool: string | null
  lastNode: string
  /** "source->target" 형태로 지나간 엣지 (순서 유지) */
  traversed: string[]
  nodeVisits: Record<string, number>
  toolCalls: Record<string, number>
  error?: string
}
