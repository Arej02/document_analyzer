import { useEffect, useState } from 'react'
import { useApiFetch } from '../AuthProvider'
import { buildCostView } from './costModel.js'

const REFRESH_MS = 15000

/**
 * Operator-only LLM cost (design.md §7.4): a total chip plus a breakdown table. Cost is internal, so
 * the cost endpoints answer 404 to everyone who isn't a configured operator. This component then
 * renders nothing at all: no error, no empty box, no hint that the feature exists.
 *
 * `endpoint` is the run's cost endpoint. `qaEndpoint` (Agent 1 only) adds the spend on questions
 * asked against the job. `groupBy` is 'stage' (Agents 1, 2) or 'unit' (Agent 3). `active` keeps the
 * numbers fresh while the run is going, and `refreshKey` refetches when the run changes state.
 * A list-price estimate, not the Azure invoice.
 */
export function CostPanel({ endpoint, qaEndpoint, groupBy = 'stage', active = false, refreshKey }) {
  const apiFetch = useApiFetch()
  const [view, setView] = useState(null)
  const [qaView, setQaView] = useState(null)

  useEffect(() => {
    let cancelled = false
    let timeoutId

    const load = async (path, setter) => {
      const response = await apiFetch(path)
      if (response.status === 404) return 'hidden'
      if (!response.ok) return 'failed'
      if (!cancelled) setter(buildCostView(await response.json()))
      return 'ok'
    }

    const poll = async () => {
      let outcome = 'failed'
      try {
        outcome = await load(endpoint, setView)
        if (outcome === 'ok' && qaEndpoint) await load(qaEndpoint, setQaView)
      } catch {
        // Cost is secondary: a failed fetch just leaves the last numbers (or nothing) on screen.
      }
      if (cancelled) return
      if (outcome === 'hidden') {
        setView(null)
        return // not an operator: don't keep asking
      }
      if (active) timeoutId = setTimeout(poll, REFRESH_MS)
    }

    poll()
    return () => { cancelled = true; clearTimeout(timeoutId) }
  }, [endpoint, qaEndpoint, active, refreshKey])

  if (!view) return null

  const rows = groupBy === 'unit' ? view.unitRows : view.stageRows
  const showUnit = groupBy === 'unit'

  return (
    <section className="cost-panel" aria-label="LLM cost (operators only)">
      <div className="cost-head">
        <h2 className="cost-title">LLM cost</h2>
        <span className="cost-chip tnum" data-unknown={view.unknown || undefined} title="List-price estimate, not the Azure invoice">
          {view.chip}
        </span>
        <span className="cost-meta tnum">
          {view.total.calls} calls · {view.total.tokens} tokens
          {view.total.errors ? ` · ${view.total.errors} failed` : ''}
          {view.total.note ? ` · ${view.total.note}` : ''}
        </span>
        {view.cache ? (
          <span className="cost-meta tnum">summary cache {view.cache.hits} hit / {view.cache.misses} miss</span>
        ) : null}
        {qaView && qaView.total.calls !== '0' ? (
          <span className="cost-meta tnum">Q&amp;A {qaView.chip} ({qaView.total.calls} calls)</span>
        ) : null}
      </div>
      {rows.length > 0 ? (
        <table className="cost-table">
          <thead>
            <tr>
              <th scope="col">{showUnit ? 'Unit' : 'Stage'}</th>
              {showUnit ? <th scope="col">Rounds</th> : null}
              {showUnit ? <th scope="col">Attempts</th> : null}
              <th scope="col">Calls</th>
              <th scope="col">Tokens</th>
              <th scope="col">Cost</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key}>
                <th scope="row">{r.label}</th>
                {showUnit ? <td className="tnum">{r.rounds}</td> : null}
                {showUnit ? <td className="tnum">{r.attempts}</td> : null}
                <td className="tnum">{r.calls}{r.errors ? ` (${r.errors} failed)` : ''}</td>
                <td className="tnum">{r.tokens}</td>
                <td className="tnum">{r.cost}{r.note ? ` · ${r.note}` : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </section>
  )
}
