import { useEffect, useRef, useState } from 'react'
import { useApiFetch } from './AuthProvider'
import './Sidebar.css'

const STATUS_DOT_CLASS = {
  done: 'sidebar-dot-done',
  running: 'sidebar-dot-active',
  queued: 'sidebar-dot-active',
  failed: 'sidebar-dot-failed',
}

// `https://github.com/acme/webshop` reads as `acme/webshop`; the full reference stays in the tooltip.
const shortRef = (ref) => String(ref ?? '').replace(/^https?:\/\/(www\.)?github\.com\//, '').replace(/\.git$/, '').replace(/\/$/, '')

function StatusDot({ status }) {
  return <span className={`sidebar-dot ${STATUS_DOT_CLASS[status] || 'sidebar-dot-idle'}`} aria-hidden="true" />
}

// The repository dropdown. A disclosure, not a listbox: the panel holds one button per repository plus
// "New analysis", so it is a short menu of actions. Arrow keys, Home and End move between them, Escape closes
// and returns focus to the trigger, and so does clicking or tabbing outside.
function RepositoryPicker({ projects, error, active, opening, onOpenRepository, onPrefetch, onNewAnalysis }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)
  const triggerRef = useRef(null)

  const items = () => [...(rootRef.current?.querySelectorAll('.sidebar-picker-panel button') ?? [])]

  useEffect(() => {
    if (!open) return undefined
    const onPointer = (event) => { if (!rootRef.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('mousedown', onPointer)
    // Start on the current repository so the menu opens where you already are.
    const list = items()
    const current = list.find((b) => b.getAttribute('aria-current') === 'true')
    ;(current ?? list[0])?.focus()
    return () => document.removeEventListener('mousedown', onPointer)
  }, [open])

  const onKeyDown = (event) => {
    if (!open) return
    if (event.key === 'Escape') {
      event.preventDefault()
      setOpen(false)
      triggerRef.current?.focus()
      return
    }
    const list = items()
    const index = list.indexOf(document.activeElement)
    const move = { ArrowDown: index + 1, ArrowUp: index - 1, Home: 0, End: list.length - 1 }[event.key]
    if (move === undefined) return
    event.preventDefault()
    list[(move + list.length) % list.length]?.focus()
  }

  const onBlur = (event) => {
    // Tabbing out of the widget closes it (focus moving inside it does not).
    if (open && !rootRef.current?.contains(event.relatedTarget)) setOpen(false)
  }

  const choose = (action) => () => { setOpen(false); action() }

  return (
    <div className="sidebar-picker" ref={rootRef} onKeyDown={onKeyDown} onBlur={onBlur}>
      <button
        type="button"
        ref={triggerRef}
        className="sidebar-picker-trigger"
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls="sidebar-picker-panel"
        onClick={() => setOpen((o) => !o)}
        disabled={opening}
      >
        {active ? <StatusDot status={active.latest_job_status} /> : null}
        <span className="sidebar-picker-value">
          <span className="sidebar-picker-caption">Repository</span>
          <span className="sidebar-picker-label" title={active?.source_ref}>{opening ? 'Opening…' : active ? shortRef(active.source_ref) : 'Select a repository'}</span>
        </span>
        <span className={`sidebar-chevron${open ? ' sidebar-chevron-open' : ''}`} aria-hidden="true">›</span>
      </button>

      {open && (
        <div className="sidebar-picker-panel" id="sidebar-picker-panel">
          {error && <p className="sidebar-error">{error}</p>}
          {!error && !projects && <p className="sidebar-loading">Loading…</p>}
          {projects && projects.length === 0 && <p className="sidebar-loading">Nothing ingested yet.</p>}
          {projects?.length > 0 && (
            <ul className="sidebar-picker-list" aria-label="Repositories">
              {projects.map((project) => (
                <li key={project.id}>
                  <button
                    type="button"
                    className="sidebar-picker-item"
                    aria-current={project.id === active?.id ? 'true' : undefined}
                    onClick={choose(() => onOpenRepository(project.id))}
                    onMouseEnter={() => onPrefetch(project.id)}
                    onFocus={() => onPrefetch(project.id)}
                  >
                    <StatusDot status={project.latest_job_status} />
                    <span className="sidebar-picker-label" title={project.source_ref}>{shortRef(project.source_ref)}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <button type="button" className="sidebar-picker-item sidebar-picker-new" onClick={choose(onNewAnalysis)}>
            <span className="sidebar-new-analysis-icon" aria-hidden="true">+</span> New analysis
          </button>
        </div>
      )}
    </div>
  )
}

// One sidebar for the whole app: a repository dropdown (with "New analysis" inside it), the current
// repository's runs and plans, and below them an outline slot the open page can fill (the plan
// document's sections). Picking a repository opens its newest plan, or its page when it has none, so the
// plan's sections appear straight away. Purely navigation: it reads the same Agent 1 / Agent 2 list
// endpoints the pages use and never mutates anything.
//
// `activeProjectId` is best-effort: GET /api/agent1/jobs/{id} doesn't carry a project_id (design.md §4), so
// App() can't always resolve it when landing directly on a job page. Without it the picker says
// "Select a repository" and the runs and plans stay hidden.
export function AppSidebar({ className, currentPage, activeProjectId, onNewAnalysis, onSelectProject, onSelectJob, onSelectPlan, onOutlineEl }) {
  const [projects, setProjects] = useState(null)
  const [error, setError] = useState(null)
  const [opening, setOpening] = useState(false)
  const [detail, setDetail] = useState(null) // { projectId, jobs, plans } for the active project, or null while loading
  const apiFetch = useApiFetch()
  // Plan lists by project id. Hovering a repository warms this, so clicking it navigates without a round trip.
  const plansCache = useRef(new Map())
  const loadPlans = (projectId, { fresh = false } = {}) => {
    const entry = plansCache.current.get(projectId)
    if (fresh || !entry || Date.now() - entry.at > 30_000) {
      const request = apiFetch(`/api/agent2/projects/${projectId}/plans`)
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null)
      plansCache.current.set(projectId, { request, at: Date.now() })
    }
    return plansCache.current.get(projectId).request
  }

  useEffect(() => {
    let cancelled = false
    apiFetch('/api/agent1/projects')
      .then(async (response) => {
        if (!response.ok) throw new Error(`Request failed (${response.status})`)
        return response.json()
      })
      .then((data) => { if (!cancelled) setProjects(data) })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
    // Re-fetch whenever the app returns to the landing page — the most likely moment
    // a repository was just added (fresh submit) or removed (delete).
  }, [currentPage === null])

  useEffect(() => {
    if (!activeProjectId) { setDetail(null); return undefined }
    let cancelled = false
    setDetail(null)
    Promise.all([
      apiFetch(`/api/agent1/projects/${activeProjectId}`).then((r) => (r.ok ? r.json() : null)),
      loadPlans(activeProjectId, { fresh: true }),
    ])
      .then(([project, plansBody]) => {
        if (cancelled) return
        setDetail({ projectId: activeProjectId, jobs: project?.jobs || [], plans: plansBody?.plans || [] })
      })
      .catch(() => { if (!cancelled) setDetail({ projectId: activeProjectId, jobs: [], plans: [] }) })
    return () => { cancelled = true }
  }, [activeProjectId])

  const active = projects?.find((p) => p.id === activeProjectId) ?? null
  const projectDetail = detail?.projectId === activeProjectId ? detail : null

  // Newest plan first (design_agent2.md §11). A repository with no plan yet opens its page, where Generate plan lives.
  const openRepository = async (projectId) => {
    const entry = plansCache.current.get(projectId)
    if (!entry || Date.now() - entry.at > 30_000) setOpening(true)
    try {
      const body = await loadPlans(projectId)
      const latest = body?.plans?.[0]
      if (latest) onSelectPlan(latest.id)
      else onSelectProject(projectId)
    } catch {
      onSelectProject(projectId)
    } finally {
      setOpening(false)
    }
  }

  return (
    <nav className={`app-sidebar${className ? ` ${className}` : ''}`} aria-label="Repositories and plan">
      <RepositoryPicker
        projects={projects}
        error={error}
        active={active}
        opening={opening}
        onOpenRepository={openRepository}
        onPrefetch={(id) => { loadPlans(id) }}
        onNewAnalysis={onNewAnalysis}
      />

      {activeProjectId && (
        <details className="sidebar-more">
          <summary>Runs and plans</summary>
          {!projectDetail ? (
            <p className="sidebar-loading">Loading…</p>
          ) : projectDetail.jobs.length === 0 && projectDetail.plans.length === 0 ? (
            <p className="sidebar-loading">No runs yet.</p>
          ) : (
            <>
              {projectDetail.jobs.length > 0 && (
                <div className="sidebar-subsection">
                  <p className="sidebar-subheading">Runs</p>
                  <ul>
                    {projectDetail.jobs.slice(0, 5).map((job) => (
                      <li key={job.id}>
                        <button
                          type="button"
                          className={`sidebar-subitem${currentPage?.param === 'job' && currentPage.id === job.id ? ' sidebar-subitem-active' : ''}`}
                          onClick={() => onSelectJob(job.id)}
                        >
                          <StatusDot status={job.status} />
                          {new Date(job.created_at).toLocaleDateString()}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {projectDetail.plans.length > 0 && (
                <div className="sidebar-subsection">
                  <p className="sidebar-subheading">Plans</p>
                  <ul>
                    {projectDetail.plans.map((plan) => (
                      <li key={plan.id}>
                        <button
                          type="button"
                          className={`sidebar-subitem${currentPage?.param === 'plan' && currentPage.id === plan.id ? ' sidebar-subitem-active' : ''}`}
                          onClick={() => onSelectPlan(plan.id)}
                        >
                          <span>Version {plan.version}</span>
                          <span className={`sidebar-plan-status sidebar-plan-status-${plan.status}`}>
                            {plan.status.replace('_', ' ')}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
          <button type="button" className="sidebar-view-project" onClick={() => onSelectProject(activeProjectId)}>
            View repository ›
          </button>
        </details>
      )}

      {/* Filled by the open page through OutlineSlot (the plan document's sections). */}
      <div className="sidebar-outline" ref={onOutlineEl} />
    </nav>
  )
}
