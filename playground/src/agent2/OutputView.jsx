// Agent 2 — migration plan document: sticky outline, collapsible sections, reviewable plan.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowDown, ArrowUp, ArrowUpDown, Check, ChevronDown, ChevronRight, CircleAlert, CircleCheck, CircleDashed, CircleX, Copy, FileText,
  Flag, GitBranch, Info, MessageSquarePlus, RotateCcw, ShieldAlert, TriangleAlert,
} from 'lucide-react'
import {
  ALL_TASKS, DATA_STRATEGY, DEPENDENCIES, DEP_STATUS, DEP_TOTAL, ENTRY_POINTS, HIGHLIGHTS, HOTSPOTS, LANGUAGES, MODULES, PHASES, QUESTIONS,
  REPO, RISKS, SCHEMA_STATS, SEVERITY, SIZE, STACK, STRUCTURE, SUMMARY, TABLES, riskBand, riskScore,
} from './planData.js'
import { reviewTotals, toMarkdown, useReview } from './review.js'
import Toc from './Toc.jsx'
import { StatePanel, Toast, fmtInt, useCopy } from '../components/shared.jsx'

const TONE_ICON = { danger: TriangleAlert, warning: CircleAlert, success: CircleCheck, neutral: Info }
function TonePill({ tone, children, icon }) {
  const Icon = icon || TONE_ICON[tone] || Info
  return <span className="pill" data-tone={tone}><Icon size={14} className="icon" aria-hidden="true" />{children}</span>
}
const SEV_ICON = { critical: ShieldAlert, high: TriangleAlert, medium: CircleAlert, low: Info }

/* ---------- outline model ---------- */
const SECTIONS = [
  { id: 'summary', title: 'Executive summary', subs: [] },
  { id: 'overview', title: 'Repository overview', subs: [{ id: 'overview-stack', title: 'Stack' }, { id: 'overview-structure', title: 'Structure' }, { id: 'overview-size', title: 'Size' }, { id: 'overview-entry', title: 'Entry points' }] },
  { id: 'architecture', title: 'Architecture and module map', subs: [{ id: 'architecture-modules', title: 'Module map' }, { id: 'architecture-hotspots', title: 'Coupling hotspots' }] },
  { id: 'dependencies', title: 'Dependency inventory', subs: [] },
  { id: 'data', title: 'Data layer and schema', subs: [{ id: 'data-overview', title: 'Schema overview' }, { id: 'data-tables', title: 'Tables to transform' }, { id: 'data-strategy', title: 'Migration strategy' }] },
  { id: 'risks', title: 'Risk assessment', subs: [] },
  { id: 'plan', title: 'Migration plan', subs: PHASES.map((p) => ({ id: `phase-${p.id}`, title: p.name })) },
  { id: 'questions', title: 'Open questions and decisions', subs: [] },
]
const SPY_IDS = SECTIONS.flatMap((s) => [s.id, ...s.subs.map((x) => x.id)])
const sectionOf = (id) => SECTIONS.find((s) => s.id === id || s.subs.some((x) => x.id === id))?.id

function useScrollSpy(enabled) {
  const [activeId, setActiveId] = useState(SPY_IDS[0])
  const [seen, setSeen] = useState(0)
  useEffect(() => {
    if (!enabled) return
    let raf = 0
    const run = () => {
      raf = 0
      const line = 96
      let current = SPY_IDS[0]
      for (const id of SPY_IDS) {
        const el = document.getElementById(id)
        if (el && el.getBoundingClientRect().top <= line) current = id
      }
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) current = SPY_IDS.filter((id) => document.getElementById(id)).pop()
      setActiveId(current)
      const idx = SECTIONS.findIndex((s) => s.id === sectionOf(current))
      setSeen((n) => Math.max(n, idx))
    }
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(run) }
    run()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => { window.removeEventListener('scroll', onScroll); window.removeEventListener('resize', onScroll); cancelAnimationFrame(raf) }
  }, [enabled])
  return [activeId, setActiveId, seen]
}

