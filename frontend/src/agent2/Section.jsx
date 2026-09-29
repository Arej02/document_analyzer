// A collapsible plan-document section (tasks_frontend.md A2.2). The heading is a button that
// owns `aria-expanded` / `aria-controls`. Collapsed sections unmount their body, so a section
// that is opened by the outline (see `useOutline`'s `beforeNavigate`) is in the DOM before we scroll.
import { ChevronDown, ChevronRight } from 'lucide-react'
import './agent2.css'

export default function Section({ id, index, title, meta, collapsed, onToggle, children }) {
  return (
    <section id={id} className="a2-section" aria-labelledby={`${id}-h`}>
      <h2 id={`${id}-h`} className="a2-h2">
        <button type="button" className="a2-toggle" aria-expanded={!collapsed} aria-controls={`${id}-body`} onClick={() => onToggle(id)}>
          {collapsed ? <ChevronRight size={18} className="ui-icon" aria-hidden="true" /> : <ChevronDown size={18} className="ui-icon" aria-hidden="true" />}
          <span className="a2-num" aria-hidden="true">{index}</span>
          <span>{title}</span>
        </button>
        {meta ? <span className="a2-meta">{meta}</span> : null}
      </h2>
      {collapsed ? null : <div id={`${id}-body`} className="a2-body">{children}</div>}
    </section>
  )
}
