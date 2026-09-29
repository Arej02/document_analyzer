// Outline for the plan document, progress variant (locked config, tasks_frontend.md). `variant="sidebar"` is the
// list alone, for the app sidebar's outline slot; the default draws it as its own column with the narrow-width disclosure.
// Sections are `{id, title, subs: [{id, title}]}`. State lives in `useOutline`.
import { ChevronRight, Circle, CircleCheck, CircleDot, List } from 'lucide-react'
import './agent2.css'

export default function Toc({ sections, activeId, activeSection, expanded, onToggle, onNavigate, seen, extras = {}, footer, open, onOpen, variant = 'inline' }) {
  const sidebar = variant === 'sidebar'
  const nav = (
      <nav id="a2-toc-list" className="a2-toc-nav" data-open={sidebar ? true : open} aria-label="Table of contents">
        <ol className="a2-toc-list">
          {sections.map((s, i) => {
            const isOpen = expanded.has(s.id)
            const inSection = activeSection === s.id
            const isSeen = seen > i
            const Mark = inSection ? CircleDot : isSeen ? CircleCheck : Circle
            return (
              <li key={s.id}>
                <div className="a2-toc-row" data-active={activeId === s.id} data-in={inSection}>
                  {s.subs.length ? (
                    <button type="button" className="a2-toc-chev" aria-expanded={isOpen} aria-label={`${isOpen ? 'Collapse' : 'Expand'} ${s.title}`} onClick={() => onToggle(s.id)}>
                      <ChevronRight size={14} className="ui-icon" data-open={isOpen} aria-hidden="true" />
                    </button>
                  ) : <span className="a2-toc-chev" aria-hidden="true" />}
                  <a href={`#${s.id}`} className="a2-toc-link" aria-current={activeId === s.id ? 'location' : undefined} onClick={(e) => { e.preventDefault(); onNavigate(s.id) }}>
                    <Mark size={14} className="ui-icon a2-toc-mark" aria-hidden="true" />
                    <span>{s.title}</span>
                    {extras[s.id] ? <span className="a2-toc-extra">{extras[s.id]}</span> : null}
                    <span className="a2-sr">{inSection ? ' (current section)' : isSeen ? ' (read)' : ' (not read yet)'}</span>
                  </a>
                </div>
                {s.subs.length && isOpen ? (
                  <ol className="a2-toc-subs">
                    {s.subs.map((sub) => (
                      <li key={sub.id}>
                        <a href={`#${sub.id}`} className="a2-toc-sublink" data-active={activeId === sub.id} aria-current={activeId === sub.id ? 'location' : undefined} onClick={(e) => { e.preventDefault(); onNavigate(sub.id) }}>{sub.title}</a>
                      </li>
                    ))}
                  </ol>
                ) : null}
              </li>
            )
          })}
        </ol>
        {footer ? <p className="a2-toc-footer">{footer}</p> : null}
      </nav>
  )
  if (sidebar) {
    return (
      <div className="a2-toc a2-toc--sidebar">
        <p className="sidebar-heading">Plan sections</p>
        {nav}
      </div>
    )
  }
  return (
    <aside className="a2-toc" aria-label="Document outline">
      <button type="button" className="a2-toc-toggle" aria-expanded={open} aria-controls="a2-toc-list" onClick={onOpen}>
        <List size={14} className="ui-icon" aria-hidden="true" />On this page
      </button>
      {nav}
    </aside>
  )
}
