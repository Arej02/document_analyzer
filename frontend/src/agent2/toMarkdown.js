// "Copy as Markdown" for the plan document (tasks_frontend.md A2.4). Pure: the caller passes what the
// sections already loaded, this shapes it with the same models the sections use, so the export and the
// screen cannot disagree. Sections come out in outline order. An empty section keeps its heading and
// says why, so the document's shape never changes. Approval state and saved phase notes are included
// exactly as stored (design_agent2.md §16 point 6).
//
// Everything that came from the repository or an LLM (names, paths, summaries, notes) is untrusted text:
// it is escaped so it cannot add table columns, headings, list items, links or HTML to the export.
import { buildExecutiveSummary, buildRepoOverview } from './overviewModel.js'
import { buildModuleRows, couplingHotspots } from './architectureModel.js'
import { statusOf, visibleColumns } from './dependencyModel.js'
import { buildSchemaRows } from './dataLayerModel.js'
import { riskDetail, riskItems, severityCounts } from './riskModel.js'
import { phaseCards } from './migrationModel.js'
import { openQuestions } from './openQuestionsModel.js'
import { provenance } from './provenance.js'

const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s)
const num = (n) => (n == null ? 'n/a' : Math.round(n).toLocaleString('en-US'))

/** One line of prose or a table cell: single line, no HTML, no markdown punctuation that changes structure. */
export function inline(value) {
  return String(value ?? '')
    .replace(/\s*[\r\n]+\s*/g, ' ')
    .trim()
    .replace(/[\\`*_[\]<>|]/g, (c) => (c === '<' ? '&lt;' : c === '>' ? '&gt;' : `\\${c}`))
    .replace(/^([#+-]|\d+[.)])/, '\\$1')
}

/** A path or name as inline code. A backtick inside uses a longer fence, and a pipe stays out of the table. */
export function code(value) {
  const text = String(value ?? '').replace(/\s*[\r\n]+\s*/g, ' ').trim().replace(/\|/g, '&#124;')
  const longest = Math.max(0, ...(text.match(/`+/g) ?? []).map((run) => run.length))
  const fence = '`'.repeat(longest + 1)
  return longest ? `${fence} ${text} ${fence}` : `${fence}${text}${fence}`
}

const row = (cells) => `| ${cells.join(' | ')} |`
const table = (head, rows) => [row(head), row(head.map(() => '---')), ...rows.map(row)].join('\n')
const quote = (text) => String(text).trim().split(/\r?\n/).map((l) => `> ${inline(l) || ' '}`.trimEnd()).join('\n')
const none = (text) => `_${text}_`

function executive(d) {
  const model = buildExecutiveSummary(d.plan, d.repoSummary)
  if (!model.paragraphs.length) return [none('No summary text is available for this plan yet.')]
  const out = model.paragraphs.flatMap((p) => [`${p.label ? `**${inline(p.label)}.** ` : ''}${inline(p.text)}${p.unverified ? ' _(unverified)_' : ''}`, ''])
  out.push(model.highlights.map((h) => `${h.label}: ${typeof h.value === 'number' ? num(h.value) : inline(h.value)}`).join('. ') + '.')
  return out
}

function overview(d) {
  const o = buildRepoOverview({ stats: d.stats, summary: d.repoSummary, routes: d.routes })
  const out = ['### Stack', '']
  out.push(o.frameworks.length ? `Frameworks: ${o.frameworks.map(inline).join(', ')}.` : none('No framework recognised.'), '')
  if (o.languages.length) out.push(table(['Language', 'Files', 'Share'], o.languages.map((l) => [inline(l.name), num(l.count), `${l.pct.toFixed(1)}%`])), '')
  else out.push(none('No source languages were recorded.'), '')
  out.push('### Size', '', table(['Measure', 'Value'], [
    ['Lines of code', num(o.size.loc)], ['Source files', num(o.size.sourceFiles)],
    ['Declared packages', num(o.size.dependencies)], ['Extracted entities', num(o.size.entities)],
  ]), '', '### Entry points', '')
  out.push(o.entryPoints.length ? table(['Route', 'File'], o.entryPoints.map((e) => [inline(e.name), code(e.filePath)])) : none('No routes were extracted.'))
  return out
}