/* ---------- building blocks ---------- */
function Section({ id, index, title, meta, collapsed, onToggle, children }) {
  return (
    <section id={id} className="doc-section" aria-labelledby={`${id}-h`}>
      <h2 id={`${id}-h`} className="doc-h2">
        <button type="button" className="doc-toggle" aria-expanded={!collapsed} aria-controls={`${id}-body`} onClick={() => onToggle(id)}>
          {collapsed ? <ChevronRight size={18} className="icon" aria-hidden="true" /> : <ChevronDown size={18} className="icon" aria-hidden="true" />}
          <span className="doc-num tnum" aria-hidden="true">{index}</span>
          <span>{title}</span>
        </button>
        {meta ? <span className="doc-meta">{meta}</span> : null}
      </h2>
      {collapsed ? null : <div id={`${id}-body`} className="doc-body">{children}</div>}
    </section>
  )
}
const Sub = ({ id, children }) => <h3 id={id} className="doc-h3" tabIndex={-1}>{children}</h3>
const TableWrap = ({ label, children }) => <div className="table-wrap" role="region" aria-label={label} tabIndex={0}>{children}</div>
const Bar = ({ pct }) => <span className="minibar" aria-hidden="true"><span style={{ width: `${Math.max(2, pct)}%` }} /></span>

function Metric({ label, children }) { return <div className="metric"><dt>{label}</dt><dd className="tnum">{children}</dd></div> }

/* ---------- sections ---------- */
function Summary() {
  return (
    <>
      {SUMMARY.map((p, i) => <p key={i} className="prose">{p}</p>)}
      <dl className="glance-row">{HIGHLIGHTS.map((h) => <Metric key={h.label} label={h.label}>{h.value}</Metric>)}</dl>
    </>
  )
}

function Overview() {
  const maxLoc = Math.max(...STRUCTURE.map((s) => s.loc))
  return (
    <>
      <Sub id="overview-stack">Stack</Sub>
      <TableWrap label="Current and target stack">
        <table className="data"><thead><tr><th scope="col">Component</th><th scope="col">Current</th><th scope="col">Target</th></tr></thead>
          <tbody>{STACK.map((s) => <tr key={s.label}><th scope="row">{s.label}</th><td>{s.current}</td><td>{s.target}</td></tr>)}</tbody></table>
      </TableWrap>
      <Sub id="overview-structure">Structure</Sub>
      <TableWrap label="Repository structure by directory">
        <table className="data"><thead><tr><th scope="col">Path</th><th scope="col" className="num">Files</th><th scope="col">Lines of code</th><th scope="col">Notes</th></tr></thead>
          <tbody>{STRUCTURE.map((s) => (
            <tr key={s.path}><th scope="row"><code>{s.path}</code></th><td className="num tnum">{fmtInt(s.files)}</td>
              <td><span className="loc-cell"><Bar pct={(s.loc / maxLoc) * 100} /><span className="tnum">{fmtInt(s.loc)}</span></span></td><td className="muted">{s.note}</td></tr>
          ))}</tbody></table>
      </TableWrap>
      <Sub id="overview-size">Size</Sub>
      <dl className="glance-row">
        <Metric label="Files">{fmtInt(SIZE.files)}</Metric><Metric label="Lines of code">{fmtInt(SIZE.loc)}</Metric>
        <Metric label="Packages">{SIZE.packages}</Metric><Metric label="Database tables">{SIZE.tables}</Metric>
      </dl>
      <ul className="lang-list" aria-label="Language breakdown">
        {LANGUAGES.map((l) => <li key={l.name}><span>{l.name}</span><Bar pct={l.pct} /><span className="tnum muted">{l.pct.toFixed(1)}%</span></li>)}
      </ul>
      <Sub id="overview-entry">Entry points</Sub>
      <TableWrap label="Entry points">
        <table className="data"><thead><tr><th scope="col">Path</th><th scope="col">Kind</th><th scope="col">Detail</th></tr></thead>
          <tbody>{ENTRY_POINTS.map((e) => <tr key={e.path}><th scope="row"><code>{e.path}</code></th><td><span className="tag">{e.kind}</span></td><td className="muted">{e.detail}</td></tr>)}</tbody></table>
      </TableWrap>
    </>
  )
}

