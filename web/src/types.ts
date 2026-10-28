export interface LlmStats {
  ms: number
  input_tokens: number | null
  output_tokens: number | null
  tps: number | null
  finish_reason?: string | null
}

export interface DocCandidate {
  doc_id: string
  name: string
  summary: string
  not_for: string | null
  score: number
  /** "q1:dense" → 1부터 순위 */
  ranks: Record<string, number>
  hit: string
  excluded: boolean
}

export interface SectionHit {
  section_id: string
  doc_name: string
  title: string
  score: number
  summary_hit: boolean
  child_hits: number
}

export interface Evidence {
  n: number
  chunk_id: string
  doc_name: string
  section_title: string
  header: string
  body: string
  page_start: number | null
  page_end: number | null
  has_numbers: boolean
  verified: boolean
  neighbor: boolean
}

export type NodeName = 'analyze' | 'route' | 'plan' | 'read' | 'answer' | 'no_reference'

export type AgentEvent =
  | { type: 'run_start'; ts: number; thread_id: string }
  | { type: 'node_start'; ts: number; node: NodeName }
  | { type: 'node_end'; ts: number; node: NodeName; ms: number }
  | { type: 'analysis'; ts: number; out_of_scope: boolean; reason: string; queries: string[]; stats: LlmStats }
  | { type: 'doc_candidates'; ts: number; ms: number; dense: boolean; attempt: number; items: DocCandidate[] }
  | { type: 'route'; ts: number; doc_ids: string[]; names: string[]; reason: string; stats: LlmStats }
  | { type: 'chunk_hits'; ts: number; ms: number; sections: SectionHit[] }
  | { type: 'plan'; ts: number; chunk_ids: string[]; labels: string[]; reason: string; stats: LlmStats }
  | { type: 'evidence'; ts: number; items: Evidence[] }
  | { type: 'no_reference'; ts: number; reason: string; out_of_scope: boolean }
  | { type: 'thinking'; ts: number; delta: string }
  | { type: 'token'; ts: number; delta: string }
  | { type: 'llm_end'; ts: number; stats: LlmStats }
  | {
      type: 'llm_retry'
      ts: number
      node: NodeName
      attempt: number
      max_retries: number
      delay: number
      reason: string
    }
  | { type: 'run_end'; ts: number; ms: number }
  | { type: 'error'; ts: number; message: string }

export interface DocumentInfo {
  doc_id: string
  name: string
  amended_on: string | null
  chunks: number
}

export interface Info {
  model: string
  dense: boolean
  documents: DocumentInfo[]
  nodes: string[]
}

export interface NodeSpan {
  node: NodeName
  t: number
  ms?: number
}

/** route 한 번 = 문서 후보 검색 + router 선택 */
export interface RouteAttempt {
  attempt: number
  ms: number
  dense: boolean
  candidates: DocCandidate[]
  docIds?: string[]
  names?: string[]
  reason?: string
  stats?: LlmStats
}

/** plan 한 번 = 청크 검색 + planner 선택 */
export interface PlanAttempt {
  ms?: number
  sections: SectionHit[]
  chunkIds?: string[]
  labels?: string[]
  reason?: string
  stats?: LlmStats
}

export interface Retry {
  node: NodeName
  attempt: number
  max: number
  delay: number
  reason: string
}

export interface Run {
  id: string
  prompt: string
  thinkingEnabled: boolean
  sentAt: number
  startTs?: number
  endTs?: number
  totalMs?: number
  spans: NodeSpan[]
  activeNode: NodeName | null
  analysis?: { outOfScope: boolean; reason: string; queries: string[]; stats: LlmStats }
  routes: RouteAttempt[]
  plans: PlanAttempt[]
  evidence: Evidence[]
  noReference?: { reason: string; outOfScope: boolean }
  thinking: string
  answer: string
  answerStats?: LlmStats
  retries: Retry[]
  error?: string
}
