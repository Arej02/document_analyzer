// Dependency inventory body, from GET /plans/{id}/dependencies (design_agent2.md §11). `rows` is null while
// loading or when the request failed, and an analysis run before packages were recorded returns an empty list
// although the manifests declare some: in both cases only the declared-package count from the job's stats is
// shown, never a made-up table. The target column only appears when some row has a proposed target. Sortable (`aria-sort`) and filterable, status is icon + text.
import { Fragment, useMemo, useState } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown, CircleAlert, CircleCheck, CircleDashed } from 'lucide-react'
import { STATUS_KEYS, ariaSort, filterRows, nextSort, sortRows, statusOf, visibleColumns } from './dependencyModel.js'
import { formatInt } from '../ui/format.js'
import './agent2.css'

const HEADINGS = { name: 'Package', ecosystem: 'Ecosystem', version: 'Declared version', manifest: 'Manifest', target: 'Target equivalent', status: 'Status' }
const STATUS_ICON = { proposed: CircleCheck, unverified: CircleAlert, none: CircleDashed }
const STATUS_TEXT = { proposed: 'Target proposed', unverified: 'Target unverified', none: 'No target proposed' }

export default function DependencyInventory({ rows, declaredCount }) {
  const [filter, setFilter] = useState({ ecosystem: 'all', status: 'all' })
  const [sort, setSort] = useState({ key: 'name', dir: 'asc' })
  const list = rows ?? []
  const columns = useMemo(() => visibleColumns(list), [list])
  const ecosystems = useMemo(() => [...new Set(list.map((r) => r.ecosystem).filter(Boolean))].sort(), [list])
  const shown = useMemo(() => sortRows(filterRows(list, filter), sort), [list, filter, sort])

  const notRecorded = rows != null && rows.length === 0 && declaredCount > 0
  if (rows == null || notRecorded) {
    return (
      <p className="a2-note">
        {declaredCount != null
          ? `${formatInt(declaredCount)} ${declaredCount === 1 ? 'package is' : 'packages are'} declared in the repository's manifests. The package list and target equivalents are not available for this analysis yet.`
          : 'The package list is not available for this analysis yet.'}
      </p>
    )
  }
  if (!list.length) return <p className="a2-note">No packages are declared in this repository.</p>

  const showStatus = columns.includes('status')
  const cell = {
    name: (r) => <th scope="row"><code>{r.name}</code>{r.dev ? <span className="a2-sub-path">dev</span> : null}</th>,
    ecosystem: (r) => <td>{r.ecosystem ?? ''}</td>,
    version: (r) => <td className="a2-mono">{r.version_spec ?? ''}</td>,
    manifest: (r) => <td><code>{r.manifest_path ?? ''}</code></td>,
    target: (r) => <td title={r.rationale || undefined}>{r.target_name ?? ''}</td>,
    status: (r) => {
      const s = statusOf(r)
      const Icon = STATUS_ICON[s.key]
      return <td><span className="ui-pill" data-tone={s.tone}><Icon size={14} className="ui-icon" aria-hidden="true" />{s.label}</span></td>
    },
  }
  const heading = (key) => {
    const Icon = sort.key !== key ? ArrowUpDown : sort.dir === 'asc' ? ArrowUp : ArrowDown
    return (
      <th key={key} scope="col" aria-sort={ariaSort(sort, key)}>
        <button type="button" className="a2-th-sort" onClick={() => setSort((s) => nextSort(s, key))}>{HEADINGS[key]}<Icon size={13} className="ui-icon" aria-hidden="true" /></button>
      </th>
    )
  }

  return (
    <>
      <p className="a2-note" aria-live="polite">Showing {formatInt(shown.length)} of {formatInt(list.length)} packages.</p>
      <div className="a2-filters">
        {ecosystems.length > 1 ? (
          <div role="group" aria-label="Filter by ecosystem" className="a2-filter-group">
            <button type="button" className="a2-chip-btn" aria-pressed={filter.ecosystem === 'all'} onClick={() => setFilter((f) => ({ ...f, ecosystem: 'all' }))}>All ecosystems</button>
            {ecosystems.map((e) => <button key={e} type="button" className="a2-chip-btn" aria-pressed={filter.ecosystem === e} onClick={() => setFilter((f) => ({ ...f, ecosystem: f.ecosystem === e ? 'all' : e }))}>{e}</button>)}
          </div>
        ) : null}
        {showStatus ? (
          <div role="group" aria-label="Filter by status" className="a2-filter-group">
            <button type="button" className="a2-chip-btn" aria-pressed={filter.status === 'all'} onClick={() => setFilter((f) => ({ ...f, status: 'all' }))}>All statuses</button>
            {STATUS_KEYS.map((k) => <button key={k} type="button" className="a2-chip-btn" aria-pressed={filter.status === k} onClick={() => setFilter((f) => ({ ...f, status: f.status === k ? 'all' : k }))}>{STATUS_TEXT[k]}</button>)}
          </div>
        ) : null}
      </div>
      <div className="a2-table-wrap" role="region" aria-label="Dependency inventory" tabIndex={0}>
        <table className="a2-table">
          <thead><tr>{columns.map(heading)}</tr></thead>
          <tbody>
            {shown.map((r) => <tr key={`${r.ecosystem}:${r.manifest_path}:${r.name}`}>{columns.map((c) => <Fragment key={c}>{cell[c](r)}</Fragment>)}</tr>)}
            {shown.length === 0 ? <tr><td colSpan={columns.length} className="a2-note">No packages match these filters.</td></tr> : null}
          </tbody>
        </table>
      </div>
    </>
  )
}