function Architecture() {
  const maxLoc = Math.max(...MODULES.map((m) => m.loc))
  const tone = { High: 'danger', Medium: 'warning', Low: 'neutral' }
  return (
    <>
      <Sub id="architecture-modules">Module map</Sub>
      <TableWrap label="Modules with size, coupling and migration target">
        <table className="data"><thead><tr><th scope="col">Module</th><th scope="col" className="num">Files</th><th scope="col">Lines of code</th><th scope="col">Coupling</th><th scope="col" className="num">Fan-in</th><th scope="col">Migrates to</th></tr></thead>
          <tbody>{MODULES.map((m) => (
            <tr key={m.name}><th scope="row">{m.name}<code className="sub-path">{m.path}</code></th><td className="num tnum">{m.files}</td>
              <td><span className="loc-cell"><Bar pct={(m.loc / maxLoc) * 100} /><span className="tnum">{fmtInt(m.loc)}</span></span></td>
              <td><TonePill tone={tone[m.coupling]}>{m.coupling}</TonePill></td><td className="num tnum">{m.fanIn}</td><td className="muted">{m.target}</td></tr>
          ))}</tbody></table>
      </TableWrap>
      <Sub id="architecture-hotspots">Coupling hotspots</Sub>
      <ul className="bullets">{HOTSPOTS.map((h) => <li key={h}>{h}</li>)}</ul>
    </>
  )
}

const STATUS_ORDER = ['direct', 'retire', 'partial', 'rewrite', 'none']
function Dependencies() {
  const [filter, setFilter] = useState('all')
  const [sort, setSort] = useState({ key: 'pkg', dir: 'asc' })
  const counts = Object.fromEntries(STATUS_ORDER.map((s) => [s, DEPENDENCIES.filter((d) => d.status === s).length]))
  const rows = useMemo(() => {
    const list = DEPENDENCIES.filter((d) => filter === 'all' || d.status === filter)
    const val = (d) => (sort.key === 'status' ? STATUS_ORDER.indexOf(d.status) : d[sort.key])
    return [...list].sort((a, b) => (val(a) > val(b) ? 1 : val(a) < val(b) ? -1 : 0) * (sort.dir === 'asc' ? 1 : -1))
  }, [filter, sort])
  const th = (key, label) => {
    const active = sort.key === key
    const Icon = !active ? ArrowUpDown : sort.dir === 'asc' ? ArrowUp : ArrowDown
    return (
      <th scope="col" aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}>
        <button type="button" className="th-sort" onClick={() => setSort({ key, dir: active && sort.dir === 'asc' ? 'desc' : 'asc' })}>{label}<Icon size={13} className="icon" aria-hidden="true" /></button>
      </th>
    )
  }
  return (
    <>
      <p className="prose">Showing the {DEP_TOTAL.shown} packages that need a decision, from {DEP_TOTAL.total} in total ({DEP_TOTAL.composer} Composer, {DEP_TOTAL.npm} npm). The remaining {DEP_TOTAL.total - DEP_TOTAL.shown} are transitive or already have a direct equivalent.</p>
      <div className="filters" role="group" aria-label="Filter by status">
        <button type="button" className="btn btn-sm" aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>All <span className="tnum muted">{DEPENDENCIES.length}</span></button>
        {STATUS_ORDER.map((s) => <button key={s} type="button" className="btn btn-sm" aria-pressed={filter === s} onClick={() => setFilter(filter === s ? 'all' : s)}>{DEP_STATUS[s].label} <span className="tnum muted">{counts[s]}</span></button>)}
      </div>
      <TableWrap label="Dependency inventory">
        <table className="data">
          <thead><tr>{th('pkg', 'Package')}{th('current', 'Current version')}{th('target', 'Target equivalent')}{th('status', 'Status')}<th scope="col">Note</th></tr></thead>
          <tbody>
            {rows.map((d) => (
              <tr key={d.pkg}><th scope="row"><code>{d.pkg}</code><span className="sub-path">{d.ecosystem}</span></th><td className="tnum mono">{d.current}</td><td>{d.target}</td>
                <td><TonePill tone={DEP_STATUS[d.status].tone}>{DEP_STATUS[d.status].label}</TonePill></td><td className="muted">{d.note}</td></tr>
            ))}
            {rows.length === 0 ? <tr><td colSpan={5} className="muted">No packages match this filter.</td></tr> : null}
          </tbody>
        </table>
      </TableWrap>
    </>
  )
}

