// Executive summary body: repo summary, then each phase's cluster report. Unverified text is marked.
import { CircleAlert, CircleCheck } from 'lucide-react'
import { formatInt } from '../ui/format.js'
import './agent2.css'

export function Unverified() {
  return <span className="ui-pill" data-tone="warning"><CircleAlert size={14} className="ui-icon" aria-hidden="true" />Unverified, review recommended</span>
}

export default function ExecutiveSummary({ model }) {
  if (!model.paragraphs.length) return <p className="a2-note">No summary text is available for this plan yet.</p>
  return (
    <>
      {model.paragraphs.map((p) => (
        <div key={p.key} className="a2-para">
          {p.label ? <h3 className="a2-h3">{p.label}</h3> : null}
          <p>{p.text}</p>
          {p.unverified ? <p><Unverified /></p> : null}
        </div>
      ))}
      <p className="a2-note">
        {model.unverifiedRepo > 0
          ? <><CircleAlert size={14} className="ui-icon" aria-hidden="true" /> {model.unverifiedRepo} repository {model.unverifiedRepo === 1 ? 'summary is' : 'summaries are'} unverified.</>
          : <><CircleCheck size={14} className="ui-icon" aria-hidden="true" /> All repository summaries verified.</>}
      </p>
      <dl className="a2-metrics">
        {model.highlights.map((h) => (
          <div key={h.label}><dt>{h.label}</dt><dd>{typeof h.value === 'number' ? formatInt(h.value) : h.value}</dd></div>
        ))}
      </dl>
    </>
  )
}
