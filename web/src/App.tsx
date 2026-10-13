import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchGraph, streamChat } from './api'
import { ChatPanel } from './components/ChatPanel'
import { GraphView } from './components/GraphView'
import { Timeline } from './components/Timeline'
import { applyEvent, newRun } from './runReducer'
import type { GraphInfo, Run } from './types'
import './App.css'

export default function App() {
  const [graph, setGraph] = useState<GraphInfo | null>(null)
  const [graphError, setGraphError] = useState<string | null>(null)
  const [runs, setRuns] = useState<Run[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [threadId, setThreadId] = useState<string | null>(null)
  const [thinking, setThinking] = useState(true)
  const [busy, setBusy] = useState(false)
  const abort = useRef<AbortController | null>(null)

  const loadGraph = useCallback(() => {
    fetchGraph().then(
      (g) => {
        setGraph(g)
        setGraphError(null)
      },
      (e: Error) => setGraphError(e.message),
    )
  }, [])

  useEffect(loadGraph, [loadGraph])

  const send = async (text: string) => {
    const id = crypto.randomUUID()
    setRuns((rs) => [...rs, newRun(id, text, thinking)])
    setSelectedId(id)
    setBusy(true)
    const ctrl = new AbortController()
    abort.current = ctrl
    const update = (fn: (r: Run) => Run) => setRuns((rs) => rs.map((r) => (r.id === id ? fn(r) : r)))
    try {
      await streamChat(
        { message: text, thread_id: threadId, thinking },
        (e) => {
          if (e.type === 'run_start') setThreadId(e.thread_id)
          update((r) => applyEvent(r, e))
        },
        ctrl.signal,
      )
    } catch (err) {
      const message = ctrl.signal.aborted ? '사용자가 중지했습니다' : (err as Error).message
      update((r) => ({ ...r, error: message, endTs: Date.now() / 1000, activeNode: null, activeTool: null }))
    } finally {
      setBusy(false)
    }
  }

  const selected = runs.find((r) => r.id === selectedId) ?? runs.at(-1) ?? null

  return (
    <div className="app">
      <header>
        <h1>Agent Orchestration Viewer</h1>
        <div className="meta">
          {graph && (
            <>
              <span className="badge">{graph.model}</span>
              <span>LangGraph · FastMCP</span>
              <span>MCP 서버 {graph.servers.length} · 도구 {graph.tools.length}</span>
            </>
          )}
          {threadId && <span className="muted">thread {threadId.slice(0, 8)}</span>}
        </div>
      </header>

      <main>
        <section className="panel chat-panel">
          <ChatPanel
            runs={runs}
            selectedId={selected?.id ?? null}
            busy={busy}
            thinking={thinking}
            onThinkingChange={setThinking}
            onSelect={setSelectedId}
            onSend={send}
            onStop={() => abort.current?.abort()}
            onReset={() => {
              setRuns([])
              setThreadId(null)
              setSelectedId(null)
            }}
          />
        </section>

        <section className="panel graph-panel">
          <div className="panel-title">오케스트레이션 그래프</div>
          {graph ? (
            <GraphView info={graph} run={selected} />
          ) : (
            <div className="empty">
              {graphError ? (
                <>
                  백엔드 연결 실패: {graphError}
                  <button onClick={loadGraph}>다시 시도</button>
                </>
              ) : (
                '그래프 불러오는 중…'
              )}
            </div>
          )}
        </section>

        <section className="panel timeline-panel">
          <div className="panel-title">실행 타임라인</div>
          <Timeline run={selected} />
        </section>
      </main>
    </div>
  )
}