function DataLayer() {
  return (
    <>
      <Sub id="data-overview">Schema overview</Sub>
      <dl className="glance-row">{SCHEMA_STATS.map((s) => <Metric key={s.label} label={s.label}>{s.value}</Metric>)}</dl>
      <p className="prose">WordPress core and WooCommerce contribute 41 tables. The 17 custom acme_* tables carry the pricing, inventory and quote data that the business depends on, and are the only tables with foreign keys.</p>
      <Sub id="data-tables">Tables to transform</Sub>
      <TableWrap label="Tables that need transformation">
        <table className="data"><thead><tr><th scope="col">Table</th><th scope="col" className="num">Rows</th><th scope="col">Becomes</th><th scope="col">Notes</th></tr></thead>
          <tbody>{TABLES.map((t) => <tr key={t.name}><th scope="row"><code>{t.name}</code></th><td className="num tnum">{fmtInt(t.rows)}</td><td>{t.target}</td><td className="muted">{t.note}</td></tr>)}</tbody></table>
      </TableWrap>
      <Sub id="data-strategy">Migration strategy</Sub>
      <ol className="steps">{DATA_STRATEGY.map((s) => <li key={s}>{s}</li>)}</ol>
    </>
  )
}

function Risks() {
  const counts = Object.keys(SEVERITY).map((k) => [k, RISKS.filter((r) => r.severity === k).length])
  return (
    <>
      <ul className="sev-summary" aria-label="Risks by severity">
        {counts.map(([k, n]) => { const Icon = SEV_ICON[k]; return <li key={k}><TonePill tone={SEVERITY[k].tone} icon={Icon}>{SEVERITY[k].label}</TonePill><span className="tnum">{n}</span></li> })}
      </ul>
      <ul className="risk-list">
        {RISKS.map((r) => (
          <li key={r.id} className="risk" data-severity={r.severity}>
            <div className="risk-head">
              <TonePill tone={SEVERITY[r.severity].tone} icon={SEV_ICON[r.severity]}>{SEVERITY[r.severity].label}</TonePill>
              <h3>{r.title}</h3>
            </div>
            <p>{r.description}</p>
            <p className="risk-mit"><strong>Mitigation.</strong> {r.mitigation}</p>
            <p className="chips">{r.files.map((f) => <code key={f} className="chip">{f}</code>)}</p>
            <p className="muted risk-phase">Addressed in {PHASES.find((p) => p.id === r.phase).name}</p>
          </li>
        ))}
      </ul>
    </>
  )
}

