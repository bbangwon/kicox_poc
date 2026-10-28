import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchInfo, streamChat } from './api'
import { ChatPanel } from './components/ChatPanel'
import { RefreshIcon } from './components/icons'
import { ScenarioPanel } from './components/ScenarioPanel'
import { Sidebar } from './components/Sidebar'
import { SidePanel, type SideTab } from './components/SidePanel'
import { applyEvent, newRun } from './runReducer'
import type { Info, Run } from './types'
import './App.css'

export default function App() {
  const [info, setInfo] = useState<Info | null>(null)
  const [infoError, setInfoError] = useState<string | null>(null)
  const [runs, setRuns] = useState<Run[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [threadId, setThreadId] = useState<string | null>(null)
  const [thinking, setThinking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState<SideTab>('evidence')
  const [focus, setFocus] = useState<{ n: number; nonce: number } | null>(null)
  const abort = useRef<AbortController | null>(null)
  // 시나리오가 단계를 연달아 보낼 때 setState 반영 전 값을 읽지 않도록 ref로도 둔다
  const thread = useRef<string | null>(null)

  const loadInfo = useCallback(() => {
    fetchInfo().then(
      (i) => {
        setInfo(i)
        setInfoError(null)
      },
      (e: Error) => setInfoError(e.message),
    )
  }, [])

  useEffect(loadInfo, [loadInfo])

  const reset = () => {
    setRuns([])
    setThreadId(null)
    thread.current = null
    setSelectedId(null)
    setFocus(null)
  }

  /** fresh=true면 이전 대화를 비우고 새 thread로 보낸다 (시나리오 첫 단계) */
  const send = async (text: string, fresh = false) => {
    const id = crypto.randomUUID()
    if (fresh) reset()
    setRuns((rs) => [...(fresh ? [] : rs), newRun(id, text, thinking)])
    setSelectedId(id)
    setFocus(null)
    setBusy(true)
    const ctrl = new AbortController()
    abort.current = ctrl
    const update = (fn: (r: Run) => Run) => setRuns((rs) => rs.map((r) => (r.id === id ? fn(r) : r)))
    try {
      await streamChat(
        { message: text, thread_id: fresh ? null : thread.current, thinking },
        (e) => {
          if (e.type === 'run_start') {
            thread.current = e.thread_id
            setThreadId(e.thread_id)
          }
          update((r) => applyEvent(r, e))
        },
        ctrl.signal,
      )
    } catch (err) {
      const message = ctrl.signal.aborted ? '사용자가 중지했습니다' : (err as Error).message
      update((r) => ({ ...r, error: message, endTs: Date.now() / 1000, activeNode: null }))
    } finally {
      setBusy(false)
    }
  }

  const selected = runs.find((r) => r.id === selectedId) ?? runs.at(-1) ?? null

  return (
    <div className="shell">
      <Sidebar info={info} />
      <div className="main">
        <header className="bar">
          <h1 className="service">내부규정</h1>
          <span className="sep">›</span>
          <span className="screen">규정 Q&A 테스트</span>
          <div className="right">
            {info && (
              <>
                <span className="pill">{info.model}</span>
                <span className="pill" data-tone={info.dense ? undefined : 'warning'}>
                  {info.dense ? 'dense + BM25' : 'BM25만 (임베딩 서버 없음)'}
                </span>
                <span className="user">문서 {info.documents.length}건</span>
              </>
            )}
            {infoError && (
              <span className="pill" data-tone="danger" title={infoError}>
                백엔드 연결 실패
                <button type="button" className="link" onClick={loadInfo}>
                  다시 시도
                </button>
              </span>
            )}
            {threadId && <span className="user mono">thread {threadId.slice(0, 8)}</span>}
            <button type="button" className="btn" onClick={reset} disabled={busy}>
              <RefreshIcon size={14} /> 새 대화
            </button>
          </div>
        </header>

        <main className="body">
          <section className="column">
            <ScenarioPanel busy={busy} onSend={send} />
          </section>
          <section className="column">
            <ChatPanel
              runs={runs}
              selectedId={selected?.id ?? null}
              busy={busy}
              thinking={thinking}
              onThinkingChange={setThinking}
              onSelect={setSelectedId}
              onSend={(t) => send(t)}
              onStop={() => abort.current?.abort()}
              onCite={(runId, n) => {
                setSelectedId(runId)
                setTab('evidence')
                setFocus({ n, nonce: Date.now() })
              }}
            />
          </section>
          <section className="column">
            <SidePanel run={selected} tab={tab} onTab={setTab} focus={focus} />
          </section>
        </main>
      </div>
    </div>
  )
}
