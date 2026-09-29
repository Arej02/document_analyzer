// Data layer body. Titled for what the data is (post types and meta keys, design_agent2.md §16):
// no row counts and no "becomes" column, because Agent 1 has neither. `state` is `useSchemaEntities`'s.
import { CircleAlert, Info, TriangleAlert } from 'lucide-react'
import { PAGE_LIMIT, buildSchemaRows, schemaCounts } from './dataLayerModel.js'
import { formatInt } from '../ui/format.js'
import { Unverified } from './ExecutiveSummary.jsx'
import './agent2.css'

const RISK = {
  high: { label: 'High', tone: 'danger', Icon: TriangleAlert },
  medium: { label: 'Medium', tone: 'warning', Icon: CircleAlert },
  low: { label: 'Low', tone: 'success', Icon: Info },
}

export default function DataLayer({ state }) {
  if (state.error) return <p className="a2-note">The schema list could not be loaded ({state.error}).</p>
  if (!state.data) return <p className="a2-note">Loading the schema list.</p>
  const { rows, total, truncated } = buildSchemaRows(state.data.entities, state.data.total_count)
  if (!rows.length) return <p className="a2-note">No post types or meta keys were found in this repository.</p>
  const counts = schemaCounts(rows)
  return (
    <>
      <p>
        {formatInt(total)} {total === 1 ? 'definition' : 'definitions'} of WordPress post types and meta keys.
        This is what the code registers, not the database: table names and row counts are not part of the analysis.
      </p>
      {counts.dynamic ? <p className="a2-note">{formatInt(counts.dynamic)} {counts.dynamic === 1 ? 'name is' : 'names are'} built at run time, so the extractor could not resolve {counts.dynamic === 1 ? 'it' : 'them'}.</p> : null}
      <div className="a2-table-wrap" role="region" aria-label="Post types and meta keys" tabIndex={0}>
        <table className="a2-table">
          <thead><tr><th scope="col">Name</th><th scope="col">File</th><th scope="col">Risk</th><th scope="col">Notes</th></tr></thead>
          <tbody>
            {rows.map((r) => {
              const risk = RISK[r.riskLevel]
              return (
                <tr key={r.id}>
                  <th scope="row"><code>{r.name}</code></th>
                  <td><code>{r.filePath}</code></td>
                  <td>{risk ? <span className="ui-pill" data-tone={risk.tone}><risk.Icon size={14} className="ui-icon" aria-hidden="true" />{risk.label}</span> : 'n/a'}</td>
                  <td>{r.note ?? <span className="a2-note">No summary</span>}{r.unverified ? <> <Unverified /></> : null}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {truncated ? <p className="a2-note">Showing the first {formatInt(PAGE_LIMIT)} of {formatInt(total)}.</p> : null}
    </>
  )
}