const TASK_STATE = {
  approved: { label: 'Approved', tone: 'success', Icon: CircleCheck },
  changes: { label: 'Needs changes', tone: 'warning', Icon: Flag },
  pending: { label: 'Pending review', tone: 'neutral', Icon: CircleDashed },
}
function PhaseCard({ phase, index, review, api }) {
  const [noteOpen, setNoteOpen] = useState(false)
  const done = phase.tasks.filter((t) => review.tasks[t.id] === 'approved').length
  const changes = phase.tasks.filter((t) => review.tasks[t.id] === 'changes').length
  const note = review.notes[phase.id] || ''
  const nameOf = (id) => PHASES.find((p) => p.id === id).name
  return (
    <article className="phase card" aria-labelledby={`phase-${phase.id}`}>
      <header className="phase-head">
        <h3 id={`phase-${phase.id}`} className="doc-h3" tabIndex={-1}><span className="phase-idx tnum">Phase {index}</span>{phase.name}</h3>
        <div className="phase-summary">
          <span className="tag mono tnum">{phase.effort}</span>
          <span className="phase-progress tnum" data-complete={done === phase.tasks.length}>{done} of {phase.tasks.length} approved{changes ? `, ${changes} need changes` : ''}</span>
        </div>
      </header>
      <dl className="phase-facts">
        <div><dt>Goals</dt><dd><ul className="bullets">{phase.goals.map((g) => <li key={g}>{g}</li>)}</ul></dd></div>
        <div><dt>Depends on</dt><dd>{phase.dependsOn.length ? phase.dependsOn.map((d) => <span key={d} className="tag">{nameOf(d)}</span>) : <span className="muted">Nothing; can start immediately</span>}</dd></div>
        <div><dt>Affected files</dt><dd className="chips">{phase.files.map((f) => <code key={f} className="chip">{f}</code>)}</dd></div>
      </dl>
      <div className="task-head"><h4>Tasks</h4>
        <button type="button" className="btn btn-sm" onClick={() => api.approvePhase(phase.id)} disabled={done === phase.tasks.length}><Check size={14} className="icon" aria-hidden="true" />Approve all</button>
      </div>
      <ul className="tasks">
        {phase.tasks.map((t) => {
          const st = review.tasks[t.id] || 'pending'
          const { label, tone, Icon } = TASK_STATE[st]
          return (
            <li key={t.id} className="task" data-state={st}>
              <div className="task-main">
                <p className="task-title">{t.title}</p>
                <p className="muted task-detail">{t.detail}</p>
              </div>
              <span className="task-effort mono tnum">{t.effort}</span>
              <span className="task-state" data-tone={tone}><Icon size={14} className="icon" aria-hidden="true" />{label}</span>
              <div className="task-actions" role="group" aria-label={`Review ${t.title}`}>
                <button type="button" className="btn btn-sm" aria-pressed={st === 'approved'} onClick={() => api.setTask(t.id, 'approved')}><Check size={14} className="icon" aria-hidden="true" />Approve</button>
                <button type="button" className="btn btn-sm" aria-pressed={st === 'changes'} onClick={() => api.setTask(t.id, 'changes')}><Flag size={14} className="icon" aria-hidden="true" />Needs changes</button>
              </div>
            </li>
          )
        })}
      </ul>
      <div className="phase-notes">
        <button type="button" className="btn btn-sm btn-ghost" aria-expanded={noteOpen} aria-controls={`note-${phase.id}`} onClick={() => setNoteOpen((o) => !o)}>
          <MessageSquarePlus size={14} className="icon" aria-hidden="true" />{note.trim() ? 'Edit note' : 'Add note'}
        </button>
        {noteOpen || note.trim() ? (
          <div id={`note-${phase.id}`}>
            <label htmlFor={`note-in-${phase.id}`} className="sr-only">Reviewer note for {phase.name}</label>
            <textarea id={`note-in-${phase.id}`} className="note-input" rows={3} value={note} placeholder="Reviewer note, included in the Markdown export" onChange={(e) => api.setNote(phase.id, e.target.value)} />
          </div>
        ) : null}
      </div>
    </article>
  )
}

function Plan({ review, api }) {
  return (
    <div className="phases">
      {PHASES.map((p, i) => (
        <div key={p.id} className="phase-anchor" id={`phase-${p.id}-wrap`}><PhaseCard phase={p} index={i} review={review} api={api} /></div>
      ))}
    </div>
  )
}

function Questions({ review, api }) {
  return (
    <ul className="questions">
      {QUESTIONS.map((q) => {
        const d = review.decisions[q.id]
        return (
          <li key={q.id} className="card question" data-decided={d != null}>
            <fieldset>
              <legend>{q.title}</legend>
              <p className="muted">{q.why}</p>
              <div className="options">
                {q.options.map((o, i) => (
                  <label key={o} className="option">
                    <input type="radio" name={q.id} checked={d === i} onChange={() => api.decide(q.id, i)} />
                    <span>{o}{i === q.recommended ? <span className="tag tag-rec">Recommended</span> : null}</span>
                  </label>
                ))}
              </div>
              <p className="q-foot">
                {d == null ? <TonePill tone="warning" icon={CircleAlert}>Open</TonePill> : <TonePill tone="success" icon={CircleCheck}>Decided</TonePill>}
                <span className="muted">Blocks {PHASES.find((p) => p.id === q.blocks).name}</span>
              </p>
            </fieldset>
          </li>
        )
      })}
    </ul>
  )
}