function architecture(d) {
  const rows = buildModuleRows(d.plan, d.unitGraph)
  if (!rows.length) return [none('This plan has no migration units.')]
  const out = ['### Module map', '', table(['Unit', 'Phase', 'Entities', 'Risk', 'Depended on by', 'Depends on'],
    rows.map((r) => [`${r.phaseOrder}.${r.unitOrder} ${inline(r.label)}${r.needsFacade ? ' (needs facade)' : ''}`, inline(r.phaseLabel), num(r.size), r.riskLevel ? cap(r.riskLevel) : 'n/a', num(r.fanIn), num(r.fanOut)])), '']
  const hot = couplingHotspots(rows)
  out.push('### Most depended-on units', '')
  out.push(...(hot.length ? hot.map((r) => `- ${inline(r.label)}: ${num(r.fanIn)} ${r.fanIn === 1 ? 'unit depends' : 'units depend'} on it.`) : [none('No unit is depended on by another.')]))
  return out
}

const DEP_HEAD = { name: 'Package', ecosystem: 'Ecosystem', version: 'Declared version', manifest: 'Manifest', target: 'Target equivalent', status: 'Status' }
function dependencies(d) {
  if (d.dependencies == null) {
    const n = d.stats?.dependencies
    return [none(Number.isFinite(n) ? `${num(n)} packages are declared in the manifests. The package list is not available for this analysis yet.` : 'The package list is not available for this analysis yet.')]
  }
  if (!d.dependencies.length) return [none('No packages are declared in this repository.')]
  const cols = visibleColumns(d.dependencies)
  const cell = {
    name: (r) => code(r.name), ecosystem: (r) => inline(r.ecosystem), version: (r) => inline(r.version_spec), manifest: (r) => (r.manifest_path ? code(r.manifest_path) : ''),
    target: (r) => inline(r.target_name), status: (r) => statusOf(r).label,
  }
  return [table(cols.map((c) => DEP_HEAD[c]), d.dependencies.map((r) => cols.map((c) => cell[c](r))))]
}

function dataLayer(d) {
  const { rows, total, truncated } = buildSchemaRows(d.schema?.entities, d.schema?.total_count)
  if (!rows.length) return [none(d.schema ? 'No post types or meta keys were found.' : 'The schema list is not available.')]
  const out = [`${num(total)} WordPress post type and meta key definitions (what the code registers, not database tables; no row counts).`, '',
    table(['Name', 'File', 'Risk', 'Notes'], rows.map((r) => [code(r.name), code(r.filePath), r.riskLevel ? cap(r.riskLevel) : 'n/a', `${inline(r.note ?? '')}${r.unverified ? ' _(unverified)_' : ''}`.trim()]))]
  if (truncated) out.push('', none(`Showing ${num(rows.length)} of ${num(total)}.`))
  return out
}

function risks(d) {
  const c = severityCounts(d.plan)
  const items = riskItems(d.plan)
  const out = [`High: ${c.high}. Medium: ${c.medium}. Low: ${c.low}.`, '']
  if (!items.length) return [...out, none('No unit is rated medium or high risk.')]
  for (const item of items) {
    const entry = d.riskDetails?.[item.clusterId]
    const det = entry?.detail ? riskDetail(entry.detail) : null
    const parts = [`- **${cap(item.level)}: ${inline(item.label)}.** Migrated in ${inline(item.phaseLabel)}, unit ${item.phaseOrder}.${item.unitOrder}.${item.unverified ? ' _(unverified)_' : ''}`]
    if (det?.factors.length) parts.push(`  Because of: ${det.factors.map(inline).join(', ')}.`)
    if (det?.files.length) parts.push(`  Files: ${det.visibleFiles.map(code).join(', ')}${det.hiddenFiles ? `, +${det.hiddenFiles} more` : ''}.`)
    out.push(parts.join('\n'))
  }
  return out
}

