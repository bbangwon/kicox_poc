import type { Info } from '../types'
import { BookIcon } from './icons'

/** 원 프로젝트 셸 사이드바 모양: 로고 밴드 + 메뉴 그룹 + 적재 문서 목록 */
export function Sidebar({ info }: { info: Info | null }) {
  return (
    <aside className="side">
      <div className="side-band">
        <span className="logo">
          KICOX <b>AI</b>
        </span>
        <span className="tag">PoC</span>
      </div>
      <nav className="side-nav" aria-label="메뉴">
        <div className="side-group">
          <span className="side-group-icon">
            <BookIcon size={18} />
          </span>
          내부규정
        </div>
        <div className="side-children">
          <span className="side-child active" aria-current="page">
            규정 Q&A 테스트
          </span>
        </div>

        <div className="side-section">적재 문서 (현행)</div>
        <ul className="side-docs">
          {info?.documents.map((d) => (
            <li key={d.doc_id} title={d.doc_id}>
              <span className="side-doc-name">{d.name}</span>
              <span className="side-doc-meta">
                {d.amended_on ?? '개정일 없음'} · 청크 {d.chunks}
              </span>
            </li>
          ))}
          {info && info.documents.length === 0 && <li className="side-doc-meta">색인된 문서가 없습니다</li>}
        </ul>
      </nav>
    </aside>
  )
}