/* ---------- right rail / glance ---------- */
function Glance({ review, inline }) {
  const score = riskScore()
  const t = reviewTotals(review)
  return (
    <div className={inline ? 'glance glance-inline' : 'glance'}>
      <section className="card glance-card" aria-label="Overall risk">
        <h2 className="card-title">Overall risk</h2>
        <p className="risk-score"><strong className="tnum">{score}</strong><span className="muted tnum">/ 100</span><TonePill tone={score >= 60 ? 'danger' : 'warning'}>{riskBand(score)}</TonePill></p>
        <div className="meter" role="img" aria-label={`Risk score ${score} out of 100`}><span style={{ width: `${score}%` }} /></div>
        <p className="muted glance-note">Weighted by severity: critical 10, high 6, medium 3, low 1.</p>
      </section>
      <section className="card glance-card" aria-label="Estimated effort">
        <h2 className="card-title">Estimated effort</h2>
        <p className="effort tnum">34–46 <span>person-weeks</span></p>
        <p className="muted glance-note">Five phases; phases 1 and 2 can run in parallel.</p>
      </section>
      <section className="card glance-card" aria-label="Review progress">
        <h2 className="card-title">Review progress</h2>
        <div className="stack-bar" role="img" aria-label={`${t.approved} approved, ${t.changes} need changes, ${t.pending} pending of ${t.total} tasks`}>
          <span data-k="approved" style={{ flexGrow: t.approved }} /><span data-k="changes" style={{ flexGrow: t.changes }} /><span data-k="pending" style={{ flexGrow: t.pending }} />
        </div>
        <ul className="rp-list">
          <li><CircleCheck size={14} className="icon rp-ok" aria-hidden="true" />Approved <span className="tnum">{t.approved}</span></li>
          <li><Flag size={14} className="icon rp-warn" aria-hidden="true" />Needs changes <span className="tnum">{t.changes}</span></li>
          <li><CircleDashed size={14} className="icon" aria-hidden="true" />Pending review <span className="tnum">{t.pending}</span></li>
        </ul>
        <p className="muted glance-note tnum">{t.openDecisions} of {QUESTIONS.length} decisions still open.</p>
      </section>
      <section className="card glance-card" aria-label="Key metrics">
        <h2 className="card-title">Repository</h2>
        <dl className="kv">
          <div><dt>Files</dt><dd className="tnum">{fmtInt(SIZE.files)}</dd></div>
          <div><dt>Lines of code</dt><dd className="tnum">{fmtInt(SIZE.loc)}</dd></div>
          <div><dt>Packages</dt><dd className="tnum">{SIZE.packages}</dd></div>
          <div><dt>Tables</dt><dd className="tnum">{SIZE.tables}</dd></div>
        </dl>
      </section>
    </div>
  )
}

function Skeleton() {
  const lines = (n, w = ['100%', '92%', '96%', '70%']) => Array.from({ length: n }, (_, i) => <div key={i} className="skel" style={{ height: 12, width: w[i % w.length] }} />)
  return (
    <div className="a2-shell" aria-busy="true" aria-label="Loading migration plan">
      <div className="toc-skel">{lines(10, ['80%', '60%', '70%', '55%'])}</div>
      <div className="doc-skel">
        <div className="skel" style={{ height: 28, width: '55%' }} />
        {lines(5)}
        <div className="skel" style={{ height: 160, width: '100%', marginTop: 16 }} />
        {lines(4)}
      </div>
      <div className="rail-skel"><div className="skel" style={{ height: 120 }} /><div className="skel" style={{ height: 88 }} /></div>
    </div>
  )
}

