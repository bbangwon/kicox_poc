import { useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { answerOf } from '../runReducer'
import type { Run } from '../types'

const SAMPLES = [
  '교육을 이틀 동안 3시간씩 들었는데 몇 학점 인정받을 수 있어?',
  '토익 학원비도 지원돼? 과세야?',
  '필라테스 강습비도 개인교육훈련비로 지원받을 수 있어?',
  '교육신청은 어디서 해?',
  '안녕! 너는 어떤 일을 할 수 있어?',
]

interface Props {
  runs: Run[]
  selectedId: string | null
  busy: boolean
  thinking: boolean
  onThinkingChange: (v: boolean) => void
  onSelect: (id: string) => void
  onSend: (text: string) => void
  onStop: () => void
  onReset: () => void
}

export function ChatPanel(p: Props) {
  const [input, setInput] = useState('')
  const bottom = useRef<HTMLDivElement>(null)
  const last = p.runs.at(-1)
  const lastAnswerLength = last ? answerOf(last).length : 0

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [p.runs.length, lastAnswerLength])

  const send = (text: string) => {
    if (!text.trim() || p.busy) return
    p.onSend(text.trim())
    setInput('')
  }

  return (
    <div className="chat">
      <div className="messages">
        {p.runs.length === 0 && (
          <div className="samples">
            <div className="samples-title">예시 요청</div>
            {SAMPLES.map((s) => (
              <button key={s} onClick={() => send(s)} disabled={p.busy}>
                {s}
              </button>
            ))}
          </div>
        )}
        {p.runs.map((run) => {
          const answer = answerOf(run)
          return (
            <div
              key={run.id}
              className={`turn ${run.id === p.selectedId ? 'selected' : ''}`}
              onClick={() => p.onSelect(run.id)}
            >
              <div className="bubble user">{run.prompt}</div>
              <div className="bubble assistant">
                {answer ? (
                  <Markdown remarkPlugins={[remarkGfm]}>{answer}</Markdown>
                ) : run.error ? (
                  <span className="err">오류: {run.error}</span>
                ) : run.endTs ? (
                  <span className="muted">(응답 없음)</span>
                ) : (
                  <span className="muted">
                    <span className="spinner" /> {run.activeTool ? `${run.activeTool} 실행 중` : run.activeNode ?? '대기'}…
                  </span>
                )}
              </div>
            </div>
          )
        })}
        <div ref={bottom} />
      </div>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault()
          send(input)
        }}
      >
        <textarea
          value={input}
          placeholder="요청을 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)"
          rows={3}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              send(input)
            }
          }}
        />
        <div className="composer-bar">
          <label>
            <input type="checkbox" checked={p.thinking} onChange={(e) => p.onThinkingChange(e.target.checked)} />{' '}
            Thinking
          </label>
          <button type="button" className="ghost" onClick={p.onReset} disabled={p.busy}>
            새 대화
          </button>
          {p.busy ? (
            <button type="button" onClick={p.onStop}>
              중지
            </button>
          ) : (
            <button type="submit" disabled={!input.trim()}>
              전송
            </button>
          )}
        </div>
      </form>
    </div>
  )
}
