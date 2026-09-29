// Risk assessment body: severity counts, then one item per medium/high unit with the names that
// carried the rating, its files as chips, and the phase that migrates it. `details` is `useRiskDetails().byId`.
import { CircleAlert, Info, TriangleAlert } from 'lucide-react'
import { riskDetail, riskItems, severityCounts } from './riskModel.js'
import { Unverified } from './ExecutiveSummary.jsx'
import './agent2.css'

const SEVERITY = {
  high: { label: 'High', tone: 'danger', Icon: TriangleAlert },
  medium: { label: 'Medium', tone: 'warning', Icon: CircleAlert },
  low: { label: 'Low', tone: 'success', Icon: Info },
}
const Pill = ({ level }) => {
  const s = SEVERITY[level]
  return <span className="ui-pill" data-tone={s.tone}><s.Icon size={14} className="ui-icon" aria-hidden="true" />{s.label}</span>
}

export default function RiskAssessment({ plan, details = {}, detailedIds = [], onSelectUnit }) {
  const items = riskItems(plan)
  const counts = severityCounts(plan)
  const detailed = new Set(detailedIds)
  return (
    <>
      <ul className="a2-sev-summary" aria-label="Units by risk level">
        {['high', 'medium', 'low'].map((k) => <li key={k}><Pill level={k} /><span className="a2-num-cell">{counts[k]}</span></li>)}
      </ul>
      {items.length === 0 ? <p className="a2-note">No unit is rated medium or high risk.</p> : (
        <ul className="a2-risk-list">
          {items.map((item) => {
            const entry = details[item.clusterId]
            const d = entry?.detail ? riskDetail(entry.detail) : null
            return (
              <li key={item.clusterId} className="a2-risk" data-severity={item.level}>
                <div className="a2-risk-head">
                  <Pill level={item.level} />
                  <button type="button" className="a2-link-btn" onClick={() => onSelectUnit?.(item.clusterId)}>{item.label}</button>
                  {item.unverified ? <Unverified /> : null}
                </div>
                <p className="a2-note">Migrated in {item.phaseLabel}, unit {item.phaseOrder}.{item.unitOrder}.</p>
                {d?.factors.length ? <p>Rated {SEVERITY[item.level].label.toLowerCase()} because of: {d.factors.join(', ')}.</p> : null}
                {d?.files.length ? (
                  <p className="a2-chips" aria-label="Affected files">
                    {d.visibleFiles.map((f) => <code key={f} className="a2-chip">{f}</code>)}
                    {d.hiddenFiles ? <span className="a2-note">+{d.hiddenFiles} more</span> : null}
                  </p>
                ) : null}
                {entry?.error ? <p className="a2-note">Details could not be loaded ({entry.error}).</p> : null}
                {!entry && detailed.has(item.clusterId) ? <p className="a2-note">Loading details.</p> : null}
                {!detailed.has(item.clusterId) ? <p className="a2-note">Open the unit for its factors and files.</p> : null}
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}
