// Explains why review controls are (or are not) available. Icon + title + text, never colour alone.
// `extra` is for facts only the page knows, e.g. "by you at 3:04 pm" on an approved plan.
import { CircleAlert, CircleCheck, CircleX, Info } from 'lucide-react'
import './agent2.css'

const ICON = { warning: CircleAlert, success: CircleCheck, danger: CircleX, neutral: Info }

export default function PlanStatusBanner({ access, extra = null }) {
  const b = access?.banner
  if (!b) return null
  const Icon = ICON[b.tone] ?? Info
  return (
    <div className="a2-banner" data-tone={b.tone} role="status">
      <Icon size={16} className="ui-icon" aria-hidden="true" />
      <div>
        <p className="a2-banner-title">{b.title}{extra ? <span className="a2-banner-extra"> {extra}</span> : null}</p>
        <p>{b.text}</p>
      </div>
    </div>
  )
}