function migration(d) {
  const cards = phaseCards(d.plan, d.phaseGraph)
  if (!cards.length) return [none('This plan has no phases.')]
  const APPROVAL = { approved: 'Approved', rejected: 'Rejected', pending: 'Not reviewed' }
  const out = []
  for (const c of cards) {
    out.push(`### Phase ${c.order}: ${inline(c.label)}`, '')
    out.push(`Approval: ${APPROVAL[c.approval.key]}.${c.riskLevel ? ` Risk: ${cap(c.riskLevel)}.` : ''}${c.unverified ? ' Report unverified.' : ''}`, '')
    out.push(c.summary ? inline(c.summary) : none('No report is available for this phase.'), '')
    if (c.dependsOn) out.push(`Depends on: ${c.dependsOn.length ? c.dependsOn.map((p) => `${p.order}. ${inline(p.label)}`).join(', ') : 'nothing'}.`, '')
    out.push('**Units**', '')
    out.push(...(c.units.length ? c.units.map((u) => `- ${c.order}.${u.unit_order} ${inline(u.label || `Unit ${u.unit_order}`)} (${num(u.size)} entities${u.risk_level ? `, ${u.risk_level} risk` : ''}${u.flags?.includes('needs_facade') ? ', needs facade' : ''}${u.grounding_status === 'unverified' ? ', unverified' : ''})`) : [none('No units.')]))
    if (c.approval.note) out.push('', '**Reviewer note**', '', quote(c.approval.note))
    out.push('')
  }
  return out
}

function questions(d) {
  const { cards, openFlags, unlisted } = openQuestions(d.plan)
  if (!cards.length && !openFlags) return [none('No open decisions.')]
  const out = [`${openFlags} open facade ${openFlags === 1 ? 'flag' : 'flags'}. Approval cannot complete until each is acknowledged or overridden.`, '']
  out.push(...cards.map((c) => `- **Open:** ${c.level === 'unit' ? 'units' : 'phases'} in a dependency cycle: ${c.members.map((m) => inline(m.label)).join(' ↔ ')}.`))
  if (unlisted) out.push('', none(`${unlisted} more open ${unlisted === 1 ? 'flag is' : 'flags are'} not listed as a cycle.`))
  return out
}

const SECTIONS = [
  ['Executive summary', executive], ['Repository overview', overview], ['Architecture and module map', architecture],
  ['Dependency inventory', dependencies], ['Data layer and schema', dataLayer], ['Risk assessment', risks],
  ['Migration plan', migration], ['Open questions and decisions', questions],
]
export const SECTION_TITLES = SECTIONS.map(([title]) => title)

/** `d`: {plan, repoSummary, stats, routes, unitGraph, phaseGraph, dependencies, schema, riskDetails, commitSha, generatedAt, repoName}. */
export function toMarkdown(d = {}) {
  const plan = d.plan ?? {}
  const phases = plan.phases ?? []
  const approved = phases.filter((p) => p.approval?.status === 'approved').length
  const prov = provenance({ commitSha: d.commitSha, generatedAt: d.generatedAt })
  const out = [`# Migration plan${d.repoName ? `: ${inline(d.repoName)}` : ''}`, '']
  const status = [`Plan status: ${inline(plan.status ?? 'unknown')}`, `${approved} of ${phases.length} phases approved`]
  if (prov) status.unshift(`Generated${prov.commit ? ` from commit ${code(prov.commit)}` : ''}${prov.ago ? ` ${prov.ago}` : ''}`)
  out.push(`${status.join('. ')}.`, '')
  SECTIONS.forEach(([title, build], i) => out.push(`## ${i + 1}. ${title}`, '', ...build(d), ''))
  return `${out.join('\n').replace(/\n{3,}/g, '\n\n').trimEnd()}\n`
}
