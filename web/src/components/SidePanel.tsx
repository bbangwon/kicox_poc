import type { Run } from '../types'
import { EvidenceList } from './EvidenceList'
import { TracePanel } from './TracePanel'

export type SideTab = 'evidence' | 'trace'

interface Props {
  run: Run | null
  tab: SideTab
  onTab: (t: SideTab) => void
  focus: { n: number; nonce: number } | null
}

const TABS: { id: SideTab; label: string }[] = [
  { id: 'evidence', label: '근거' },
  { id: 'trace', label: '처리 단계' },
]

export function SidePanel({ run, tab, onTab, focus }: Props) {
  return (
    <div className="side-panel">
      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            className="tab"
            aria-selected={tab === t.id}
            onClick={() => onTab(t.id)}
          >
            {t.label}
            {t.id === 'evidence' && run && run.evidence.length > 0 && <span className="count-badge">{run.evidence.length}</span>}
          </button>
        ))}
      </div>
      <div className="side-scroll">
        {tab === 'evidence' ? <EvidenceList run={run} focus={focus} /> : <TracePanel run={run} />}
      </div>
    </div>
  )
}