/* ---------- screen ---------- */
export default function OutputView({ settings, update }) {
  const state = settings.a2State
  const { review, ...api } = useReview()
  const [copyMsg, copy] = useCopy()
  const [collapsed, setCollapsed] = useState(() => new Set())
  const [expanded, setExpanded] = useState(() => new Set(['overview']))
  const [tocOpen, setTocOpen] = useState(false)
  const ready = state === 'ready'
  const [activeId, setActiveId, seen] = useScrollSpy(ready)
  const activeSection = sectionOf(activeId)
  const pendingScroll = useRef(null)

  useEffect(() => { if (activeSection) setExpanded((s) => (s.has(activeSection) ? s : new Set(s).add(activeSection))) }, [activeSection])

  const toggleSection = useCallback((id) => setCollapsed((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n }), [])
  const navigate = useCallback((id) => {
    const section = sectionOf(id)
    setCollapsed((s) => { if (!s.has(section)) return s; const n = new Set(s); n.delete(section); return n })
    pendingScroll.current = id
    setActiveId(id)
    setTocOpen(false)
    requestAnimationFrame(() => {
      const el = document.getElementById(pendingScroll.current)
      if (!el) return
      el.scrollIntoView({ block: 'start' })
      const focusable = el.querySelector('h3[tabindex], h2 button') || el
      if (focusable.focus) focusable.focus({ preventScroll: true })
    })
  }, [setActiveId])

  const totals = reviewTotals(review)
  const extras = { plan: `${totals.approved}/${totals.total}`, questions: totals.openDecisions ? `${totals.openDecisions} open` : 'Done' }
  const allCollapsed = collapsed.size === SECTIONS.length

  if (state === 'loading') return <div className="a2 a2-wrap"><Skeleton /></div>
  if (state === 'empty') {
    return <div className="a2"><StatePanel icon={<FileText size={20} />} title="No migration plan yet" actions={<button type="button" className="btn btn-primary" onClick={() => update({ a2State: 'ready' })}>Generate plan</button>}>
      A plan is generated from a completed repository analysis. Run an analysis first, then generate the plan to review its phases, risks and open decisions here.
    </StatePanel></div>
  }
  if (state === 'error') {
    return <div className="a2"><StatePanel tone="danger" icon={<CircleX size={20} />} title="Plan generation failed" actions={<><button type="button" className="btn btn-primary" onClick={() => update({ a2State: 'ready' })}><RotateCcw size={14} className="icon" aria-hidden="true" />Retry</button><button type="button" className="btn" onClick={() => update({ a2State: 'empty' })}>Back</button></>}>
      The planner timed out after 90 seconds while clustering modules for {REPO.job}. Nothing was saved, and the analysis is unchanged. Retry to run planning again.
    </StatePanel></div>
  }

  const bodies = {
    summary: <Summary />, overview: <Overview />, architecture: <Architecture />, dependencies: <Dependencies />,
    data: <DataLayer />, risks: <Risks />, plan: <Plan review={review} api={api} />, questions: <Questions review={review} api={api} />,
  }
  const meta = { risks: `${RISKS.length} risks`, plan: `${totals.approved} of ${totals.total} tasks approved`, questions: `${totals.openDecisions} open`, dependencies: `${DEPENDENCIES.length} of ${DEP_TOTAL.total}` }

  return (
    <div className="a2 a2-wrap">
      <div className="a2-shell">
        <Toc sections={SECTIONS} activeId={activeId} activeSection={activeSection} expanded={expanded} variant={settings.sidebarStyle} seen={seen} extras={extras}
          onToggle={(id) => setExpanded((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })}
          onNavigate={navigate} open={tocOpen} onOpen={() => setTocOpen((o) => !o)}
          footer={<>Generated from <code>{REPO.commit}</code> {REPO.generated}</>} />

        <main className="doc" id="doc">
          <header className="doc-head">
            <div>
              <p className="doc-kicker"><span className="pill" data-tone="success"><CircleCheck size={14} className="icon" aria-hidden="true" />Plan ready</span><span className="muted a2-sub"><GitBranch size={14} className="icon" aria-hidden="true" /><code>{REPO.commit}</code></span></p>
              <h1>Migration plan for <span className="repo">{REPO.name}</span></h1>
            </div>
            <div className="doc-actions">
              <button type="button" className="btn" onClick={() => setCollapsed(allCollapsed ? new Set() : new Set(SECTIONS.map((s) => s.id)))}>{allCollapsed ? 'Expand all' : 'Collapse all'}</button>
              <button type="button" className="btn" onClick={api.reset}><RotateCcw size={14} className="icon" aria-hidden="true" />Reset review</button>
              <button type="button" className="btn btn-primary" onClick={() => copy(toMarkdown(review), 'Plan copied as Markdown')}><Copy size={14} className="icon" aria-hidden="true" />Copy as Markdown</button>
            </div>
          </header>

          <Glance review={review} inline />

          {SECTIONS.map((s, i) => (
            <Section key={s.id} id={s.id} index={i + 1} title={s.title} meta={meta[s.id]} collapsed={collapsed.has(s.id)} onToggle={toggleSection}>{bodies[s.id]}</Section>
          ))}
        </main>

        <aside className="rail" aria-label="Plan at a glance"><Glance review={review} /></aside>
      </div>
      <Toast message={copyMsg} />
    </div>
  )
}
