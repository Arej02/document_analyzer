// Turns what a running Agent 1 job already exposes into StatChips values. A chip with no
// data source yet is left out (so it renders "Pending") rather than faked.
//
//   stats  buildRunView().stats: the merged `stats.updated` payloads (worker/run.py).
//   job    GET /api/agent1/jobs/{id}: changed/unchanged/removed_file_count once diffed.
//
// Returns { [chipKey]: { value: string, sub?: string } }.

import { formatInt } from '../ui/format.js'

const count = (...values) => values.find((v) => Number.isFinite(v))

// `language.PHP: 12` -> { name: 'PHP', count: 12 }
function namesUnder(stats, prefix) {
  return Object.entries(stats)
    .filter(([key, value]) => key.startsWith(prefix) && Number.isFinite(value))
    .map(([key, value]) => ({ name: key.slice(prefix.length), count: value }))
}

export function buildStatChips(stats = {}, job = null) {
  const chips = {}
  const changed = count(stats.changed_files, job?.changed_file_count)
  const unchanged = count(stats.unchanged_files, job?.unchanged_file_count)
  const removed = count(stats.removed_files, job?.removed_file_count) ?? 0
  if (changed != null && unchanged != null) {
    const parts = [`${formatInt(changed)} changed`, `${formatInt(unchanged)} unchanged`]
    if (removed > 0) parts.push(`${formatInt(removed)} removed`)
    chips.files = { value: formatInt(changed + unchanged), sub: parts.join(' · ') }
  }

  // Discovered stats (worker/discovery.py) arrive together once, so `loc` says discovery has run.
  if (Number.isFinite(stats.loc)) {
    const languages = namesUnder(stats, 'language.').sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
    const sourceFiles = languages.reduce((sum, l) => sum + l.count, 0)
    chips.loc = { value: formatInt(stats.loc), sub: sourceFiles ? `across ${formatInt(sourceFiles)} source ${sourceFiles === 1 ? 'file' : 'files'}` : undefined }
    chips.languages = languages.length
      ? { value: formatInt(languages.length), sub: languages.map((l) => `${l.name} ${formatInt(l.count)}`).join(' · ') }
      : { value: '0', sub: 'No source code found' }
    const frameworks = namesUnder(stats, 'framework.').map((f) => f.name).sort()
    chips.frameworks = frameworks.length ? { value: formatInt(frameworks.length), sub: frameworks.join(' · ') } : { value: '0', sub: 'None recognised' }
  }
  if (Number.isFinite(stats.dependencies)) chips.dependencies = { value: formatInt(stats.dependencies), sub: 'declared in manifests' }
  return chips
}
