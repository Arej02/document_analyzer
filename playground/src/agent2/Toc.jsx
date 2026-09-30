// Sticky table of contents with three visual variants: minimal, numbered, progress.
import { ChevronRight, CircleCheck, CircleDot, Circle, List } from 'lucide-react'

export default function Toc({ sections, activeId, activeSection, expanded, onToggle, onNavigate, variant, seen, extras, footer, open, onOpen }) {
  return (
    <aside className={`toc toc-${variant}`} aria-label="Document outline">
      <button type="button" className="btn toc-mobile-toggle" aria-expanded={open} aria-controls="toc-list" onClick={onOpen}>
        <List size={14} className="icon" aria-hidden="true" />On this page
      </button>
      <nav id="toc-list" className="toc-nav" data-open={open} aria-label="Table of contents">
        <ol className="toc-list">
          {sections.map((s, i) => {
            const isOpen = expanded.has(s.id)
            const inSection = activeSection === s.id
            const isSeen = seen > i
            return (
              <li key={s.id} className="toc-group">
                <div className="toc-row" data-active={activeId === s.id} data-in={inSection}>
                  {s.subs.length ? (
                    <button type="button" className="toc-chev" aria-expanded={isOpen} aria-label={`${isOpen ? 'Collapse' : 'Expand'} ${s.title}`} onClick={() => onToggle(s.id)}>
                      <ChevronRight size={14} className="icon" data-open={isOpen} aria-hidden="true" />
                    </button>
                  ) : <span className="toc-chev-space" />}
                  <a href={`#${s.id}`} className="toc-link" aria-current={activeId === s.id ? 'location' : undefined} onClick={(e) => { e.preventDefault(); onNavigate(s.id) }}>
                    {variant === 'numbered' ? <span className="toc-num tnum">{i + 1}</span> : null}
                    {variant === 'progress' ? (
                      <span className="toc-mark" aria-hidden="true">
                        {inSection ? <CircleDot size={14} className="icon" /> : isSeen ? <CircleCheck size={14} className="icon" /> : <Circle size={14} className="icon" />}
                      </span>
                    ) : null}
                    <span className="toc-text">{s.title}</span>
                    {variant === 'progress' && extras[s.id] ? <span className="toc-extra tnum">{extras[s.id]}</span> : null}
                    {variant === 'progress' ? <span className="sr-only">{inSection ? ' (current section)' : isSeen ? ' (read)' : ' (not read yet)'}</span> : null}
                  </a>
                </div>
                {s.subs.length && isOpen ? (
                  <ol className="toc-subs">
                    {s.subs.map((sub, j) => (
                      <li key={sub.id}>
                        <a href={`#${sub.id}`} className="toc-sublink" data-active={activeId === sub.id} aria-current={activeId === sub.id ? 'location' : undefined} onClick={(e) => { e.preventDefault(); onNavigate(sub.id) }}>
                          {variant === 'numbered' ? <span className="toc-num tnum">{i + 1}.{j + 1}</span> : null}
                          <span className="toc-text">{sub.title}</span>
                        </a>
                      </li>
                    ))}
                  </ol>
                ) : null}
              </li>
            )
          })}
        </ol>
        <p className="toc-footer muted">{footer}</p>
      </nav>
    </aside>
  )
}
