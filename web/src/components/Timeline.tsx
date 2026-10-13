import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { LlmStats, Retry, Run, Step } from '../types'

function sec(ms: number) {
  return `${(ms / 1000).toFixed(1)}s`
}

function Stats({ stats }: { stats?: LlmStats }) {
  if (!stats) return null
  return (
    <span className="stats">
      {stats.input_tokens != null && <>in {stats.input_tokens} · </>}
      {stats.output_tokens != null && <>out {stats.output_tokens} · </>}
      {stats.tps != null && <>{stats.tps} tok/s</>}
    </span>
  )
}

function Retries({ retries }: { retries: Retry[] }) {
  if (!retries.length) return null
  return (
    <ul className="retries">
      {retries.map((r) => (
        <li key={r.attempt}>
          ↻ LLM 서버 오류로 재시도 {r.attempt}/{r.max} ({r.delay}s 대기) — <span className="reason">{r.reason}</span>
        </li>
      ))}
    </ul>
  )
}

function pretty(text: string) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

function StepCard({ step, index, run, now }: { step: Step; index: number; run: Run; now: number }) {
  const start = run.startTs ?? step.t
  const total = Math.max(((run.endTs ?? now) - start) * 1000, 1)
  const offset = (step.t - start) * 1000
  const dur = ((step.endT ?? now) - step.t) * 1000
  const running = step.endT === undefined && !run.endTs

  const bar = (
    <div className="gantt">
      <div
        className={`gantt-bar ${step.kind}`}
        style={{ left: `${(offset / total) * 100}%`, width: `${Math.max((dur / total) * 100, 0.8)}%` }}
      />
    </div>
  )

  const head = (icon: string, title: ReactNode, extra?: ReactNode) => (
    <div className="step-head">
      <span className="step-idx">{index + 1}</span>
      <span className={`step-icon ${step.kind}`}>{icon}</span>
      <span className="step-title">{title}</span>
      {extra}
      <span className="step-time">
        +{sec(offset)} · {running ? <span className="spinner" /> : sec(dur)}
      </span>
    </div>
  )

  if (step.kind === 'plan') {
    return (
      <div className="step">
        {head('▤', 'planner · 실행 계획', <Stats stats={step.stats} />)}
        {bar}
        <Retries retries={step.retries} />
        {step.steps.length > 0 && (
          <ol className="plan">
            {step.steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        )}
      </div>
    )
  }

  if (step.kind === 'llm') {
    const decision = running
      ? '생각 중…'
      : step.toolCalls.length
        ? `→ ${step.toolCalls.map((t) => t.name).join(', ')}`
        : '→ 최종 답변'
    return (
      <div className="step">
        {head('◆', <>agent · LLM <span className="decision">{decision}</span></>, <Stats stats={step.stats} />)}
        {bar}
        <Retries retries={step.retries} />
        {step.thinking && (
          <details open={running}>
            <summary>Thinking ({step.thinking.length.toLocaleString()}자)</summary>
            <pre className="thinking">{step.thinking}</pre>
          </details>
        )}
      </div>
    )
  }

  return (
    <div className={`step ${step.error ? 'error' : ''}`}>
      {head(
        '⚙',
        <>
          tools · <code>{step.name}</code>
        </>,
        <span className="badge">MCP · {step.server}</span>,
      )}
      {bar}
      <div className="io">
        <div>
          <div className="io-label">arguments</div>
          <pre>{JSON.stringify(step.args, null, 2)}</pre>
        </div>
        <div>
          <div className="io-label">{step.error ? 'error' : 'result'}</div>
          <pre>{step.result === undefined ? '…' : pretty(step.result)}</pre>
        </div>
      </div>
    </div>
  )
}

export function Timeline({ run }: { run: Run | null }) {
  const [now, setNow] = useState(() => Date.now() / 1000)
  const bottom = useRef<HTMLDivElement>(null)
  const running = run && !run.endTs

  useEffect(() => {
    if (!running) return
    const t = setInterval(() => setNow(Date.now() / 1000), 100)
    return () => clearInterval(t)
  }, [running])

  useEffect(() => {
    if (running) bottom.current?.scrollIntoView({ block: 'end' })
  }, [run?.steps.length, running])

  if (!run) return <div className="empty">실행 기록이 없습니다. 왼쪽에서 요청을 보내보세요.</div>

  const toolSteps = run.steps.filter((s) => s.kind === 'tool')
  const llmSteps = run.steps.filter((s) => s.kind === 'llm' || s.kind === 'plan')
  const totalMs = ((run.endTs ?? now) - (run.startTs ?? now)) * 1000

  return (
    <div className="timeline">
      <div className="summary">
        <span>
          총 <b>{sec(totalMs)}</b>
        </span>
        <span>
          LLM 호출 <b>{llmSteps.length}</b>
        </span>
        <span>
          MCP 호출 <b>{toolSteps.length}</b>
        </span>
        <span>Thinking {run.thinkingEnabled ? 'ON' : 'OFF'}</span>
        {running && <span className="live">● 실행 중</span>}
      </div>
      {run.steps.map((s, i) => (
        <StepCard key={s.id} step={s} index={i} run={run} now={now} />
      ))}
      {run.error && <div className="step error">오류: {run.error}</div>}
      <div ref={bottom} />
    </div>
  )
}
