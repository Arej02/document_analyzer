// Data layer section (tasks_frontend.md A2.3, decisions in design_agent2.md §16).
//
// Source: `GET /api/agent1/jobs/{id}/entities?entity_type=schema`. Those entities are WordPress post types
// and meta keys (`wordpress_extractor.py`), not database tables, and nothing reads a database, so there are
// NO row counts and NO "becomes" mapping. The list response carries name, file, summary and risk but not
// `raw_facts.kind`, so post types and meta keys are not told apart here either.

export const PAGE_LIMIT = 200

const RISK_ORDER = { high: 0, medium: 1, low: 2 }

/** Rows for the list, plus how many the API says exist so the caller can say when the list is cut short. */
export function buildSchemaRows(entities, totalCount) {
  const rows = (entities ?? []).map((e) => ({
    id: e.id,
    name: e.name,
    filePath: e.file_path,
    note: e.summary_text || null,
    riskLevel: e.risk_level ?? null,
    unverified: e.grounding_status === 'unverified',
    dynamic: typeof e.name === 'string' && e.name.startsWith('(dynamic '), // the extractor could not resolve the name
  }))
  rows.sort((a, b) => (RISK_ORDER[a.riskLevel] ?? 3) - (RISK_ORDER[b.riskLevel] ?? 3) || String(a.name).localeCompare(String(b.name), 'en'))
  const total = Number.isFinite(totalCount) ? totalCount : rows.length
  return { rows, total, truncated: total > rows.length }
}

export function schemaCounts(rows) {
  return {
    total: rows.length,
    dynamic: rows.filter((r) => r.dynamic).length,
    unverified: rows.filter((r) => r.unverified).length,
  }
}
