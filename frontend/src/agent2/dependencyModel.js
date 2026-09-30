// Dependency inventory model (tasks_frontend.md A2.3, decisions in design_agent2.md §16).
//
// Rows are the planned `GET /plans/{id}/dependencies` shape, which does not exist yet (tasks_agent2.md
// Phase 7): `{name, ecosystem, version_spec, manifest_path, dev, target_name?, rationale?, grounding_status?}`.
// Facts (name, ecosystem, declared version, manifest) come from Agent 1. The target is an inference, so a
// column only appears when some row has one, and a target that is not grounded is labelled unverified.

export const COLUMNS = ['name', 'ecosystem', 'version', 'manifest', 'target', 'status']

const has = (v) => v != null && v !== ''

/** Columns with at least one value; `name` always shows. `status` needs a target to mean anything. */
export function visibleColumns(rows) {
  const some = (pick) => rows.some((r) => has(pick(r)))
  const cols = new Set(['name'])
  if (some((r) => r.ecosystem)) cols.add('ecosystem')
  if (some((r) => r.version_spec)) cols.add('version')
  if (some((r) => r.manifest_path)) cols.add('manifest')
  if (some((r) => r.target_name)) { cols.add('target'); cols.add('status') }
  return COLUMNS.filter((c) => cols.has(c))
}

/** Icon + text status, never colour alone. Only meaningful for a row that has a target. */
export function statusOf(row) {
  if (!has(row.target_name)) return { key: 'none', label: 'No target proposed', tone: 'neutral' }
  if (row.grounding_status === 'unverified') return { key: 'unverified', label: 'Target unverified', tone: 'warning' }
  return { key: 'proposed', label: 'Target proposed', tone: 'success' }
}

export const STATUS_KEYS = ['proposed', 'unverified', 'none']

export function filterRows(rows, { ecosystem = 'all', status = 'all' } = {}) {
  return rows.filter((r) => (ecosystem === 'all' || r.ecosystem === ecosystem) && (status === 'all' || statusOf(r).key === status))
}

const SORT_VALUE = {
  name: (r) => r.name ?? '',
  ecosystem: (r) => r.ecosystem ?? '',
  version: (r) => r.version_spec ?? '',
  manifest: (r) => r.manifest_path ?? '',
  target: (r) => r.target_name ?? '',
  status: (r) => STATUS_KEYS.indexOf(statusOf(r).key),
}

/** Stable sort; ties break on name so the order never jumps between renders. */
export function sortRows(rows, { key = 'name', dir = 'asc' } = {}) {
  const value = SORT_VALUE[key] ?? SORT_VALUE.name
  const sign = dir === 'desc' ? -1 : 1
  return [...rows].sort((a, b) => {
    const [x, y] = [value(a), value(b)]
    const order = typeof x === 'number' ? x - y : String(x).localeCompare(String(y), 'en', { numeric: true })
    return order * sign || String(a.name ?? '').localeCompare(String(b.name ?? ''), 'en')
  })
}

export const ariaSort = (sort, key) => (sort.key !== key ? 'none' : sort.dir === 'asc' ? 'ascending' : 'descending')
export const nextSort = (sort, key) => ({ key, dir: sort.key === key && sort.dir === 'asc' ? 'desc' : 'asc' })
