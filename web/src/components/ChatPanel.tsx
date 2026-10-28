import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { citedNumbers, processSteps } from '../runReducer'
import type { Run } from '../types'
import { CheckIcon, ChevronIcon, SendIcon, SparkleIcon, StopIcon } from './icons'

const MAX_LENGTH = 2000

const SUGGESTIONS = [
  '복지포인트는 언제까지 써야 해요?',
  '질병으로 휴직하면 월급 얼마나 나와요?',
  '퇴직하면 복지포인트는 어떻게 되나요?',
  '출장 가면 일비 얼마예요?',
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
  onCite: (runId: string, n: number) => void
}

function clock(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })
}

function seconds(run: Run) {
  const end = run.endTs ?? Date.now() / 1000
  return ((end - (run.startTs ?? run.sentAt)) || 0).toFixed(1)
}

/** 처리 단계 상자 (원 프로젝트 ThinkBlock). 실행 중에는 펼치고, 끝나면 접는다. */
function ThinkBlock({ run }: { run: Run }) {
  const running = !run.endTs
  const [open, setOpen] = useState<boolean | null>(null)
  const isOpen = open ?? running
  const steps = processSteps(run)
  const current = steps.find((s) => s.state === 'run')
  const title = running
    ? `${current?.title ?? '대기'} 중…`
    : run.error
      ? '처리 중 오류'
      : run.noReference
        ? '참조문서 없음'
        : `처리 완료 · ${run.evidence.length}개 근거`

  return (
    <div className="think" data-state={running ? 'RUNNING' : run.error ? 'FAILED' : 'DONE'} data-open={isOpen || undefined}>
      <button type="button" className="think-head" onClick={() => setOpen(!isOpen)} aria-expanded={isOpen}>
        <span className="think-icon">
          <SparkleIcon size={14} />
        </span>
        <span className="think-title">{title}</span>
        <span className="think-time">{seconds(run)}초</span>
        <span className="chev">
          <ChevronIcon size={14} />
        </span>
      </button>
      {isOpen && (
        <ol className="steps">
          {steps.map((s) => (
            <li key={s.key} className="step" data-s={s.state}>
              <span className="step-icon">{s.state === 'done' && <CheckIcon />}</span>
              <span className="step-text">
                <span className="step-title">{s.title}</span>
                {s.detail && <span className="step-detail">{s.detail}</span>}
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/** 답변 본문: [n] 인용을 칩으로 바꿔 근거 카드와 잇는다 */
function Answer({ run, onCite }: { run: Run; onCite: (n: number) => void }) {
  const known = new Set(run.evidence.map((e) => e.n))
  const text = run.answer.replace(/\[(\d{1,2})\]/g, (m, n) => (known.has(Number(n)) ? `[${n}](#cite-${n})` : m))
  return (
    <div className={`md ${run.endTs ? '' : 'caret'}`}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            const n = href?.startsWith('#cite-') ? Number(href.slice(6)) : null
            if (n === null) return <a href={href}>{children}</a>
            return (
              <button
                type="button"
                className="cite"
                onClick={(e) => {
                  e.stopPropagation()
                  onCite(n)
                }}
                title="근거 보기"
              >
                {n}
              </button>
            )
          },
        }}
      >
        {text}
      </Markdown>
    </div>
  )
}

function AiTurn({ run, onCite }: { run: Run; onCite: (n: number) => void }) {
  const cited = citedNumbers(run.answer)
  const unverified = run.evidence.filter((e) => cited.includes(e.n) && e.has_numbers && !e.verified)
  return (
    <div className="ai">
      <span className="avatar">
        <SparkleIcon size={15} />
      </span>
      <div className="ai-body">
        <div className="ai-name">
          규정 도우미 <span className="ai-time">{clock(run.startTs ?? run.sentAt)}</span>
        </div>
        <ThinkBlock run={run} />
        {run.answer && <Answer run={run} onCite={onCite} />}
        {run.error && <div className="error">오류: {run.error}</div>}
        {run.endTs && !run.answer && !run.error && <div className="stopped">(응답 없음)</div>}
        {run.endTs && (
          <div className="chips">
            {run.answer && !run.noReference && (
              <span className="mini-chip" data-tone={cited.length ? undefined : 'warn'}>
                인용 {cited.length} / 근거 {run.evidence.length}
              </span>
            )}
            {unverified.length > 0 && (
              <span className="mini-chip" data-tone="warn" title="사람 검증 전인 수치가 든 근거를 인용했습니다">
                검증 전 수치 포함 {unverified.map((e) => `[${e.n}]`).join('')}
              </span>
            )}
            {run.totalMs != null && <span className="mini-chip">{(run.totalMs / 1000).toFixed(1)}초</span>}
          </div>
        )}
      </div>
    </div>
  )
}

export function ChatPanel(p: Props) {
  const [input, setInput] = useState('')
  const bottom = useRef<HTMLDivElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const last = p.runs.at(-1)

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [p.runs.length, last?.answer.length, last?.spans.length])

  // 입력 높이 자동 조절 (최대 160px, CSS max-height)
  useLayoutEffect(() => {
    const el = textarea.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [input])

  const send = (text: string) => {
    if (!text.trim() || p.busy) return
    p.onSend(text.trim())
    setInput('')
  }

  return (
    <div className="chat">
      <div className="column-head">
        <span className="step-dot">
          <SparkleIcon size={12} />
        </span>
        <h2 className="column-title">규정 Q&A</h2>
        <span className="net" title="답변은 적재된 규정 원문만 근거로 작성합니다">
          근거 기반
        </span>
      </div>

      <div className="msgs" role="log" aria-live="polite">
        {p.runs.length === 0 && (
          <div className="welcome">
            <div className="welcome-title">내부규정에 대해 물어보세요</div>
            <div className="welcome-text">
              질문을 분석해 문서를 고르고, 원문 청크를 찾아 근거 번호와 함께 답합니다. 왼쪽 시나리오로 시험하거나 직접
              입력하세요.
            </div>
            <div className="chips">
              {SUGGESTIONS.map((s) => (
                <button key={s} type="button" className="sug" onClick={() => send(s)} disabled={p.busy}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {p.runs.map((run) => (
          <div
            key={run.id}
            className="turn"
            data-selected={run.id === p.selectedId || undefined}
            onClick={() => p.onSelect(run.id)}
          >
            <div className="user">
              <div className="bubble">{run.prompt}</div>
              <span className="user-meta">
                {clock(run.sentAt)} · Thinking {run.thinkingEnabled ? 'on' : 'off'}
              </span>
            </div>
            <AiTurn run={run} onCite={(n) => p.onCite(run.id, n)} />
          </div>
        ))}
        <div ref={bottom} />
      </div>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault()
          send(input)
        }}
      >
        <div className="box">
          <textarea
            ref={textarea}
            className="input"
            value={input}
            maxLength={MAX_LENGTH}
            rows={1}
            placeholder="규정에 대해 질문하세요"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                send(input)
              }
            }}
          />
          <div className="box-foot">
            <label className="toggle" title="답변 단계에서 모델의 Thinking을 켭니다 (분석·선택 단계는 항상 off)">
              <input type="checkbox" checked={p.thinking} onChange={(e) => p.onThinkingChange(e.target.checked)} />
              Thinking
            </label>
            <span className="count">
              {input.length}/{MAX_LENGTH}
            </span>
            {p.busy ? (
              <button type="button" className="send" data-stop onClick={p.onStop} aria-label="중지">
                <StopIcon />
              </button>
            ) : (
              <button type="submit" className="send" disabled={!input.trim()} aria-label="보내기">
                <SendIcon />
              </button>
            )}
          </div>
        </div>
        <div className="note">Enter 보내기 · Shift+Enter 줄바꿈 · 답변은 적재된 규정 원문만 근거로 합니다</div>
      </form>
    </div>
  )
}
