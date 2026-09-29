// Real data for the Executive summary and Repository overview sections (tasks_frontend.md A2.3,
// decisions in design_agent2.md §16). Pure: the caller fetches, this shapes.
//
//   plan     GET /api/agent2/plans/{id}
//   summary  GET /api/agent1/jobs/{source_job_id}/summary  (repo_summary, risk_level, entity_counts, unverified_count)
//   stats    the merged `stats.updated` payloads of that job (worker/discovery.py): loc, language.<Name>,
//            framework.<Name>, dependencies
//   routes   GET /api/agent1/jobs/{source_job_id}/entities?entity_type=route
//
// Nothing here is invented. There is no per-directory breakdown, no target stack and no effort in the
// data, so the overview has no Structure table and no Target column.

const numberOr = (v, fallback = null) => (Number.isFinite(v) ? v : fallback)

/** Executive summary: the repo-level summary first, then one paragraph per phase from its cluster report. */
export function buildExecutiveSummary(plan, repoSummary) {
  const paragraphs = []
  if (repoSummary?.repo_summary) {
    paragraphs.push({ key: 'repo', label: null, text: repoSummary.repo_summary, unverified: false })
  }
  for (const phase of plan?.phases ?? []) {
    if (!phase.summary_text) continue
    paragraphs.push({ key: `phase-${phase.phase_cluster_id}`, label: phase.label, text: phase.summary_text, unverified: phase.grounding_status === 'unverified' })
  }
  const phases = plan?.phases ?? []
  return {
    paragraphs,
    unverifiedRepo: numberOr(repoSummary?.unverified_count, 0),
    highlights: [
      { label: 'Phases', value: phases.length },
      { label: 'Migration units', value: phases.reduce((n, p) => n + (p.units?.length ?? 0), 0) },
      ...(repoSummary?.risk_level ? [{ label: 'Repository risk', value: repoSummary.risk_level }] : []),
    ],
  }
}

function namesUnder(stats, prefix) {
  return Object.entries(stats ?? {})
    .filter(([key, value]) => key.startsWith(prefix) && Number.isFinite(value))
    .map(([key, value]) => ({ name: key.slice(prefix.length), count: value }))
}

/** Repository overview: stack (languages, frameworks), size, entry points. Missing inputs stay null/empty. */
export function buildRepoOverview({ stats, summary, routes } = {}) {
  const languages = namesUnder(stats, 'language.').sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
  const sourceFiles = languages.reduce((n, l) => n + l.count, 0)
  const entityCounts = summary?.entity_counts ?? null
  return {
    languages: languages.map((l) => ({ ...l, pct: sourceFiles ? (l.count / sourceFiles) * 100 : 0 })),
    frameworks: namesUnder(stats, 'framework.').map((f) => f.name).sort(),
    size: {
      loc: numberOr(stats?.loc),
      sourceFiles: languages.length ? sourceFiles : null,
      dependencies: numberOr(stats?.dependencies),
      entities: entityCounts ? Object.values(entityCounts).reduce((n, c) => n + (Number.isFinite(c) ? c : 0), 0) : null,
    },
    entityCounts,
    entryPoints: (routes ?? []).map((r) => ({ id: r.id, name: r.name, filePath: r.file_path })),
  }
}
