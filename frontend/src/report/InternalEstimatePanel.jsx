// Operator-only internal estimate (tasks_report.md Phase 7 frontend pair; design_report.md §9.5). Our own AI
// cost range for generating the code, never part of the report document or the PDF, so it lives outside the
// iframe. Everyone who isn't a configured operator gets a 404, and then this renders nothing at all: no
// error, no empty box, no hint that it exists (same as CostPanel).

import { useEffect, useState } from 'react'
import { useApiFetch } from '../AuthProvider'
import { internalEstimateView } from './internalEstimateModel.js'

export default function InternalEstimatePanel({ reportId }) {
  const apiFetch = useApiFetch()
  const [view, setView] = useState(null)

  useEffect(() => {
    let cancelled = false
    setView(null)
    apiFetch(`/api/reports/${reportId}/internal-estimate`)
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => { if (!cancelled) setView(internalEstimateView(data)) })
      .catch(() => {}) // secondary information: a failure just leaves nothing on screen
    return () => { cancelled = true }
    // apiFetch is a new function every render; the report id is the real dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reportId])

  if (!view) return null

  return (
    <section className="cost-panel" aria-label="Internal estimate (operators only)">
      <div className="cost-head">
        <h2 className="cost-title">Internal estimate</h2>
        <span className="cost-chip tnum" data-unknown={view.hasCost ? undefined : true} title="List-price estimate of our AI spend, not the customer's quote">
          {view.cost}
        </span>
        <span className="cost-meta tnum">{view.n}</span>
        <span className="cost-meta">{view.priceBasis}</span>
        {view.unpriced && <span className="cost-meta tnum">{view.unpriced}</span>}
        {view.lowConfidence && <span className="cost-meta">{view.lowConfidence}</span>}
      </div>
      {view.buckets.length > 0 && (
        <table className="cost-table">
          <thead>
            <tr><th scope="col">Bucket</th><th scope="col">Units</th><th scope="col">n</th><th scope="col">Source</th><th scope="col">Median cost</th></tr>
          </thead>
          <tbody>
            {view.buckets.map((b) => (
              <tr key={b.key}>
                <th scope="row">{b.label}</th>
                <td className="tnum">{b.units}</td>
                <td className="tnum">{b.n}</td>
                <td>{b.source}</td>
                <td className="tnum">{b.median}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
