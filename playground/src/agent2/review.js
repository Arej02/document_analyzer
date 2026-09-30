// Reviewer state (task approvals, phase notes, decisions), persisted locally, plus the Markdown export.
import { useCallback, useEffect, useState } from 'react'
import { ALL_TASKS, DEPENDENCIES, DEP_STATUS, DEP_TOTAL, ENTRY_POINTS, HOTSPOTS, LANGUAGES, MODULES, PHASES, QUESTIONS, REPO, RISKS, SEVERITY, SIZE, STACK, STRUCTURE, SUMMARY, DATA_STRATEGY, TABLES, riskBand, riskScore } from './planData.js'

const KEY = 'galvanise.playground.review.v1'
const EMPTY = { tasks: {}, notes: {}, decisions: {} }

function load() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY))
    return v && typeof v === 'object' ? { ...EMPTY, ...v } : EMPTY
  } catch { return EMPTY }
}

export function useReview() {
  const [review, setReview] = useState(load)
  useEffect(() => { try { localStorage.setItem(KEY, JSON.stringify(review)) } catch { /* in-memory only */ } }, [review])
  const setTask = useCallback((id, value) => setReview((r) => {
    const tasks = { ...r.tasks }
    if (tasks[id] === value || !value) delete tasks[id]; else tasks[id] = value // pressing the active choice clears it
    return { ...r, tasks }
  }), [])
  const approvePhase = useCallback((phaseId) => setReview((r) => ({ ...r, tasks: { ...r.tasks, ...Object.fromEntries(PHASES.find((p) => p.id === phaseId).tasks.map((t) => [t.id, 'approved'])) } })), [])
  const setNote = useCallback((phaseId, text) => setReview((r) => ({ ...r, notes: { ...r.notes, [phaseId]: text } })), [])
  const decide = useCallback((qid, idx) => setReview((r) => ({ ...r, decisions: { ...r.decisions, [qid]: idx } })), [])
  const reset = useCallback(() => setReview(EMPTY), [])
  return { review, setTask, approvePhase, setNote, decide, reset }
}

export function reviewTotals(review) {
  const vals = ALL_TASKS.map((t) => review.tasks[t.id])
  const approved = vals.filter((v) => v === 'approved').length
  const changes = vals.filter((v) => v === 'changes').length
  return { total: ALL_TASKS.length, approved, changes, pending: ALL_TASKS.length - approved - changes, openDecisions: QUESTIONS.filter((q) => review.decisions[q.id] == null).length }
}

const num = (n) => n.toLocaleString('en-US')
const row = (cells) => `| ${cells.join(' | ')} |`
const table = (head, rows) => [row(head), row(head.map(() => '---')), ...rows.map(row)].join('\n')
const LABEL = { approved: 'Approved', changes: 'Needs changes' }

export function toMarkdown(review) {
  const phaseName = (id) => PHASES.find((p) => p.id === id).name
  const t = reviewTotals(review)
  const score = riskScore()
  const out = []
  out.push(`# Migration plan: ${REPO.name}`, '', `Generated from commit \`${REPO.commit}\` (${REPO.job}). Review status: ${t.approved} of ${t.total} tasks approved, ${t.changes} need changes, ${t.openDecisions} decisions open.`, '')
  out.push('## 1. Executive summary', '', ...SUMMARY.flatMap((p) => [p, '']))
  out.push('## 2. Repository overview', '', '### Stack', '', table(['Component', 'Current', 'Target'], STACK.map((s) => [s.label, s.current, s.target])), '')
  out.push('### Structure', '', table(['Path', 'Files', 'Lines', 'Notes'], STRUCTURE.map((s) => [`\`${s.path}\``, num(s.files), num(s.loc), s.note])), '')
  out.push('### Size', '', `${num(SIZE.files)} files, ${num(SIZE.loc)} lines, ${SIZE.packages} packages, ${SIZE.tables} tables. Languages: ${LANGUAGES.map((l) => `${l.name} ${l.pct}%`).join(', ')}.`, '')
  out.push('### Entry points', '', table(['Path', 'Kind', 'Detail'], ENTRY_POINTS.map((e) => [`\`${e.path}\``, e.kind, e.detail])), '')
  out.push('## 3. Architecture and module map', '', table(['Module', 'Path', 'Files', 'Lines', 'Coupling', 'Target'], MODULES.map((m) => [m.name, `\`${m.path}\``, num(m.files), num(m.loc), m.coupling, m.target])), '', '### Coupling hotspots', '', ...HOTSPOTS.map((h) => `- ${h}`), '')
  out.push('## 4. Dependency inventory', '', `Showing ${DEP_TOTAL.shown} of ${DEP_TOTAL.total} packages.`, '', table(['Package', 'Current', 'Target equivalent', 'Status'], DEPENDENCIES.map((d) => [`\`${d.pkg}\``, d.current, d.target, DEP_STATUS[d.status].label])), '')
  out.push('## 5. Data layer and schema', '', table(['Table', 'Rows', 'Target', 'Notes'], TABLES.map((x) => [`\`${x.name}\``, num(x.rows), x.target, x.note])), '', '### Migration strategy', '', ...DATA_STRATEGY.map((s, i) => `${i + 1}. ${s}`), '')
  out.push('## 6. Risk assessment', '', `Overall risk score: ${score}/100 (${riskBand(score)}).`, '')
  RISKS.forEach((r) => out.push(`- **${SEVERITY[r.severity].label}: ${r.title}.** ${r.description} Mitigation: ${r.mitigation} (Addressed in ${phaseName(r.phase)}.)`))
  out.push('', '## 7. Migration plan', '')
  PHASES.forEach((p, i) => {
    out.push(`### Phase ${i} — ${p.name}`, '', `Effort: ${p.effort}. Depends on: ${p.dependsOn.length ? p.dependsOn.map(phaseName).join(', ') : 'nothing'}.`, '', '**Goals**', '', ...p.goals.map((g) => `- ${g}`), '', '**Tasks**', '')
    p.tasks.forEach((k) => out.push(`- [${review.tasks[k.id] === 'approved' ? 'x' : ' '}] ${k.title} (${k.effort})${LABEL[review.tasks[k.id]] === 'Needs changes' ? ' **Needs changes**' : ''}\n  ${k.detail}`))
    out.push('', `**Affected files:** ${p.files.map((f) => `\`${f}\``).join(', ')}`, '')
    if (review.notes[p.id]?.trim()) out.push('**Reviewer notes**', '', ...review.notes[p.id].trim().split('\n').map((l) => `> ${l}`), '')
  })
  out.push('## 8. Open questions and decisions', '')
  QUESTIONS.forEach((q) => {
    const d = review.decisions[q.id]
    out.push(`- **${q.title}** (blocks ${phaseName(q.blocks)}) ${d == null ? 'Open. Options: ' + q.options.join(' / ') : 'Decided: ' + q.options[d]}`)
  })
  return out.join('\n') + '\n'
}
