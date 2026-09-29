import { useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ReactFlow, Background, Handle, MarkerType, Position } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import './styles.css'
import { AuthProvider, useAuth, useApiFetch } from './AuthProvider'
import { SignIn } from './SignIn'
import PrimaryButton from './PrimaryButton'
import { ProductShell } from './showcase/ProductShell'
import './showcase/utilities.css'
import './showcase/product-shell.css'
import { ThemeProvider, ThemeToggle } from './theme/ThemeProvider'
import { createBrowserThemeAdapter, createThemeStore } from './theme/themeStore.js'


const ARCHIVE_EXTENSIONS = ['.zip', '.tar', '.tar.gz', '.tgz']
const isArchiveFilename = (name) => ARCHIVE_EXTENSIONS.some((ext) => name.toLowerCase().endsWith(ext))

const GITHUB_URL_RE = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/?$/
const isValidGithubUrl = (value) => GITHUB_URL_RE.test(value)

// FastAPI error bodies are either {"detail": "some message"} (HTTPException) or
// {"detail": [{"msg": "...", ...}, ...]} (pydantic validation errors) — read
// whichever shape actually came back instead of dumping raw JSON at the user.
const formatJobErrorMessage = (status, body) => {
  const detail = body?.detail
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message
  if (Array.isArray(detail) && detail.length) {
    const messages = detail.map((item) => item?.msg).filter(Boolean)
    if (messages.length) return messages.join('; ')
  }
  return `Request failed (${status})`
}

const GithubIcon = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 2.35a9.65 9.65 0 0 0-3.05 18.81c.49.09.66-.21.66-.47v-1.7c-2.69.58-3.26-1.14-3.26-1.14-.44-1.12-1.08-1.42-1.08-1.42-.88-.61.07-.6.07-.6.97.07 1.49 1 1.49 1 .87 1.48 2.27 1.05 2.82.8.09-.63.34-1.05.62-1.29-2.15-.25-4.42-1.08-4.42-4.78 0-1.05.38-1.91.99-2.58-.1-.25-.43-1.22.1-2.55 0 0 .8-.26 2.65.98a9.2 9.2 0 0 1 4.83 0c1.84-1.24 2.64-.98 2.64-.98.53 1.33.2 2.3.1 2.55.62.67.99 1.53.99 2.58 0 3.71-2.27 4.52-4.43 4.77.35.3.66.88.66 1.77v2.63c0 .26.18.56.67.47A9.65 9.65 0 0 0 12 2.35Z" />
  </svg>
)

// Landing view for "repos we've ingested" — GET /api/agent1/projects. Every row
// links to that project's own detail page (ProjectDetailPage, below) by project_id
// — not straight to a job's status page — since design.md §4 itself says the
// frontend "should generally query by project_id, not by a bare job_id" once
// there's a stable per-project view to send it to (tasks.md Phase 5's re-ingest
// action needed one anyway). A project with no completed run yet is still
// clickable: ProjectDetailPage's own job history isn't gated on latest_job_id
// the way this row's status text is.
function ProjectsList({ onSelectProject }) {
  // null = still loading; [] = loaded, genuinely empty — kept distinct so a
  // brand-new user with zero ingested repos sees an explicit message instead
  // of the same blank render a mid-flight fetch would otherwise also produce.
  const [projects, setProjects] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    apiFetch('/api/agent1/projects')
      .then(async (response) => {
        if (!response.ok) throw new Error(`Request failed (${response.status})`)
        return response.json()
      })
      .then(setProjects)
      .catch((err) => setError(err.message))
  }, [])

  if (error) return <p className="error">{error}</p>
  if (!projects) return <p className="summary-loading">Loading repositories…</p>
  if (!projects.length) {
    return <p className="summary-loading">No repositories ingested yet — analyze one above to get started.</p>
  }

  return (
    <>
      <h2>Repositories you've ingested</h2>
      <ul className="projects-list">
        {projects.map((project) => (
          <li key={project.id}>
            <button type="button" className="project-link" onClick={() => onSelectProject(project.id)}>
              {project.source_ref}
            </button>
            <span className="project-status">{project.latest_job_status ?? 'no runs yet'}</span>
          </li>
        ))}
      </ul>
    </>
  )
}

function ArchiveDropZone({ file, onFileSelected, onRejected }) {
  const fileInputRef = useRef(null)

  const acceptFile = (candidate) => {
    if (!candidate) return
    if (!isArchiveFilename(candidate.name)) {
      onRejected(`"${candidate.name}" isn't a .zip or .tar(.gz) archive.`)
      return
    }
    onFileSelected(candidate)
  }

  return (
    <div
      className="upload-state"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => { e.preventDefault(); acceptFile(e.dataTransfer.files?.[0]) }}
    >
      <input
        ref={fileInputRef}
        type="file"
        accept=".zip,.tar,.tar.gz,.tgz"
        className="file-input-hidden"
        onChange={(e) => acceptFile(e.target.files?.[0])}
      />
      {file ? (
        <>
          <strong>{file.name}</strong>
          <span>{(file.size / 1024).toFixed(1)} KB selected</span>
          <button type="button" onClick={() => fileInputRef.current?.click()}>Choose a different archive</button>
        </>
      ) : (
        <>
          <strong>Drop a .zip or .tar.gz archive here</strong>
          <span>or select a file from your computer</span>
          <button type="button" onClick={() => fileInputRef.current?.click()}>Choose archive</button>
        </>
      )}
    </div>
  )
}

const JOB_POLL_INTERVAL_MS = 3000

// design.md §4: progress_stage is one of these three while status === 'running'
// (or null before extraction starts) — 'done' isn't a real progress_stage value,
// it's mapped in here from job.status so the indicator has a final filled step.
const JOB_STAGES = [
  { key: 'extracting', label: 'Extracting' },
  { key: 'summarizing', label: 'Summarizing' },
  { key: 'grounding', label: 'Grounding' },
  { key: 'done', label: 'Done' },
]

// Agent 3's execution stages (design_agent3.md §2 execution_stage enum), with the same
// synthetic final 'done' step as JOB_STAGES above.
const EXECUTION_STAGES = [
  { key: 'prepare', label: 'Prepare' },
  { key: 'units', label: 'Units' },
  { key: 'assemble', label: 'Assemble' },
  { key: 'final_test', label: 'Final test' },
  { key: 'done', label: 'Done' },
]

// Agent 2's planning stages (design_agent2.md §2 planning_stage enum), with the same
// synthetic final 'done' step as JOB_STAGES above.
const PLANNING_STAGES = [
  { key: 'graph', label: 'Graph' },
  { key: 'cluster', label: 'Cluster' },
  { key: 'report', label: 'Report' },
  { key: 'order', label: 'Order' },
  { key: 'done', label: 'Done' },
]

function jobStageState(stages, index, status, progressStage) {
  if (status === 'done') return 'complete'
  const currentIndex = stages.findIndex((stage) => stage.key === progressStage)
  if (currentIndex === -1) return 'pending' // queued, or failed before any stage was recorded
  if (index < currentIndex) return 'complete'
  if (index === currentIndex) return status === 'failed' ? 'failed' : 'active'
  return 'pending'
}

function ProgressStages({ stages, status, progressStage, label }) {
  return (
    <ol className="job-stages" aria-label={label}>
      {stages.map((stage, index) => {
        const state = jobStageState(stages, index, status, progressStage)
        return (
          <li
            key={stage.key}
            className={`job-stage job-stage-${state}`}
            aria-current={state === 'active' || state === 'failed' ? 'step' : undefined}
          >
            <span className="job-stage-dot" aria-hidden="true" />
            {stage.label}
          </li>
        )
      })}
    </ol>
  )
}

// design.md §2's entity_type enum, pluralized for display — irregular enough
// (class/class**es**, not class+s) that a naive `+ 's'` gets it wrong.
const ENTITY_TYPE_PLURAL = {
  function: 'functions',
  class: 'classes',
  route: 'routes',
  schema: 'schemas',
  hook: 'hooks',
  dependency: 'dependencies',
}

// tasks.md Phase 4 "Surface unverified summaries distinctly ... so it's visible that
// manual review may be needed — don't hide this": a dedicated badge, not folded into
// plain body text, and shown either way — a reassuring "all verified" state when
// unverified_count is 0, not just a warning that vanishes when there's nothing to
// report. Per-entity grounding-status detail (which requires the not-yet-built
// GET /jobs/{job_id}/entities endpoint) is a separate task, not built here.
function UnverifiedBadge({ count }) {
  const needsReview = count > 0
  return (
    <p
      className={`unverified-badge ${needsReview ? 'unverified-badge-warn' : 'unverified-badge-clear'}`}
      role={needsReview ? 'status' : undefined}
    >
      <span className="unverified-badge-icon" aria-hidden="true">{needsReview ? '!' : '✓'}</span>
      {needsReview
        ? `${count} ${count === 1 ? 'summary' : 'summaries'} unverified — manual review recommended`
        : 'All summaries verified'}
    </p>
  )
}

// Validation-metrics work: grounding_confidence grades a summary/report beyond the
// binary grounding_status (worker/grounding_status.py::confidence_for) — null before a
// summary/report exists, same as grounding_status itself.
function ConfidenceBadge({ confidence }) {
  if (!confidence) return null
  return <span className={`confidence confidence-${confidence}`}>{confidence} confidence</span>
}

function GroundingIssuesList({ issues }) {
  if (!issues || issues.length === 0) return null
  return (
    <ul className="grounding-issues">
      {issues.map((issue, index) => <li key={index}>{issue}</li>)}
    </ul>
  )
}

// Results view for a finished job — GET /api/agent1/jobs/{job_id}/summary (design.md
// §4), only valid once status === 'done' (JobStatusPage only renders this then). The
// paginated entity browser is EntityBrowser, below; its per-entity grounding-status
// detail view is still a separate, not-yet-built tasks.md task.
function JobSummary({ jobId }) {
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    apiFetch(`/api/agent1/jobs/${jobId}/summary`)
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        return response.json()
      })
      .then((data) => { if (!cancelled) setSummary(data) })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [jobId])

  if (error) return <p className="error">{error}</p>
  if (!summary) return <p className="summary-loading">Loading results…</p>

  const entityTypeEntries = Object.entries(summary.entity_counts)

  return (
    <section className="job-summary" aria-label="Repository summary">
      <div className="summary-badges">
        {summary.risk_level && <p className={`risk-level risk-level-${summary.risk_level}`}>{summary.risk_level} risk</p>}
        <UnverifiedBadge count={summary.unverified_count} />
      </div>
      <p className="repo-summary-text">{summary.repo_summary || 'No repo-level summary is available for this run.'}</p>
      {entityTypeEntries.length > 0 ? (
        <ul className="entity-counts">
          {entityTypeEntries.map(([entityType, count]) => (
            <li key={entityType}><strong>{count}</strong> {count === 1 ? entityType : (ENTITY_TYPE_PLURAL[entityType] || `${entityType}s`)}</li>
          ))}
        </ul>
      ) : (
        <p className="summary-loading">No entities were extracted from this repository.</p>
      )}
    </section>
  )
}

const ENTITY_TYPE_OPTIONS = ['function', 'class', 'route', 'schema', 'hook', 'dependency']
const RISK_LEVEL_OPTIONS = ['low', 'medium', 'high']
const ENTITIES_PAGE_SIZE = 20

// tasks.md Phase 4 "GET /api/agent1/jobs/{job_id}/entities — paginated table/list in
// the knowledge base browser, with filters for entity_type and risk_level" (design.md
// §4). Filters reset the page back to the start — changing what's being browsed
// without resetting `offset` could otherwise land on an empty/out-of-range page.
// Per-entity detail (raw_facts, edges) is the separate, not-yet-built
// GET .../entities/{entity_id} task — rows here are read-only summary rows.
function EntityBrowser({ jobId }) {
  const [entityType, setEntityType] = useState('')
  const [riskLevel, setRiskLevel] = useState('')
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    const params = new URLSearchParams({ limit: String(ENTITIES_PAGE_SIZE), offset: String(offset) })
    if (entityType) params.set('entity_type', entityType)
    if (riskLevel) params.set('risk_level', riskLevel)

    apiFetch(`/api/agent1/jobs/${jobId}/entities?${params}`)
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        return response.json()
      })
      .then((data) => { if (!cancelled) setPage(data) })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [jobId, entityType, riskLevel, offset])

  const changeFilter = (setter) => (event) => { setter(event.target.value); setOffset(0) }

  return (
    <section className="entity-browser" aria-label="Knowledge base entities">
      <h2>Entities</h2>
      <div className="entity-filters">
        <label>
          Type
          <select value={entityType} onChange={changeFilter(setEntityType)}>
            <option value="">All</option>
            {ENTITY_TYPE_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
        </label>
        <label>
          Risk
          <select value={riskLevel} onChange={changeFilter(setRiskLevel)}>
            <option value="">All</option>
            {RISK_LEVEL_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
        </label>
      </div>

      {error && <p className="error">{error}</p>}
      {!error && !page && <p className="summary-loading">Loading entities…</p>}

      {page && (page.entities.length === 0 ? (
        <p className="entity-table-empty">No entities match these filters.</p>
      ) : (
        <>
          <table className="entity-table">
            <thead>
              <tr><th>Type</th><th>Name</th><th>File</th><th>Risk</th><th>Grounding</th><th>Confidence</th></tr>
            </thead>
            <tbody>
              {page.entities.map((entity) => (
                <tr key={entity.id}>
                  <td>{entity.entity_type}</td>
                  <td>{entity.name}</td>
                  <td className="entity-file-path">{entity.file_path}</td>
                  <td>{entity.risk_level ? <span className={`risk-level risk-level-${entity.risk_level}`}>{entity.risk_level}</span> : '—'}</td>
                  <td>{entity.grounding_status === 'unverified' ? <span className="unverified-badge unverified-badge-warn">unverified</span> : (entity.grounding_status || '—')}</td>
                  <td>{entity.grounding_confidence ? <ConfidenceBadge confidence={entity.grounding_confidence} /> : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="entity-pagination">
            <span>{offset + 1}–{offset + page.entities.length} of {page.total_count}</span>
            <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - ENTITIES_PAGE_SIZE))}>Previous</button>
            <button type="button" disabled={offset + ENTITIES_PAGE_SIZE >= page.total_count} onClick={() => setOffset(offset + ENTITIES_PAGE_SIZE)}>Next</button>
          </div>
        </>
      ))}
    </section>
  )
}

// Live, in-progress discovery view for JobStatusPage's queued/running state — sits
// alongside ProgressStages so there's something more than a stepper to look at while
// extraction/summarization run. Unlike EntityBrowser (the paginated table above, only
// valid once status === 'done'), GET .../entities isn't gated on job status — it's
// queryable as soon as rows are persisted — so this polls it on the same cadence as
// the job poll. Two levels only: a module node per file_path, fanning out to entity
// nodes colored by risk_level. No raw_facts/edges/AST detail here — that's
// EntityBrowser's job once the run finishes; this is a "something's happening"
// visual, not a code structure dump.
const RISK_NODE_STYLE = {
  low: { border: 'var(--ring)', background: 'rgba(160,161,207,.14)', color: 'var(--emphasis)' },
  medium: { border: 'var(--warning)', background: 'rgba(255,207,122,.14)', color: 'var(--warning)' },
  high: { border: '#ff5c4d', background: 'rgba(255,92,77,.16)', color: 'var(--danger)' },
}
const DEFAULT_RISK_NODE_STYLE = { border: 'rgba(var(--neutral-rgb),.22)', background: 'rgba(var(--neutral-rgb),.05)', color: 'var(--emphasis)' }

const ENTITY_TYPE_TAG = {
  function: 'fn', class: 'cls', route: 'rt', schema: 'sch', hook: 'hook', dependency: 'dep',
}

function EntityTreeNode({ data }) {
  const style = RISK_NODE_STYLE[data.riskLevel] || DEFAULT_RISK_NODE_STYLE
  return (
    <div className="entity-tree-node" style={{ borderColor: style.border, background: style.background }}>
      <Handle type="target" position={Position.Left} />
      <span className="entity-tree-node-tag">{ENTITY_TYPE_TAG[data.entityType] || data.entityType.slice(0, 3)}</span>
      <span className="entity-tree-node-name" style={{ color: style.color }}>{data.name}</span>
    </div>
  )
}

function ModuleTreeNode({ data }) {
  return (
    <div className={`module-tree-node ${data.expanded ? 'module-tree-node-expanded' : ''}`}>
      <span className="module-tree-node-name">{data.label}</span>
      <span className="module-tree-node-count">{data.count}</span>
      <Handle type="source" position={Position.Right} />
    </div>
  )
}

// Registered once at module scope — a nodeTypes object re-created every render
// makes React Flow warn (and re-mount node components) on each poll.
const LIVE_TREE_NODE_TYPES = { module: ModuleTreeNode, entity: EntityTreeNode }

const LIVE_TREE_ENTITY_LIMIT = 100
// Rendered-node cap (module nodes + entity nodes) — once exceeded, entities in the
// least-recently-active modules collapse back down to just their module node with a
// count badge, so the canvas stays legible instead of growing forever.
const LIVE_TREE_NODE_CAP = 70
const LIVE_TREE_MODULE_X = 20
const LIVE_TREE_ENTITY_X = 300
const LIVE_TREE_ROW_HEIGHT = 44
const LIVE_TREE_MODULE_GAP = 20

// Pure layout builder: groups entities by file_path, sorts modules by how recently
// they last gained an entity (most active first), then greedily expands modules in
// that order until the node cap is spent — remaining modules render collapsed
// (module node only). Within an expanded module, the most recently added entities
// are the ones shown if the module itself doesn't fully fit the remaining budget.
function buildEntityTree(entities) {
  const modules = new Map()
  entities.forEach((entity) => {
    const key = entity.file_path || '(unknown file)'
    if (!modules.has(key)) modules.set(key, [])
    modules.get(key).push(entity)
  })

  const moduleList = Array.from(modules.entries()).map(([filePath, moduleEntities]) => ({
    filePath,
    entities: moduleEntities,
    recency: Math.max(...moduleEntities.map((e) => e._firstSeenPoll)),
  }))
  moduleList.sort((a, b) => b.recency - a.recency)

  let remainingBudget = Math.max(0, LIVE_TREE_NODE_CAP - moduleList.length)
  const nodes = []
  const edges = []
  let y = 0

  moduleList.forEach((module) => {
    const sortedEntities = [...module.entities].sort((a, b) => b._firstSeenPoll - a._firstSeenPoll)
    const showCount = Math.min(sortedEntities.length, remainingBudget)
    const visible = sortedEntities.slice(0, showCount)
    remainingBudget -= showCount

    const blockHeight = Math.max(1, visible.length) * LIVE_TREE_ROW_HEIGHT
    const moduleId = `module:${module.filePath}`
    const fileName = module.filePath.split('/').pop() || module.filePath

    nodes.push({
      id: moduleId,
      type: 'module',
      position: { x: LIVE_TREE_MODULE_X, y: y + (blockHeight - LIVE_TREE_ROW_HEIGHT) / 2 },
      data: { label: fileName, count: module.entities.length, expanded: visible.length > 0 },
      draggable: false,
      selectable: false,
    })

    visible.forEach((entity, index) => {
      nodes.push({
        id: entity.id,
        type: 'entity',
        position: { x: LIVE_TREE_ENTITY_X, y: y + index * LIVE_TREE_ROW_HEIGHT },
        data: { name: entity.name, entityType: entity.entity_type, riskLevel: entity.risk_level },
        draggable: false,
        selectable: false,
      })
      edges.push({
        id: `edge:${entity.id}`,
        source: moduleId,
        target: entity.id,
        type: 'smoothstep',
        style: { stroke: 'rgba(var(--neutral-rgb),.18)' },
      })
    })

    y += blockHeight + LIVE_TREE_MODULE_GAP
  })

  return { nodes, edges }
}

function LiveEntityTree({ jobId }) {
  const [entities, setEntities] = useState([])
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()
  const pollCountRef = useRef(0)

  useEffect(() => {
    let cancelled = false
    let timeoutId

    const poll = async () => {
      try {
        const response = await apiFetch(`/api/agent1/jobs/${jobId}/entities?limit=${LIVE_TREE_ENTITY_LIMIT}`)
        if (!response.ok) throw new Error(`Request failed (${response.status})`)
        const data = await response.json()
        const pollIndex = pollCountRef.current
        pollCountRef.current += 1
        if (cancelled) return
        // Merge-by-id, tagging only genuinely new rows with this poll's index — entities
        // already known keep their original _firstSeenPoll so recency ordering (and the
        // "don't re-animate" behavior, which just falls out of React Flow reusing the
        // same node id across polls) stay stable.
        setEntities((prev) => {
          const existingIds = new Set(prev.map((e) => e.id))
          const additions = data.entities.filter((e) => !existingIds.has(e.id))
          if (additions.length === 0) return prev
          return [...prev, ...additions.map((e) => ({ ...e, _firstSeenPoll: pollIndex }))]
        })
        setError(null)
      } catch (err) {
        if (!cancelled) setError(err.message)
      } finally {
        if (!cancelled) timeoutId = setTimeout(poll, JOB_POLL_INTERVAL_MS)
      }
    }

    poll()
    return () => { cancelled = true; clearTimeout(timeoutId) }
  }, [jobId])

  const { nodes, edges } = useMemo(() => buildEntityTree(entities), [entities])

  if (entities.length === 0) {
    return (
      <div className="live-entity-tree live-entity-tree-empty">
        {error ? <p className="error">{error}</p> : <p className="summary-loading">Scanning files…</p>}
      </div>
    )
  }

  return (
    <div className="live-entity-tree">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={LIVE_TREE_NODE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} color="rgba(var(--neutral-rgb),.06)" />
      </ReactFlow>
    </div>
  )
}

// tasks.md Phase 5 "'Re-ingest' action on a project's page, pointing at the same
// source (upload new zip or re-pull GitHub URL)" — POST /api/agent1/projects/
// {project_id}/reingest (design.md §4). Request shape follows the project's own
// (fixed, not caller-chosen) source_type: a github project just re-pulls
// project.source_ref, optionally with a fresh private-repo token; an upload
// project has no stored original archive to re-pull, so it requires picking a
// new one, same ArchiveDropZone used by the main submission form.
function ReingestAction({ project, onReingested }) {
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [token, setToken] = useState('')
  const [showToken, setShowToken] = useState(false)
  const [file, setFile] = useState(null)
  const apiFetch = useApiFetch()

  const reingest = async (event) => {
    event.preventDefault()
    if (project.source_type === 'upload' && !file) return

    setError(null)
    setSubmitting(true)
    try {
      const response = project.source_type === 'github'
        ? await apiFetch(`/api/agent1/projects/${project.id}/reingest`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(token.trim() ? { access_token: token.trim() } : {}),
          })
        : await apiFetch(`/api/agent1/projects/${project.id}/reingest`, {
            method: 'POST',
            body: (() => {
              const formData = new FormData()
              formData.append('file', file)
              return formData
            })(),
          })

      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(formatJobErrorMessage(response.status, body))
      }
      const data = await response.json()
      onReingested(data.job_id)
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="reingest-action" onSubmit={reingest} aria-label="Re-ingest this repository">
      <h2>Re-ingest</h2>
      {project.source_type === 'github' ? (
        <>
          <p className="reingest-hint">Re-pulls <code>{project.source_ref}</code> fresh.</p>
          <button type="button" className="token-toggle" onClick={() => setShowToken(!showToken)}>
            <span>{showToken ? '⌄' : '›'}</span> Private repository? Add a GitHub token
          </button>
          {showToken && (
            <input
              className="token-input"
              aria-label="GitHub personal access token"
              placeholder="GitHub personal access token"
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          )}
        </>
      ) : (
        <>
          <p className="reingest-hint">Pick a new archive to re-ingest — there's no original upload to re-pull from.</p>
          <ArchiveDropZone file={file} onFileSelected={setFile} onRejected={setError} />
        </>
      )}
      <PrimaryButton
        label="Re-ingest"
        type="submit"
        disabled={project.source_type === 'upload' && !file}
        loading={submitting}
        style={{ marginTop: 14 }}
      />
      {error && <p className="error">{error}</p>}
    </form>
  )
}

// tasks_agent2.md Phase 1 "Generate plan" — POST /api/agent2/projects/{project_id}/
// plans (design_agent2.md §11) with no body, so the backend defaults the source to
// projects.latest_job_id. Gated on latest_job_status === 'done' here too, but the
// backend re-checks, and its 409s (source not done, a planning job already active)
// are shown verbatim.
function GeneratePlanAction({ project, onPlanningJobCreated }) {
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const apiFetch = useApiFetch()
  const ready = project.latest_job_status === 'done'

  const generate = async (event) => {
    event.preventDefault()
    if (!ready) return

    setError(null)
    setSubmitting(true)
    try {
      const response = await apiFetch(`/api/agent2/projects/${project.id}/plans`, { method: 'POST' })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(formatJobErrorMessage(response.status, body))
      }
      const data = await response.json()
      onPlanningJobCreated(data.planning_job_id)
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="reingest-action" onSubmit={generate} aria-label="Generate a migration plan">
      <h2>Migration plan</h2>
      <p className="reingest-hint">
        {ready
          ? 'Builds an ordered migration plan from the latest ingestion run.'
          : 'Finish an ingestion run first — a plan needs a completed run to build from.'}
      </p>
      <PrimaryButton
        label="Generate plan"
        type="submit"
        disabled={!ready}
        loading={submitting}
        style={{ marginTop: 14 }}
      />
      {error && <p className="error">{error}</p>}
    </form>
  )
}

// tasks_agent2.md Phase 5 "Plans" section — GET /api/agent2/projects/{project_id}/
// plans (design_agent2.md §11): every version, newest first. The backend runs the
// §9.1 stale check before listing, so `status` is already `stale` where it applies.
// `is_stale` drives the badge separately, because a `rejected` plan on an older run
// keeps its status but was still built on an older ingestion run.
function PlansList({ projectId, onSelectPlan }) {
  const [plans, setPlans] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    apiFetch(`/api/agent2/projects/${projectId}/plans`)
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        return response.json()
      })
      .then((data) => { if (!cancelled) setPlans(data.plans) })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [projectId])

  return (
    <section className="job-history" aria-label="Plans">
      <h2>Plans</h2>
      {error && <p className="error">{error}</p>}
      {!error && !plans && <p className="summary-loading">Loading plans…</p>}
      {plans && plans.length === 0 && <p className="summary-loading">No plans yet — generate one above.</p>}
      {plans && plans.length > 0 && (
        <ul className="projects-list">
          {plans.map((plan) => (
            <li key={plan.id}>
              <button type="button" className="project-link" onClick={() => onSelectPlan(plan.id)}>
                Version {plan.version}
                <span className="run-diff-hint"> · {new Date(plan.created_at).toLocaleString()}</span>
              </button>
              <span className="plan-row-badges">
                {plan.is_stale && (
                  <span className="unverified-badge unverified-badge-warn plan-stale-badge">built on an older ingestion run</span>
                )}
                <span className={`plan-status plan-status-${plan.status}`}>{plan.status.replace('_', ' ')}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

// tasks.md Phase 5's home for the re-ingest action above — GET /api/agent1/projects/
// {project_id} (design.md §4: source info, full job history most-recent-first, and
// the latest job's status) had never had a frontend view built for it before this
// (Phase 1's own note on that endpoint flagged this as still-missing). Each job in
// the history links to its own status page (JobStatusPage) via the same navigation
// ReingestAction's own success handler uses.
// DELETE /api/agent1/projects/{project_id} (design.md §4): wipes every run, summary,
// planning job and plan for the repository. Two-step inline confirm rather than
// window.confirm, so the warning can say exactly what goes. The backend's 409
// (a run or planning job still queued/running) is shown verbatim.
function DeleteProjectAction({ project, onDeleted }) {
  const [confirming, setConfirming] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  const remove = async () => {
    setError(null)
    setDeleting(true)
    try {
      const response = await apiFetch(`/api/agent1/projects/${project.id}`, { method: 'DELETE' })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(formatJobErrorMessage(response.status, body))
      }
      onDeleted()
    } catch (err) {
      setError(err.message)
      setDeleting(false)
    }
  }

  return (
    <section className="reingest-action delete-action" aria-label="Delete this repository">
      <h2>Delete repository</h2>
      {confirming ? (
        <>
          <p className="reingest-hint">
            This permanently deletes all {project.jobs.length} run{project.jobs.length === 1 ? '' : 's'} of{' '}
            <code>{project.source_ref}</code>, their summaries, source snapshots, every migration plan built
            from them, and the code generated from those plans. It can't be undone.
          </p>
          <div className="delete-confirm-row">
            <button type="button" className="danger-button" onClick={remove} disabled={deleting}>
              {deleting ? 'Deleting…' : 'Yes, delete everything'}
            </button>
            <button type="button" className="secondary-button" onClick={() => { setConfirming(false); setError(null) }} disabled={deleting}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="reingest-hint">Removes this repository and everything ingested or planned from it.</p>
          <button type="button" className="danger-button" onClick={() => setConfirming(true)}>
            Delete repository…
          </button>
        </>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  )
}

function ProjectDetailPage({ projectId, onSelectJob, onSelectPlanningJob, onSelectPlan, onBackToList }) {
  const [project, setProject] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    apiFetch(`/api/agent1/projects/${projectId}`)
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        return response.json()
      })
      .then((data) => { if (!cancelled) setProject(data) })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [projectId])

  return (
    <main className="page-shell">

      <section className="hero project-detail" aria-label="Project detail">
        <button type="button" className="token-toggle back-link" onClick={onBackToList}>
          <span>‹</span> Back to repositories
        </button>
        {error && <p className="error">{error}</p>}
        {!error && !project && <p className="summary-loading">Loading project…</p>}
        {project && (
          <>
            <p className="eyebrow">PROJECT</p>
            <h1 className="project-detail-title">{project.source_ref}</h1>
            <p className="project-status project-detail-status">
              {project.latest_job_status ? `Latest run: ${project.latest_job_status}` : 'No completed runs yet'}
            </p>
            <ReingestAction project={project} onReingested={onSelectJob} />
            <GeneratePlanAction project={project} onPlanningJobCreated={onSelectPlanningJob} />
          </>
        )}
      </section>
      {project && (
        <section className="job-history" aria-label="Run history">
          <h2>Run history</h2>
          {project.jobs.length === 0 ? (
            <p className="summary-loading">No runs yet.</p>
          ) : (
            <ul className="projects-list">
              {project.jobs.map((job) => (
                <li key={job.id}>
                  <button type="button" className="project-link" onClick={() => onSelectJob(job.id)}>
                    {new Date(job.created_at).toLocaleString()}
                    {job.changed_file_count != null && (
                      <span className="run-diff-hint">
                        {job.unchanged_file_count === 0
                          ? ' · fresh'
                          : ` · ${job.changed_file_count} changed, ${job.unchanged_file_count} reused`}
                      </span>
                    )}
                  </button>
                  <span className="project-status">{job.status}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
      {project && <PlansList projectId={projectId} onSelectPlan={onSelectPlan} />}
      {project && (
        <section className="job-history" aria-label="Danger zone">
          <DeleteProjectAction project={project} onDeleted={onBackToList} />
        </section>
      )}
    </main>
  )
}

// tasks.md Phase 5 "Show something distinguishing a fresh ingestion from a cached/
// incremental one ... once the backend exposes that count" — GET /api/agent1/jobs/
// {job_id} now does (design.md §4: changed_file_count/unchanged_file_count/
// removed_file_count, written by worker/diff.py early in the `extracting` stage).
// `null` means the diff hasn't run yet (job still `queued`, or extraction hasn't
// reached it) — render nothing rather than a misleading "0 changed". A project's
// very first ingestion has unchanged_file_count === 0 by construction (diff.py: no
// previous job means everything counts as changed), which is exactly what
// distinguishes "fresh" from "incremental" here — no extra flag needed.
function DiffSummary({ job }) {
  if (job.changed_file_count == null) return null
  const { changed_file_count: changed, unchanged_file_count: unchanged, removed_file_count: removed } = job

  if (unchanged === 0) {
    const total = changed + removed
    return (
      <p className="diff-summary">
        Fresh ingestion — {total} {total === 1 ? 'file' : 'files'} processed
      </p>
    )
  }

  return (
    <p className="diff-summary diff-summary-incremental">
      {changed} {changed === 1 ? 'file' : 'files'} changed since last run
      {' '}(<strong>{unchanged}</strong> reused{removed > 0 ? `, ${removed} removed` : ''})
    </p>
  )
}

// Extraction-quality validation metric (design.md §2 ingestion_jobs.extraction_stats,
// worker/run.py::_run_extractors) — per-extractor entities/edges produced and files that
// failed to parse. Null until the extraction stage has run for this job.
function ExtractionStats({ job }) {
  if (!job.extraction_stats) return null
  const entries = Object.entries(job.extraction_stats)
  if (entries.length === 0) return null
  return (
    <div className="extraction-stats">
      <strong>Extraction</strong>
      <ul className="extraction-stats-list">
        {entries.map(([extractor, stats]) => (
          <li key={extractor}>
            {extractor}: {stats.entities} {stats.entities === 1 ? 'entity' : 'entities'}, {stats.edges} {stats.edges === 1 ? 'edge' : 'edges'}
            {stats.files_errored > 0 && (
              <span className="extraction-stats-errored"> · {stats.files_errored} {stats.files_errored === 1 ? 'file' : 'files'} failed to parse</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

// What extraction and source evidence actually covered — GET /api/agent1/jobs/{job_id}/
// coverage (design.md §4, ingestion_jobs.extraction_coverage). Graphify handles every
// non-PHP code language; this says which files it extracted, only partially extracted or
// couldn't, and which files have no stored excerpt, so a reviewer knows what a summary or
// an answer could never have seen.
function ExtractionCoverage({ jobId }) {
  const [coverage, setCoverage] = useState(undefined)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    apiFetch(`/api/agent1/jobs/${jobId}/coverage`)
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        return response.json()
      })
      .then((data) => { if (!cancelled) setCoverage(data.coverage) })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [jobId])

  if (error) return <p className="error">{error}</p>
  if (coverage === undefined) return <p className="summary-loading">Loading coverage…</p>
  if (!coverage) return <p className="summary-loading">No coverage report for this run (it predates coverage reporting).</p>

  const graphify = coverage.graphify
  const evidence = coverage.evidence
  const languages = Object.entries(graphify?.by_language || {})
  const graphifyStatus = {
    ran: null,
    unavailable: `Graphify was unavailable${graphify?.reason ? ` (${graphify.reason})` : ''} — only PHP was analysed.`,
    no_candidate_files: 'No non-PHP code files to analyse.',
    no_analysable_files: 'Graphify could not analyse any of the non-PHP files.',
  }[graphify?.status]

  return (
    <div className="extraction-stats coverage-report">
      <strong>Coverage</strong>
      {graphifyStatus && <p className="coverage-note">{graphifyStatus}</p>}
      {languages.length > 0 && (
        <ul className="extraction-stats-list">
          {languages.map(([language, counts]) => (
            <li key={language}>
              {language}: {Object.entries(counts).map(([status, n]) => `${n} ${status}`).join(', ')}
            </li>
          ))}
          {graphify.files_claimed_by_php_extractors > 0 && <li>php: {graphify.files_claimed_by_php_extractors} via PHP extractors</li>}
          {graphify.files_without_code_extractor > 0 && (
            <li className="extraction-stats-errored">{graphify.files_without_code_extractor} files with no code extractor</li>
          )}
        </ul>
      )}
      {graphify?.not_extracted_count > 0 && (
        <details className="coverage-details">
          <summary>{graphify.not_extracted_count} {graphify.not_extracted_count === 1 ? 'file' : 'files'} not fully extracted</summary>
          <ul>
            {graphify.not_extracted_sample.map((file) => (
              <li key={file.path}><code>{file.path}</code> — {file.status}{file.reason ? `: ${file.reason}` : ''}</li>
            ))}
          </ul>
        </details>
      )}
      {evidence && (
        <p className="coverage-note">
          Source evidence: {evidence.chunks} {evidence.chunks === 1 ? 'excerpt' : 'excerpts'} from {evidence.files_chunked} files
          {evidence.files_skipped_count > 0 && <span className="extraction-stats-errored"> · {evidence.files_skipped_count} files without excerpts</span>}
          {evidence.redacted_chunks > 0 && <span> · {evidence.redacted_chunks} with secrets redacted</span>}
        </p>
      )}
    </div>
  )
}

// "Ask the codebase" — POST /api/agent1/jobs/{job_id}/ask (design.md §4). Answers cite
// the stored source excerpts (E#) and graph relationships (R#) they rest on; the backend
// rejects an answer that cites anything it wasn't given, so every chip below points at
// something real. Clicking a citation shows the exact excerpt.
function AskCodebase({ jobId }) {
  const [question, setQuestion] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [answer, setAnswer] = useState(null)
  const [error, setError] = useState(null)
  const [openCitation, setOpenCitation] = useState(null)
  const apiFetch = useApiFetch()

  const ask = async (event) => {
    event.preventDefault()
    if (!question.trim() || submitting) return
    setSubmitting(true)
    setError(null)
    setOpenCitation(null)
    try {
      const response = await apiFetch(`/api/agent1/jobs/${jobId}/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(formatJobErrorMessage(response.status, body))
      setAnswer(body)
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  const evidenceById = Object.fromEntries((answer?.context?.evidence || []).map((e) => [e.evidence_id, e]))
  const citationChips = (ids) => ids.map((id) => (
    <button key={id} type="button" className="citation-chip" onClick={() => setOpenCitation(openCitation === id ? null : id)}>{id}</button>
  ))
  const opened = openCitation && evidenceById[openCitation]
  const openedRelationship = openCitation && !opened && answer?.citations.find((c) => c.id === openCitation)

  return (
    <section className="entity-browser ask-codebase" aria-label="Ask the codebase">
      <h2>Ask the codebase</h2>
      <form className="ask-form" onSubmit={ask}>
        <input
          type="text"
          value={question}
          maxLength={2000}
          placeholder="e.g. What calls charge_card, and what does it write?"
          onChange={(event) => setQuestion(event.target.value)}
        />
        <button type="submit" disabled={submitting || !question.trim()}>{submitting ? 'Asking…' : 'Ask'}</button>
      </form>
      {error && <p className="error">{error}</p>}
      {answer && (
        <div className="ask-answer">
          {answer.outcome === 'insufficient_evidence' && <p className="unverified-badge unverified-badge-warn">insufficient evidence</p>}
          <p className="repo-summary-text">{answer.summary}</p>
          {[['Observed in the code', answer.observations], ['Interpretation', answer.interpretation]].map(([title, claims]) => (
            claims.length > 0 && (
              <div key={title}>
                <strong>{title}</strong>
                <ul className="ask-claims">
                  {claims.map((claim, i) => <li key={i}>{claim.text} {citationChips(claim.citations)}</li>)}
                </ul>
              </div>
            )
          ))}
          {answer.citations.length > 0 && (
            <p className="ask-sources">
              Sources: {answer.citations.map((c) => (
                <span key={c.id}>
                  {citationChips([c.id])} {c.kind === 'evidence' ? <code>{c.file_path}:{c.line_start}-{c.line_end}</code> : 'relationship'}{' '}
                </span>
              ))}
            </p>
          )}
          {opened && (
            <figure className="ask-excerpt">
              <figcaption><code>{opened.file_path}</code> lines {opened.line_start}–{opened.line_end}</figcaption>
              <pre>{opened.content}</pre>
            </figure>
          )}
          {openedRelationship && <pre className="ask-excerpt">{openedRelationship.text}</pre>}
          {answer.limitations.length > 0 && (
            <details className="coverage-details">
              <summary>Limitations ({answer.limitations.length})</summary>
              <ul>{answer.limitations.map((line) => <li key={line}>{line}</li>)}</ul>
            </details>
          )}
        </div>
      )}
    </section>
  )
}

// Landing spot after a successful submit — job_id lives in a `?job=` query param
// (no router in this app yet) so it survives a refresh/share. Polls
// GET /api/agent1/jobs/{job_id} until the job leaves a terminal state's need
// for further polling.
//
// "Retry" for a failed job routes back to the submission form rather than
// resubmitting the same source automatically: GET /api/agent1/jobs/{job_id}
// (design.md §4) returns only {job_id, status, progress_stage, error} — no
// project_id or source_ref/repo_url to resubmit with. POST /api/agent1/
// projects/{project_id}/reingest (ProjectDetailPage, above) is exactly the
// "resubmit this source" action now, but this page can't route straight to
// it without a project_id this response still doesn't carry — reaching a
// failed job via its project's run history and re-ingesting from there is
// the supported path today. Re-entering the source here is the fallback.
function JobStatusPage({ jobId, onBackToForm }) {
  const [job, setJob] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    let timeoutId

    const poll = async () => {
      try {
        const response = await apiFetch(`/api/agent1/jobs/${jobId}`)
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        const data = await response.json()
        if (cancelled) return
        setJob(data)
        if (data.status === 'queued' || data.status === 'running') {
          timeoutId = setTimeout(poll, JOB_POLL_INTERVAL_MS)
        }
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    }

    poll()
    return () => { cancelled = true; clearTimeout(timeoutId) }
  }, [jobId])

  const failed = job?.status === 'failed'
  const done = job?.status === 'done'

  return (
    <main className="page-shell">

      <section className="hero job-status" aria-label="Job status">
        <p className="eyebrow">{job ? job.status.toUpperCase() : (error ? 'ERROR' : 'LOADING')}</p>
        <h1>Job <code>{jobId}</code></h1>
        {error && <p className="error">{error}</p>}
        {job && <DiffSummary job={job} />}
        {job && <ExtractionStats job={job} />}
        {job && <ProgressStages stages={JOB_STAGES} status={job.status} progressStage={job.progress_stage} label="Ingestion progress" />}
        {job && (job.status === 'queued' || job.status === 'running') && <LiveEntityTree jobId={jobId} />}
        {failed && (
          <p className="error job-failed-message">
            Ingestion failed: {job.error || 'an unknown error occurred.'}
          </p>
        )}
        {done && <JobSummary jobId={jobId} />}
        {done && <ExtractionCoverage jobId={jobId} />}
        <PrimaryButton
          label={failed ? 'Retry' : 'Analyze'}
          type="button"
          onClick={onBackToForm}
          style={{ marginTop: 8 }}
        />
      </section>
      {done && <EntityBrowser jobId={jobId} />}
      {done && <AskCodebase jobId={jobId} />}
    </main>
  )
}

// tasks_agent2.md Phase 1 "Planning status" — polls GET /api/agent2/planning-jobs/
// {id} (design_agent2.md §11) until the job is terminal, same loop shape as
// JobStatusPage. Unlike that page, the response carries project_id, so "Try again"
// can go straight back to the project page (where "Generate plan" lives) even
// after a refresh.
function PlanningJobStatusPage({ planningJobId, onBackToProject, onViewPlan, onBackToForm }) {
  const [job, setJob] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    let timeoutId

    const poll = async () => {
      try {
        const response = await apiFetch(`/api/agent2/planning-jobs/${planningJobId}`)
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        const data = await response.json()
        if (cancelled) return
        setJob(data)
        if (data.status === 'queued' || data.status === 'running') {
          timeoutId = setTimeout(poll, JOB_POLL_INTERVAL_MS)
        }
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    }

    poll()
    return () => { cancelled = true; clearTimeout(timeoutId) }
  }, [planningJobId])

  const failed = job?.status === 'failed'
  const done = job?.status === 'done'

  return (
    <main className="page-shell">

      <section className="hero job-status" aria-label="Planning job status">
        <p className="eyebrow">{job ? job.status.toUpperCase() : (error ? 'ERROR' : 'LOADING')}</p>
        <h1>Planning job <code>{planningJobId}</code></h1>
        {error && <p className="error">{error}</p>}
        {!error && !job && <p className="summary-loading">Loading planning job…</p>}
        {job && (
          <ProgressStages
            stages={PLANNING_STAGES}
            status={job.status}
            progressStage={job.progress_stage}
            label="Planning progress"
          />
        )}
        {failed && (
          <>
            <p className="error job-failed-message">Planning failed: {job.error || 'an unknown error occurred.'}</p>
            <PrimaryButton
              label="Try again"
              type="button"
              onClick={() => onBackToProject(job.project_id)}
              style={{ marginTop: 8 }}
            />
          </>
        )}
        {done && (job.plan_id ? (
          <PrimaryButton label="View plan" type="button" onClick={() => onViewPlan(job.plan_id)} style={{ marginTop: 8 }} />
        ) : (
          <p className="error">The planning job finished, but no plan was recorded for it.</p>
        ))}
        {error && (
          <PrimaryButton label="Back" type="button" onClick={onBackToForm} style={{ marginTop: 8 }} />
        )}
      </section>
    </main>
  )
}

// Unit flags from GET /plans/{plan_id} (design_agent2.md §11), in display form.
const UNIT_FLAG_LABELS = { foundation: 'foundation', merged: 'merged' }

// `edit` is set while the plan takes overrides ("Overrides" in tasks_agent2.md):
// { checked, onToggle, onMove(delta), isFirst, isLast, disabled }.
function PlanUnitRow({ unit, selected, onSelect, edit }) {
  const needsFacade = unit.flags.includes('needs_facade')
  const label = unit.label || `Unit ${unit.unit_order}`
  return (
    <li className={edit ? 'plan-unit-row plan-unit-row-editable' : 'plan-unit-row'}>
      {edit && (
        <input
          type="checkbox"
          className="plan-unit-select"
          checked={edit.checked}
          onChange={() => edit.onToggle(unit.cluster_id)}
          disabled={edit.disabled}
          aria-label={`Select ${label} to merge`}
        />
      )}
      <button
        type="button"
        className={`plan-unit${selected ? ' plan-unit-selected' : ''}`}
        aria-current={selected ? 'true' : undefined}
        onClick={() => onSelect(unit.cluster_id)}
      >
        <span className="plan-unit-order">{unit.unit_order}</span>
        <span className="plan-unit-label">
          {needsFacade && (
            <span className="plan-facade-marker" title="Part of a dependency cycle that needs a facade">⚠</span>
          )}
          {unit.label || <em>Unlabeled unit</em>}
        </span>
        <span className="plan-unit-meta">
          {unit.flags.filter((flag) => UNIT_FLAG_LABELS[flag]).map((flag) => (
            <span key={flag} className="plan-flag">{UNIT_FLAG_LABELS[flag]}</span>
          ))}
          {needsFacade && <span className="plan-flag plan-flag-warn">needs facade</span>}
          <span className="plan-unit-size">{unit.size ?? '—'} {unit.size === 1 ? 'entity' : 'entities'}</span>
          {unit.risk_level && <span className={`risk-level risk-level-${unit.risk_level}`}>{unit.risk_level}</span>}
          {unit.grounding_status === 'unverified' && (
            <span className="unverified-badge unverified-badge-warn">unverified</span>
          )}
          <ConfidenceBadge confidence={unit.grounding_confidence} />
        </span>
      </button>
      {edit && (
        <span className="plan-unit-move" role="group" aria-label={`Move ${label}`}>
          <button
            type="button"
            onClick={() => edit.onMove(unit, -1)}
            disabled={edit.disabled || edit.isFirst}
            aria-label={`Move ${label} up`}
            title="Move up"
          >
            ↑
          </button>
          <button
            type="button"
            onClick={() => edit.onMove(unit, 1)}
            disabled={edit.disabled || edit.isLast}
            aria-label={`Move ${label} down`}
            title="Move down"
          >
            ↓
          </button>
        </span>
      )}
    </li>
  )
}

function PlanApprovalState({ approval }) {
  if (!approval) return <span className="plan-status">not reviewed</span>
  return (
    <span className="plan-approval">
      <span className={`plan-status plan-status-${approval.status}`}>{approval.status}</span>
      {approval.note && <span className="plan-approval-note">“{approval.note}”</span>}
    </span>
  )
}

const EDGE_DIRECTION_LABELS = { outgoing: 'depends on', incoming: 'depended on by', internal: 'internal' }

function PlanContractSection({ title, items, render }) {
  return (
    <div className="plan-contract-section">
      <h4>{title}</h4>
      {items.length === 0 ? <p className="summary-loading">None.</p> : <ul>{items.map(render)}</ul>}
    </div>
  )
}

// tasks_agent2.md Phase 5 "Unit detail" — GET /api/agent2/plans/{plan_id}/units/
// {cluster_id} (design_agent2.md §11). Members render as plain rows: Agent 1's entity
// detail view isn't built yet (tasks.md Phase 4), so there's nothing to link to.
// `unitLabel` names other units (edge ends, callers) from the plan overview.
function PlanUnitDetail({ planId, clusterId, unitLabel, onClose }) {
  const [unit, setUnit] = useState(null)
  const [error, setError] = useState(null)
  const panelRef = useRef(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    setUnit(null)
    setError(null)
    apiFetch(`/api/agent2/plans/${planId}/units/${clusterId}`)
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        return response.json()
      })
      .then((data) => { if (!cancelled) setUnit(data) })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [planId, clusterId])

  // Once loaded, bring the panel into view if it's off-screen: below the phase list on
  // narrow screens, or above the viewport after scrolling down a long list.
  useEffect(() => {
    const panel = panelRef.current
    if (!unit || !panel) return
    const { top } = panel.getBoundingClientRect()
    if (top < 0 || top > window.innerHeight * 0.6) panel.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [unit])

  const contract = unit?.test_contract || {}
  const unverifiedIds = new Set(contract.unverified_member_ids || [])
  const memberName = (entityId) => unit?.members.find((m) => m.id === entityId)?.name || entityId
  const labelOf = (id) => unit?.constituent_reports.find((r) => r.cluster_id === id)?.label || unitLabel(id)

  return (
    <section className="plan-unit-detail" aria-label="Unit detail" ref={panelRef}>
      <div className="plan-unit-detail-head">
        <p className="eyebrow">UNIT</p>
        <button type="button" className="token-toggle plan-unit-close" onClick={onClose}>Close ✕</button>
      </div>
      {error && <p className="error">{error}</p>}
      {!error && !unit && <p className="summary-loading">Loading unit…</p>}
      {unit && (
        <>
          <h3 className="plan-unit-detail-title">
            <span className="plan-unit-detail-order">{unit.phase_order}.{unit.unit_order}</span>
            {unit.label || <em>Unlabeled unit</em>}
          </h3>
          <div className="summary-badges">
            {unit.risk_level && <span className={`risk-level risk-level-${unit.risk_level}`}>{unit.risk_level} risk</span>}
            <ConfidenceBadge confidence={unit.grounding_confidence} />
            {unit.flags.map((flag) => (
              <span key={flag} className={`plan-flag${flag === 'needs_facade' ? ' plan-flag-warn' : ''}`}>
                {flag.replace('_', ' ')}
              </span>
            ))}
            <span className="plan-unit-size">{unit.size ?? '—'} {unit.size === 1 ? 'entity' : 'entities'}</span>
          </div>

          <h4>Report</h4>
          {unit.report ? (
            <>
              <div className="summary-badges">
                {unit.report.grounding_status === 'unverified' && <UnverifiedBadge count={1} />}
                <ConfidenceBadge confidence={unit.report.grounding_confidence} />
              </div>
              <p className="plan-phase-summary">{unit.report.summary_text || 'The report has no summary text.'}</p>
              <GroundingIssuesList issues={unit.report.grounding_issues} />
            </>
          ) : unit.constituent_reports.length > 0 ? (
            <>
              <p className="summary-loading">
                This unit was merged from {unit.constituent_reports.length} units, so it has their reports instead of one of its own.
              </p>
              <ul className="plan-constituent-reports">
                {unit.constituent_reports.map((report) => (
                  <li key={report.cluster_id}>
                    <div className="summary-badges">
                      <strong>{report.label || unitLabel(report.cluster_id)}</strong>
                      {report.risk_level && <span className={`risk-level risk-level-${report.risk_level}`}>{report.risk_level}</span>}
                      {report.grounding_status === 'unverified' && <span className="unverified-badge unverified-badge-warn">unverified</span>}
                      <ConfidenceBadge confidence={report.grounding_confidence} />
                    </div>
                    <p className="plan-phase-summary">{report.summary_text || 'No report is available for this unit.'}</p>
                    <GroundingIssuesList issues={report.grounding_issues} />
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="summary-loading">No report is available for this unit.</p>
          )}

          <h4>Members</h4>
          {unit.members.length === 0 ? (
            <p className="entity-table-empty">This unit has no members.</p>
          ) : (
            <div className="plan-table-scroll">
              <table className="entity-table">
                <thead>
                  <tr><th>Type</th><th>Name</th><th>File</th><th>Grounding</th></tr>
                </thead>
                <tbody>
                  {unit.members.map((member) => (
                    <tr key={member.id} className={unverifiedIds.has(member.id) ? 'plan-member-unverified' : undefined}>
                      <td>{member.entity_type}</td>
                      <td>{member.name}</td>
                      <td className="entity-file-path">{member.file_path}</td>
                      <td>{member.grounding_status === 'unverified' ? <span className="unverified-badge unverified-badge-warn">unverified</span> : (member.grounding_status || '—')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h4>Test contract</h4>
          <div className="plan-contract">
            <PlanContractSection
              title="Public interface"
              items={contract.public_interface || []}
              render={(entry) => (
                <li key={entry.entity_id}>
                  <code>{entry.class ? `${entry.class}::` : ''}{entry.name}({(entry.params || []).join(', ')})</code>
                  <span className="plan-flag">{entry.kind}</span>
                  <span className="entity-file-path">{entry.file_path}</span>
                </li>
              )}
            />
            <PlanContractSection
              title="Incoming callers"
              items={contract.incoming_call_sites || []}
              render={(site) => (
                <li key={`${site.from_entity_id}-${site.to_entity_id}`}>
                  {labelOf(site.from_unit_id)} → <code>{memberName(site.to_entity_id)}</code>
                  <span className="plan-unit-size">×{site.call_count}</span>
                </li>
              )}
            />
            <PlanContractSection
              title="Outgoing dependencies"
              items={contract.outgoing_dependencies || []}
              render={(dep) => (
                <li key={dep.to_unit_id}>
                  {labelOf(dep.to_unit_id)}
                  <span className="plan-unit-size">
                    {dep.call_count} {dep.call_count === 1 ? 'call' : 'calls'}
                    {dep.hook_binding_count > 0 && `, ${dep.hook_binding_count} hook/route ${dep.hook_binding_count === 1 ? 'binding' : 'bindings'}`}
                  </span>
                </li>
              )}
            />
            <PlanContractSection
              title="External calls"
              items={contract.external_calls || []}
              render={(name) => <li key={name}><code>{name}</code></li>}
            />
            <PlanContractSection
              title="Hooks bound"
              items={contract.hooks_bound || []}
              render={(name) => <li key={name}><code>{name}</code></li>}
            />
            <PlanContractSection
              title="Routes bound"
              items={contract.routes_bound || []}
              render={(name) => <li key={name}><code>{name}</code></li>}
            />
            <PlanContractSection
              title="Side-effect signals"
              items={contract.side_effect_signals || []}
              render={(signal) => (
                <li key={signal.entity_id}>
                  <code>{memberName(signal.entity_id)}</code>
                  <span className={`risk-level risk-level-${signal.risk_level}`}>{signal.risk_level}</span>
                  <span className="plan-contract-excerpt">{signal.summary_excerpt}</span>
                </li>
              )}
            />
            <div className="plan-contract-section">
              <h4>Unverified members</h4>
              {unverifiedIds.size === 0 ? (
                <p className="summary-loading">None.</p>
              ) : (
                <p className="plan-contract-unverified">
                  {unverifiedIds.size === 1
                    ? '1 member has an unverified summary'
                    : `${unverifiedIds.size} members have unverified summaries`}{' '}
                  (highlighted in Members). Don't trust {unverifiedIds.size === 1 ? 'it' : 'them'} when writing tests.
                </p>
              )}
            </div>
          </div>

          <h4>Dependencies</h4>
          {unit.edges.length === 0 ? (
            <p className="entity-table-empty">This unit has no dependencies on other units.</p>
          ) : (
            <div className="plan-table-scroll">
              <table className="entity-table">
                <thead>
                  <tr><th>Direction</th><th>Unit</th><th>Calls</th><th>Hook bindings</th><th>Flag</th></tr>
                </thead>
                <tbody>
                  {unit.edges.map((edge) => {
                    const other = edge.direction === 'incoming' ? edge.from_cluster_id : edge.to_cluster_id
                    return (
                      <tr key={edge.id} className={edge.flag === 'needs_facade' ? 'plan-edge-facade' : undefined}>
                        <td>{EDGE_DIRECTION_LABELS[edge.direction] || edge.direction}</td>
                        <td>
                          {edge.direction === 'internal'
                            ? `${labelOf(edge.from_cluster_id)} → ${labelOf(edge.to_cluster_id)}`
                            : labelOf(other)}
                        </td>
                        <td>{edge.call_count}</td>
                        <td>{edge.hook_binding_count}</td>
                        <td>
                          {edge.flag === 'none' ? '—' : (
                            <span className={`plan-flag${edge.flag === 'needs_facade' ? ' plan-flag-warn' : ''}`}>
                              {edge.flag === 'needs_facade' ? '⚠ needs facade' : edge.flag}
                            </span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  )
}

// Same legibility cap as the live entity tree; nodes past it are the latest in
// migration order, and the graph says how many it left out.
const PLAN_GRAPH_NODE_CAP = LIVE_TREE_NODE_CAP
const PLAN_GRAPH_COLUMN_WIDTH = 250
const PLAN_GRAPH_ROW_HEIGHT = 64
const PLAN_GRAPH_FACADE_COLOR = '#ff5c4d'
const PLAN_GRAPH_EDGE_COLOR = 'var(--graph-edge)'

function PlanGraphNode({ data }) {
  const style = RISK_NODE_STYLE[data.riskLevel] || DEFAULT_RISK_NODE_STYLE
  return (
    <div
      className={`plan-graph-node${data.selected ? ' plan-graph-node-selected' : ''}`}
      style={{ borderColor: style.border, background: style.background }}
      title={data.label}
    >
      {/* Arrows run dependent → dependency, i.e. right to left. A cycle's back edge
          (dependency to the right) arcs over the top instead of crossing the nodes. */}
      <Handle type="target" position={Position.Right} id="in" />
      <Handle type="target" position={Position.Top} id="back-in" />
      <span className="plan-graph-node-label" style={{ color: style.color }}>{data.label}</span>
      <span className="plan-graph-node-size">{data.size ?? '—'}</span>
      <Handle type="source" position={Position.Left} id="out" />
      <Handle type="source" position={Position.Top} id="back-out" />
    </div>
  )
}

// Module scope for the same reason as LIVE_TREE_NODE_TYPES.
const PLAN_GRAPH_NODE_TYPES = { plan: PlanGraphNode }

// Pure layout: a node's column is one past its deepest dependency, so dependencies sit
// left of their dependents and left-to-right reads as migration order. Nodes arrive in
// plan order (dependencies first), so a dependency not placed yet can only be the other
// end of a cycle, and is ignored for layering.
function buildPlanGraph(graph, { labelFor, selectedId }) {
  const shown = graph.nodes.slice(0, PLAN_GRAPH_NODE_CAP)
  const shownIds = new Set(shown.map((node) => node.id))
  const edges = graph.edges.filter((edge) => shownIds.has(edge.from) && shownIds.has(edge.to))

  const dependencies = new Map()
  edges.forEach((edge) => {
    if (!dependencies.has(edge.from)) dependencies.set(edge.from, [])
    dependencies.get(edge.from).push(edge.to)
  })
  const column = new Map()
  const rowsInColumn = []
  const nodes = shown.map((node, index) => {
    const placed = (dependencies.get(node.id) || []).filter((id) => column.has(id)).map((id) => column.get(id))
    const col = placed.length ? Math.max(...placed) + 1 : 0
    column.set(node.id, col)
    const row = rowsInColumn[col] || 0
    rowsInColumn[col] = row + 1
    return {
      id: node.id,
      type: 'plan',
      position: { x: col * PLAN_GRAPH_COLUMN_WIDTH, y: row * PLAN_GRAPH_ROW_HEIGHT },
      data: { label: labelFor(node, index), size: node.size, riskLevel: node.risk_level, selected: node.id === selectedId },
      draggable: false,
      selectable: false,
    }
  })

  const flowEdges = edges.map((edge) => {
    const color = edge.flag === 'needs_facade' ? PLAN_GRAPH_FACADE_COLOR : PLAN_GRAPH_EDGE_COLOR
    const backEdge = column.get(edge.to) >= column.get(edge.from)
    return {
      id: `${edge.from}->${edge.to}`,
      source: edge.from,
      target: edge.to,
      sourceHandle: backEdge ? 'back-out' : 'out',
      targetHandle: backEdge ? 'back-in' : 'in',
      ...(backEdge ? { type: 'smoothstep', pathOptions: { offset: 24 } } : {}),
      markerEnd: { type: MarkerType.ArrowClosed, color },
      style: {
        stroke: color,
        strokeWidth: edge.flag === 'needs_facade' ? 2 : 1.5,
        ...(edge.flag === 'merged' ? { strokeDasharray: '6 4' } : {}),
      },
    }
  })

  return { nodes, edges: flowEdges, total: graph.nodes.length }
}

// tasks_agent2.md Phase 5 "Plan graph" — GET /api/agent2/plans/{plan_id}/graph
// (design_agent2.md §11). Phase level first; clicking a phase drills into its units
// (`&phase_id=`), and clicking a unit opens PlanUnitDetail (`&unit=`). Labels come
// from the plan overview, so they match the list view.
function PlanGraph({ planId, phases, phaseId, selectedUnitId, onSelectPhase, onSelectUnit }) {
  const [graph, setGraph] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    setGraph(null)
    setError(null)
    const query = phaseId ? `level=unit&phase_id=${encodeURIComponent(phaseId)}` : 'level=phase'
    apiFetch(`/api/agent2/plans/${planId}/graph?${query}`)
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        return response.json()
      })
      .then((data) => { if (!cancelled) setGraph(data) })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [planId, phaseId])

  const phase = phases.find((p) => p.phase_cluster_id === phaseId)
  const phaseTitle = (p) => `Phase ${p.phase_order}${p.label ? ` · ${p.label}` : ''}`
  const noun = phaseId ? 'units' : 'phases'

  const flow = useMemo(() => {
    if (!graph) return null
    const labelFor = phaseId
      ? (node) => {
          const unit = phase?.units.find((u) => u.cluster_id === node.id)
          return unit ? `${phase.phase_order}.${unit.unit_order} ${unit.label || 'Unlabeled unit'}` : (node.label || 'Unlabeled unit')
        }
      : (node) => {
          const p = phases.find((candidate) => candidate.phase_cluster_id === node.id)
          return p ? phaseTitle(p) : (node.label || 'Unlabeled phase')
        }
    return buildPlanGraph(graph, { labelFor, selectedId: phaseId ? selectedUnitId : null })
  }, [graph, phases, phaseId, selectedUnitId])

  return (
    <div className="plan-graph">
      <nav className="plan-graph-breadcrumb" aria-label="Graph level">
        {phaseId ? (
          <>
            <button type="button" className="project-link" onClick={() => onSelectPhase(null)}>All phases</button>
            <span aria-hidden="true">›</span>
            <span aria-current="page">{phase ? phaseTitle(phase) : 'Phase'}</span>
          </>
        ) : (
          <span aria-current="page">All phases</span>
        )}
      </nav>
      {error && <p className="error">{error}</p>}
      {!error && !flow && <p className="summary-loading">Loading graph…</p>}
      {flow && flow.nodes.length === 0 && <p className="summary-loading">There are no {noun} to draw.</p>}
      {flow && flow.nodes.length > 0 && (
        <>
          {flow.total > flow.nodes.length && (
            <p className="plan-graph-cap-note">
              Showing {flow.nodes.length} of {flow.total} {noun}, the earliest in migration order.
            </p>
          )}
          <div className="plan-graph-canvas">
            <ReactFlow
              key={phaseId || 'phases'}
              nodes={flow.nodes}
              edges={flow.edges}
              nodeTypes={PLAN_GRAPH_NODE_TYPES}
              onNodeClick={(_event, node) => (phaseId ? onSelectUnit(node.id) : onSelectPhase(node.id))}
              fitView
              fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={false}
              proOptions={{ hideAttribution: true }}
            >
              <Background gap={16} color="rgba(var(--neutral-rgb),.06)" />
            </ReactFlow>
          </div>
          <p className="plan-graph-legend">
            Left to right is migration order, and arrows point at what each {phaseId ? 'unit' : 'phase'} depends on.
            {' '}<span className="plan-graph-legend-facade">Red</span>: cycle that needs a facade.
            {' '}<span className="plan-graph-legend-merged">Dashed</span>: merged.
            {' '}Click a {phaseId ? 'unit to see its detail' : 'phase to see its units'}.
          </p>
        </>
      )}
    </div>
  )
}

// tasks_agent2.md Phase 5 "Submit" — POST /api/agent2/plans/{plan_id}/submit
// (design_agent2.md §11), `draft → in_review`. PlanPage only renders this for a draft.
// A 409 (stale, or the plan moved on since it loaded) shows the backend's message.
function SubmitPlanAction({ planId, onSubmitted }) {
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const apiFetch = useApiFetch()

  const submit = async (event) => {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const response = await apiFetch(`/api/agent2/plans/${planId}/submit`, { method: 'POST' })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(formatJobErrorMessage(response.status, body))
      }
      await onSubmitted()
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="reingest-action plan-submit-action" onSubmit={submit} aria-label="Submit plan for review">
      <h2>Review</h2>
      <p className="reingest-hint">
        This plan is a draft. Submitting it opens each phase for approval; overrides still make a new version.
      </p>
      <PrimaryButton label="Submit for review" type="submit" loading={submitting} style={{ marginTop: 14 }} />
      {error && <p className="error">{error}</p>}
    </form>
  )
}

// tasks_agent3.md Phase 1 "Generate code" — POST /api/agent3/plans/{plan_id}/executions
// (design_agent3.md §9). Only an approved, non-stale plan can start code generation; for
// any other plan the button stays disabled and the hint says why. The backend re-checks
// every gate (including the source snapshot and an already-running job), and its 409
// is shown verbatim.
function generateCodeBlocker(plan) {
  if (plan.is_stale || plan.status === 'stale') {
    return 'This plan is stale: it was built on an older ingestion run. Generate a new plan from the project page.'
  }
  if (plan.status === 'rejected') return 'This plan was rejected. Generate a new plan, or open another version.'
  if (plan.status === 'draft') return 'Approve every phase first: submit this draft for review, then approve each phase.'
  if (plan.status === 'in_review') return 'Approve every phase first. Code can only be generated from an approved plan.'
  return null
}

function GenerateCodeAction({ plan, onExecutionCreated }) {
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const apiFetch = useApiFetch()
  const blocker = generateCodeBlocker(plan)

  const generate = async (event) => {
    event.preventDefault()
    if (blocker) return
    setError(null)
    setSubmitting(true)
    try {
      const response = await apiFetch(`/api/agent3/plans/${plan.id}/executions`, { method: 'POST' })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(formatJobErrorMessage(response.status, body))
      }
      const data = await response.json()
      onExecutionCreated(data.execution_job_id)
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="reingest-action plan-submit-action" onSubmit={generate} aria-label="Generate code from this plan">
      <h2>Code generation</h2>
      <p className="reingest-hint">
        {blocker || 'Generates FastAPI code for every unit in plan order, and tests each unit in an isolated sandbox.'}
      </p>
      <PrimaryButton
        label="Generate code"
        type="submit"
        disabled={Boolean(blocker)}
        loading={submitting}
        style={{ marginTop: 14 }}
      />
      {error && <p className="error" role="alert">{error}</p>}
    </form>
  )
}

// tasks_agent3.md Phase 1 "Execution list" — GET /api/agent3/plans/{plan_id}/executions
// (design_agent3.md §9): every code generation run of this plan, newest first, each
// linking to its status page. `counts` is live, so a running job shows its progress.
function ExecutionRunsList({ planId, onOpenExecution }) {
  const [runs, setRuns] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    setRuns(null)
    setError(null)
    apiFetch(`/api/agent3/plans/${planId}/executions`)
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        return response.json()
      })
      .then((data) => { if (!cancelled) setRuns(data.executions) })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [planId])

  return (
    <section className="job-history" aria-label="Code generation runs">
      <h2>Code generation runs</h2>
      {error && <p className="error">{error}</p>}
      {!error && !runs && <p className="summary-loading">Loading code generation runs…</p>}
      {runs && runs.length === 0 && <p className="summary-loading">No code generation runs yet.</p>}
      {runs && runs.length > 0 && (
        <ul className="projects-list">
          {runs.map((run) => (
            <li key={run.id}>
              <button type="button" className="project-link" onClick={() => onOpenExecution(run.id)}>
                {new Date(run.created_at).toLocaleString()}
                <span className="run-diff-hint">
                  {' · '}{run.counts.done} of {run.counts.total} {run.counts.total === 1 ? 'unit' : 'units'} done
                  {run.counts.done > 0 && ` (${run.counts.passed} passed)`}
                </span>
              </button>
              <span className="plan-row-badges">
                {run.status === 'running' && run.progress_stage && (
                  <span className="run-diff-hint">{run.progress_stage.replace('_', ' ')}</span>
                )}
                <span className="project-status">{run.status}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

// tasks_agent2.md Phase 5 "Approval" — POST /api/agent2/plans/{plan_id}/phases/
// {phase_cluster_id}/approval (design_agent2.md §11). PlanPage only renders this while the
// plan is `in_review`. A repeat decision replaces the earlier one, so the controls stay up
// after a vote until the plan leaves review. The note starts from the saved decision's note.
function PhaseApprovalControls({ planId, phase, onDecided }) {
  const [note, setNote] = useState(phase.approval?.note || '')
  const [pending, setPending] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()
  const noteId = `plan-phase-note-${phase.phase_cluster_id}`

  const decide = async (status) => {
    setError(null)
    setPending(status)
    try {
      const response = await apiFetch(
        `/api/agent2/plans/${planId}/phases/${phase.phase_cluster_id}/approval`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status, note: note.trim() || null }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(formatJobErrorMessage(response.status, body))
      }
      await onDecided()
    } catch (err) {
      setError(err.message)
    } finally {
      setPending(null)
    }
  }

  const current = phase.approval?.status
  return (
    <div className="plan-phase-approval" role="group" aria-label={`Review phase ${phase.phase_order}`}>
      <label className="plan-phase-note-label" htmlFor={noteId}>Note (optional)</label>
      <textarea
        id={noteId}
        className="plan-phase-note"
        rows={2}
        value={note}
        onChange={(event) => setNote(event.target.value)}
        placeholder="Why you're approving or rejecting this phase"
      />
      <div className="plan-phase-approval-buttons">
        {['approved', 'rejected'].map((status) => (
          <button
            key={status}
            type="button"
            className={`plan-phase-decide plan-phase-decide-${status}`}
            disabled={pending !== null}
            aria-pressed={current === status}
            onClick={() => decide(status)}
          >
            {pending === status ? 'Saving…' : status === 'approved' ? 'Approve' : 'Reject'}
          </button>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  )
}

// tasks_agent2.md Phase 5 "Approval" — the persistent notice while `open_facade_flags > 0`
// (design_agent2.md §7, §9.1): the plan can't reach `approved` until every flagged cycle has a
// `facade_ack` override. It lists each open cycle from the overview's `facade_cycles`, linking
// a unit-level cycle's units. While the plan takes overrides ("Overrides" in tasks_agent2.md),
// each cycle also gets a required note and an "Acknowledge" button.
function PlanFacadeNotice({ plan, editable, pending, onSelectFlaggedUnit, onAcknowledge }) {
  const count = plan.open_facade_flags
  const unitsById = new Map(plan.phases.flatMap((phase) => phase.units.map((unit) => [unit.cluster_id, { phase, unit }])))
  const phasesById = new Map(plan.phases.map((phase) => [phase.phase_cluster_id, phase]))
  const cycles = plan.facade_cycles || []
  return (
    <div className="plan-facade-notice" role="note" aria-label="Open facade flags">
      <p>
        <strong>⚠ {count} open facade {count === 1 ? 'flag' : 'flags'}.</strong> Approval can't complete until each
        flagged dependency cycle is acknowledged or overridden, even with every phase approved.
      </p>
      {cycles.length > 0 ? (
        <ul className="plan-facade-cycles" aria-label="Flagged dependency cycles">
          {cycles.map((cycle) => (
            <PlanFacadeCycle
              key={`${cycle.level}:${cycle.scc_id}`}
              cycle={cycle}
              unitsById={unitsById}
              phasesById={phasesById}
              editable={editable}
              pending={pending}
              onSelectFlaggedUnit={onSelectFlaggedUnit}
              onAcknowledge={onAcknowledge}
            />
          ))}
        </ul>
      ) : (
        <p className="plan-facade-hint">The flagged cycles run between phases; the Graph view shows them in the error color.</p>
      )}
    </div>
  )
}

function PlanFacadeCycle({ cycle, unitsById, phasesById, editable, pending, onSelectFlaggedUnit, onAcknowledge }) {
  const [note, setNote] = useState('')
  const [error, setError] = useState(null)
  const noteId = `plan-facade-note-${cycle.level}-${cycle.scc_id}`
  const canAcknowledge = editable && cycle.scc_id !== null

  const acknowledge = async (event) => {
    event.preventDefault()
    setError(null)
    try {
      await onAcknowledge({ level: cycle.level, scc_id: cycle.scc_id, note: note.trim() })
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <li className="plan-facade-cycle">
      <span className="plan-facade-cycle-level">{cycle.level === 'unit' ? 'Units' : 'Phases'}</span>
      <span className="plan-facade-units">
        {cycle.cluster_ids.map((id, index) => {
          const found = unitsById.get(id)
          const phase = phasesById.get(id)
          return (
            <span key={id}>
              {index > 0 && <span className="plan-facade-arrow" aria-hidden="true"> ↔ </span>}
              {found ? (
                <button type="button" className="project-link" onClick={() => onSelectFlaggedUnit(found.phase, found.unit)}>
                  {found.unit.label || `Unit ${found.phase.phase_order}.${found.unit.unit_order}`}
                </button>
              ) : (
                <span>{phase ? `Phase ${phase.phase_order}${phase.label ? ` · ${phase.label}` : ''}` : id.slice(0, 8)}</span>
              )}
            </span>
          )
        })}
      </span>
      {canAcknowledge && (
        <form className="plan-facade-ack" onSubmit={acknowledge}>
          <label className="plan-phase-note-label" htmlFor={noteId}>Facade note (required)</label>
          <input
            id={noteId}
            className="plan-phase-note"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="How the cycle will be broken, e.g. an interface both sides call"
            required
          />
          <button type="submit" className="plan-phase-decide" disabled={pending || !note.trim()}>
            Acknowledge
          </button>
          {error && <p className="error">{error}</p>}
        </form>
      )}
      {editable && cycle.scc_id === null && (
        <p className="plan-facade-hint">This cycle has no id to acknowledge it by; merge its units instead.</p>
      )}
    </li>
  )
}

const mergeCount = (phase, selection) => phase.units.filter((u) => selection.has(u.cluster_id)).length

// tasks_agent2.md Phase 5 "Overrides" — POST /api/agent2/plans/{plan_id}/overrides
// (design_agent2.md §9.2, §11). Every override writes a new version, so on success the
// page moves to it. The backend's 422 message (e.g. a reorder that would put a unit before
// its dependency) is thrown for the calling control to show inline.
function useCreateOverride(planId, onCreated) {
  const apiFetch = useApiFetch()
  const [pending, setPending] = useState(false)
  const create = async (kind, payload) => {
    setPending(true)
    try {
      const response = await apiFetch(`/api/agent2/plans/${planId}/overrides`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, payload }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(formatJobErrorMessage(response.status, body))
      onCreated(body.plan_id)
    } finally {
      setPending(false)
    }
  }
  return [create, pending]
}

// tasks_agent2.md Phase 5 "Plan overview" — GET /api/agent2/plans/{plan_id}
// (design_agent2.md §11). The backend runs the §9.1 stale check on read, so `status`
// is already `stale` where it applies; `is_stale` drives the banner the same way it
// drives PlansList's badge. A unit click sets `&unit=`, which opens PlanUnitDetail
// beside the phases (below them on narrow screens). The phases show as a list or as
// PlanGraph; a graph drilled into one phase (`&phase_id=`) reopens as the graph.
// Every write control is gated on a status (submit on `draft`, approval on `in_review`),
// so an `approved` plan shows only its "Approved by … at …" state.
function PlanPage({
  planId, selectedUnitId, phaseId, onSelectUnit, onCloseUnit, onSelectPhase, onBackToProject, onBackToForm, onOpenPlan,
  onExecutionCreated, onOpenExecution,
}) {
  const [plan, setPlan] = useState(null)
  const [error, setError] = useState(null)
  const [expanded, setExpanded] = useState(() => new Set())
  const [view, setView] = useState(phaseId ? 'graph' : 'list')
  // Override controls: units ticked for a merge, and each phase's last override error.
  const [mergeSelection, setMergeSelection] = useState(() => new Set())
  const [overrideErrors, setOverrideErrors] = useState({})
  const [createOverride, overridePending] = useCreateOverride(planId, onOpenPlan)
  const apiFetch = useApiFetch()
  const { session } = useAuth()

  const fetchPlan = async () => {
    const response = await apiFetch(`/api/agent2/plans/${planId}`)
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      throw new Error(formatJobErrorMessage(response.status, body))
    }
    return response.json()
  }
  // After a write (e.g. submit): swap in the fresh plan without the loading state, so
  // open phases and the selected unit stay as they were.
  const refreshPlan = async () => setPlan(await fetchPlan())

  useEffect(() => {
    let cancelled = false
    setPlan(null)
    setError(null)
    setMergeSelection(new Set())
    setOverrideErrors({})
    fetchPlan()
      .then((data) => {
        if (cancelled) return
        setPlan(data)
        // Open the phase holding a unit selected via the URL, so a refresh keeps it visible.
        const selectedPhase = data.phases.find((phase) => phase.units.some((u) => u.cluster_id === selectedUnitId))
        setExpanded(new Set(selectedPhase ? [selectedPhase.phase_cluster_id] : []))
      })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [planId])

  const togglePhase = (phaseId) => {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(phaseId)) next.delete(phaseId)
      else next.add(phaseId)
      return next
    })
  }

  const unitLabels = useMemo(() => {
    const labels = new Map()
    plan?.phases.forEach((phase) => phase.units.forEach((unit) => {
      labels.set(unit.cluster_id, unit.label || `Unit ${phase.phase_order}.${unit.unit_order}`)
    }))
    return labels
  }, [plan])
  const unitLabel = (clusterId) => unitLabels.get(clusterId) || `Unit ${clusterId.slice(0, 8)}`

  // A notice link opens the unit's panel and, in the list, its phase, so the row is visible too.
  const selectFlaggedUnit = (phase, unit) => {
    setExpanded((current) => new Set(current).add(phase.phase_cluster_id))
    onSelectUnit(unit.cluster_id)
  }

  // design_agent2.md §9.2: overrides are only taken on a draft or in-review, non-stale plan.
  const editable = Boolean(plan) && !plan.is_stale && ['draft', 'in_review'].includes(plan.status)
  const phaseOverride = async (phaseId, kind, payload) => {
    setOverrideErrors((current) => ({ ...current, [phaseId]: null }))
    try {
      await createOverride(kind, payload)
    } catch (err) {
      setOverrideErrors((current) => ({ ...current, [phaseId]: err.message }))
    }
  }
  const toggleMergeUnit = (clusterId) => {
    setMergeSelection((current) => {
      const next = new Set(current)
      if (next.has(clusterId)) next.delete(clusterId)
      else next.add(clusterId)
      return next
    })
  }
  const mergeSelected = (phase) => phaseOverride(phase.phase_cluster_id, 'merge', {
    cluster_ids: phase.units.filter((u) => mergeSelection.has(u.cluster_id)).map((u) => u.cluster_id),
  })
  const moveUnit = (phase, unit, delta) => phaseOverride(phase.phase_cluster_id, 'reorder', {
    cluster_id: unit.cluster_id,
    unit_order: unit.unit_order + delta,
  })

  const showView = (next) => {
    setView(next)
    if (next === 'list' && phaseId) onSelectPhase(null) // the drill-down only exists in the graph
  }

  return (
    <main className="page-shell">

      <section className="hero project-detail" aria-label="Migration plan">
        <button
          type="button"
          className="token-toggle back-link"
          onClick={() => (plan ? onBackToProject(plan.project_id) : onBackToForm())}
        >
          <span>‹</span> {plan ? 'Back to project' : 'Back to repositories'}
        </button>
        {error && <p className="error">{error}</p>}
        {!error && !plan && <p className="summary-loading">Loading plan…</p>}
        {plan && (
          <>
            <p className="eyebrow">MIGRATION PLAN</p>
            <h1 className="project-detail-title">Version {plan.version}</h1>
            {plan.parent_plan_id && (
              // An override made this version from an earlier one (design_agent2.md §9.2); the
              // project page's Plans list has every version.
              <p className="plan-version-note">
                Version {plan.version} (from {plan.parent_version ?? '?'})
                {' · '}
                <button type="button" className="project-link" onClick={() => onOpenPlan(plan.parent_plan_id)}>
                  Open version {plan.parent_version ?? 'it was made from'}
                </button>
                {' · '}
                <button type="button" className="project-link" onClick={() => onBackToProject(plan.project_id)}>
                  All versions
                </button>
              </p>
            )}
            <div className="summary-badges plan-header-badges">
              <span className={`plan-status plan-status-${plan.status}`}>{plan.status.replace('_', ' ')}</span>
              <span className={`plan-flag${plan.open_facade_flags > 0 ? ' plan-flag-warn' : ''}`}>
                {plan.open_facade_flags} open facade {plan.open_facade_flags === 1 ? 'flag' : 'flags'}
              </span>
              {/* Clustering-quality metrics (design_agent2.md §2/§13) — null for a plan
                  whose planning job predates these fields. single_unit_phases > 1 is the
                  forward-looking detector for the 46-single-unit-phase degeneracy §13
                  documents, which used to only be caught by a manual dry-run inspection. */}
              {plan.modularity != null && (
                <span className="plan-flag" title="Leiden modularity of the final unit-level clustering">
                  modularity {plan.modularity.toFixed(2)}
                </span>
              )}
              {plan.phase_size_stats && plan.phase_size_stats.single_unit_phases > 1 && (
                <span className="plan-flag plan-flag-warn" title="Phases with only one unit each often mean the phase graph was too sparse to cluster meaningfully">
                  {plan.phase_size_stats.single_unit_phases} single-unit phases
                </span>
              )}
            </div>
            {plan.is_stale && (
              <p className="plan-stale-banner" role="status">
                This plan was built on an older ingestion run. It can't be approved; generate a new plan from the
                project page.
              </p>
            )}
            {plan.status === 'approved' && (
              <p className="plan-approved-state" role="status">
                <strong>✓ Approved</strong>
                {' by '}
                {plan.approved_by === session?.user?.id
                  ? 'you'
                  : plan.approved_by ? <code title={plan.approved_by}>{plan.approved_by.slice(0, 8)}</code> : 'an unknown reviewer'}
                {plan.approved_at && <> at {new Date(plan.approved_at).toLocaleString()}</>}
              </p>
            )}
            {plan.open_facade_flags > 0 && (
              <PlanFacadeNotice
                plan={plan}
                editable={editable}
                pending={overridePending}
                onSelectFlaggedUnit={selectFlaggedUnit}
                onAcknowledge={(payload) => createOverride('facade_ack', payload)}
              />
            )}
            {plan.status === 'draft' && <SubmitPlanAction planId={planId} onSubmitted={refreshPlan} />}
            <GenerateCodeAction plan={plan} onExecutionCreated={onExecutionCreated} />
          </>
        )}
      </section>
      {plan && (plan.status === 'approved' || plan.is_stale) && (
        // An approved plan that later went stale keeps its earlier runs (design_agent3.md §13).
        <ExecutionRunsList planId={planId} onOpenExecution={onOpenExecution} />
      )}
      {plan && (
        <div className={selectedUnitId ? 'plan-layout plan-layout-with-detail' : 'plan-layout'}>
          <section className="job-history" aria-label="Phases">
            <div className="plan-phases-head">
              <h2>Phases</h2>
              {plan.phases.length > 0 && (
                <div className="plan-view-toggle" role="group" aria-label="Phases view">
                  {['list', 'graph'].map((option) => (
                    <button key={option} type="button" aria-pressed={view === option} onClick={() => showView(option)}>
                      {option === 'list' ? 'List' : 'Graph'}
                    </button>
                  ))}
                </div>
              )}
            </div>
            {plan.phases.length === 0 ? (
              <p className="summary-loading">This plan has no phases.</p>
            ) : view === 'graph' ? (
              <PlanGraph
                planId={planId}
                phases={plan.phases}
                phaseId={phaseId}
                selectedUnitId={selectedUnitId}
                onSelectPhase={onSelectPhase}
                onSelectUnit={onSelectUnit}
              />
            ) : (
              <ol className="plan-phases">
                {plan.phases.map((phase) => {
                  const isOpen = expanded.has(phase.phase_cluster_id)
                  const unitsId = `plan-phase-units-${phase.phase_cluster_id}`
                  return (
                    <li key={phase.phase_cluster_id} className="plan-phase">
                      <button
                        type="button"
                        className="plan-phase-header"
                        aria-expanded={isOpen}
                        aria-controls={unitsId}
                        onClick={() => togglePhase(phase.phase_cluster_id)}
                      >
                        <span className="plan-phase-chevron" aria-hidden="true">{isOpen ? '⌄' : '›'}</span>
                        <span className="plan-phase-title">
                          Phase {phase.phase_order}{phase.label ? ` · ${phase.label}` : ''}
                        </span>
                        <span className="plan-phase-count">
                          {phase.units.length} {phase.units.length === 1 ? 'unit' : 'units'}
                        </span>
                      </button>
                      <div className="plan-phase-body">
                        <div className="summary-badges">
                          {phase.risk_level && (
                            <span className={`risk-level risk-level-${phase.risk_level}`}>{phase.risk_level} risk</span>
                          )}
                          {phase.grounding_status === 'unverified' && <UnverifiedBadge count={1} />}
                          <ConfidenceBadge confidence={phase.grounding_confidence} />
                          <PlanApprovalState approval={phase.approval} />
                        </div>
                        <p className="plan-phase-summary">{phase.summary_text || 'No report is available for this phase.'}</p>
                        {plan.status === 'in_review' && (
                          <PhaseApprovalControls planId={planId} phase={phase} onDecided={refreshPlan} />
                        )}
                        {isOpen && editable && phase.units.length > 1 && (
                          <div className="plan-merge-bar">
                            <button
                              type="button"
                              className="plan-phase-decide"
                              disabled={overridePending || mergeCount(phase, mergeSelection) < 2}
                              onClick={() => mergeSelected(phase)}
                            >
                              {overridePending ? 'Saving…' : `Merge ${mergeCount(phase, mergeSelection)} selected`}
                            </button>
                            <span className="plan-merge-hint">
                              Tick two or more units to merge them, or move one with ↑ ↓. Each change makes a new version.
                            </span>
                          </div>
                        )}
                        {isOpen && overrideErrors[phase.phase_cluster_id] && (
                          <p className="error" role="alert">{overrideErrors[phase.phase_cluster_id]}</p>
                        )}
                        {isOpen && (
                          <ol className="plan-units" id={unitsId}>
                            {phase.units.map((unit, index) => (
                              <PlanUnitRow
                                key={unit.cluster_id}
                                unit={unit}
                                selected={unit.cluster_id === selectedUnitId}
                                onSelect={onSelectUnit}
                                edit={editable && phase.units.length > 1 ? {
                                  checked: mergeSelection.has(unit.cluster_id),
                                  onToggle: toggleMergeUnit,
                                  onMove: (movedUnit, delta) => moveUnit(phase, movedUnit, delta),
                                  isFirst: index === 0,
                                  isLast: index === phase.units.length - 1,
                                  disabled: overridePending,
                                } : null}
                              />
                            ))}
                          </ol>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ol>
            )}
          </section>
          {selectedUnitId && (
            <PlanUnitDetail planId={planId} clusterId={selectedUnitId} unitLabel={unitLabel} onClose={onCloseUnit} />
          )}
        </div>
      )}
    </main>
  )
}

// tasks_agent3.md Phase 1 "Execution status" — GET /api/agent3/executions/{id}
// (design_agent3.md §9), polled until the job is done or failed. `counts` is computed
// live from the job's unit rows, so "units done" moves while stage `units` runs.
// `done` means every unit reached a verdict, not that every unit passed (specs FR1).
function ExecutionStatusPage({ executionJobId, selectedUnitId, onSelectUnit, onCloseUnit, onBackToPlan, onBackToForm }) {
  const [job, setJob] = useState(null)
  const [error, setError] = useState(null)
  // Not URL-synced: the "View results" button (tasks_agent3.md Phase 5, deferred
  // from Phase 1) toggles between this page's own status view and ExecutionResultsPage,
  // both still under the single ?execution=<id> page (frontend conventions, §12).
  // A deep link straight to a unit (?execution=&unit=) starts on results, not status.
  const [view, setView] = useState(selectedUnitId ? 'results' : 'status')
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    let timeoutId

    const poll = async () => {
      try {
        const response = await apiFetch(`/api/agent3/executions/${executionJobId}`)
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        const data = await response.json()
        if (cancelled) return
        setJob(data)
        if (data.status === 'queued' || data.status === 'running') {
          timeoutId = setTimeout(poll, JOB_POLL_INTERVAL_MS)
        }
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    }

    setJob(null)
    setError(null)
    poll()
    return () => { cancelled = true; clearTimeout(timeoutId) }
  }, [executionJobId])

  if (view === 'results' && job) {
    return (
      <ExecutionResultsPage
        executionJobId={executionJobId}
        job={job}
        selectedUnitId={selectedUnitId}
        onSelectUnit={onSelectUnit}
        onCloseUnit={onCloseUnit}
        onBack={() => { onCloseUnit(); setView('status') }}
        onBackToPlan={() => onBackToPlan(job.plan_id)}
      />
    )
  }

  const counts = job?.counts
  const failed = job?.status === 'failed'
  const done = job?.status === 'done'

  return (
    <main className="page-shell">
      <section className="hero job-status" aria-label="Code generation status">
        <button
          type="button"
          className="token-toggle back-link"
          onClick={() => (job ? onBackToPlan(job.plan_id) : onBackToForm())}
        >
          <span>‹</span> {job ? 'Back to plan' : 'Back to repositories'}
        </button>
        <p className="eyebrow">{job ? `CODE GENERATION · ${job.status.toUpperCase()}` : (error ? 'ERROR' : 'LOADING')}</p>
        <h1>Execution <code>{executionJobId}</code></h1>
        {error && <p className="error">{error}</p>}
        {!error && !job && <p className="summary-loading">Loading code generation…</p>}
        {job && (
          <>
            <ProgressStages
              stages={EXECUTION_STAGES}
              status={job.status}
              progressStage={job.progress_stage}
              label="Code generation progress"
            />
            {counts && counts.total > 0 && (
              <>
                <p className="execution-progress" role="status" aria-live="polite">
                  <strong>{counts.done}</strong> of <strong>{counts.total}</strong>{' '}
                  {counts.total === 1 ? 'unit' : 'units'} done
                </p>
                <div className="summary-badges" aria-label="Unit verdicts so far">
                  <span className="plan-flag">{counts.passed} passed</span>
                  <span className={`plan-flag${counts.failed_verification > 0 ? ' plan-flag-warn' : ''}`}>
                    {counts.failed_verification} failed verification
                  </span>
                  <span className={`plan-flag${counts.error > 0 ? ' plan-flag-warn' : ''}`}>{counts.error} error</span>
                  {counts.built_on_failed_dependency > 0 && (
                    <span className="plan-flag plan-flag-warn">
                      {counts.built_on_failed_dependency} built on a failed dependency
                    </span>
                  )}
                </div>
              </>
            )}
            {counts && counts.total === 0 && <p className="summary-loading">This execution has no units.</p>}
          </>
        )}
        {failed && (
          <p className="error job-failed-message">
            Code generation failed: {job.error || 'an unknown error occurred.'}
          </p>
        )}
        {done && (
          <p className="reingest-hint">
            Every unit has a verdict.{' '}
            {job.final_test_summary
              ? `Final test run: ${job.final_test_summary.passed} passed, ${job.final_test_summary.failed} failed.`
              : 'No final test run was recorded.'}
          </p>
        )}
        {(failed || done) && (
          <PrimaryButton label="Back to plan" type="button" onClick={() => onBackToPlan(job.plan_id)} style={{ marginTop: 8 }} />
        )}
        {done && (
          <PrimaryButton label="View results" type="button" onClick={() => setView('results')} style={{ marginTop: 8, marginLeft: 8 }} />
        )}
        {error && <PrimaryButton label="Back" type="button" onClick={onBackToForm} style={{ marginTop: 8 }} />}
      </section>
    </main>
  )
}

// tasks_agent3.md Phase 5 "Results" — GET /api/agent3/executions/{id}/units
// (design_agent3.md §9). Reached from ExecutionStatusPage's "View results" button
// on `done`, still under the same ?execution=<id> page (no separate URL param —
// frontend conventions, §12). `selectedUnitId` (`&unit=`, tasks_agent3.md Phase 5
// "Unit detail") swaps the whole page for ExecutionUnitPage rather than showing it
// inline, unlike PlanUnitDetail — legacy-vs-generated source panes need the width.
function ExecutionResultsPage({ executionJobId, job, selectedUnitId, onSelectUnit, onCloseUnit, onBack, onBackToPlan }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError(null)
    ;(async () => {
      try {
        const response = await apiFetch(`/api/agent3/executions/${executionJobId}/units`)
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        const body = await response.json()
        if (!cancelled) setData(body)
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    })()
    return () => { cancelled = true }
  }, [executionJobId])

  if (selectedUnitId) {
    return <ExecutionUnitPage executionJobId={executionJobId} clusterId={selectedUnitId} onClose={onCloseUnit} />
  }

  return (
    <main className="page-shell">
      <section className="hero" aria-label="Code generation results">
        <button type="button" className="token-toggle back-link" onClick={onBack}>
          <span>‹</span> Back to status
        </button>
        <p className="eyebrow">CODE GENERATION RESULTS</p>
        <h1>Execution <code>{executionJobId}</code> results</h1>
        {job?.final_test_summary && (
          <p className="reingest-hint">
            Final test run: {job.final_test_summary.passed} passed, {job.final_test_summary.failed} failed
            {job.final_test_summary.errors ? `, ${job.final_test_summary.errors} errors` : ''}.
          </p>
        )}
        {error && <p className="error">{error}</p>}
        {!error && !data && <p className="summary-loading">Loading results…</p>}
        {data && data.phases.length === 0 && <p className="summary-loading">This execution has no units.</p>}
        {data && data.phases.length > 0 && (
          <ol className="plan-phases">
            {data.phases.map((phase) => (
              <li key={phase.phase_cluster_id} className="plan-phase">
                <div className="plan-phase-header">
                  <span className="plan-phase-title">
                    Phase {phase.phase_order}{phase.label ? ` · ${phase.label}` : ''}
                  </span>
                  <span className="plan-phase-count">
                    {phase.units.length} {phase.units.length === 1 ? 'unit' : 'units'}
                  </span>
                </div>
                <div className="plan-phase-body">
                  <ol className="plan-units">
                    {phase.units.map((unit) => (
                      <ExecutionUnitRow key={unit.cluster_id} unit={unit} onSelect={onSelectUnit} />
                    ))}
                  </ol>
                </div>
              </li>
            ))}
          </ol>
        )}
        <PrimaryButton label="Back to plan" type="button" onClick={onBackToPlan} style={{ marginTop: 8 }} />
        <DownloadProjectButton executionJobId={executionJobId} hasOutput={Boolean(job?.has_output)} />
      </section>
    </main>
  )
}

// tasks_agent3.md Phase 5 "Download" — GET /api/agent3/executions/{id}/download
// (design_agent3.md §9): a 5-minute signed URL, `409` until the output exists.
// Disabled with a reason until `job.has_output`, same convention as
// GenerateCodeAction's `generateCodeBlocker` hint.
function DownloadProjectButton({ executionJobId, hasOutput }) {
  const [error, setError] = useState(null)
  const [downloading, setDownloading] = useState(false)
  const apiFetch = useApiFetch()

  const download = async () => {
    setError(null)
    setDownloading(true)
    try {
      const response = await apiFetch(`/api/agent3/executions/${executionJobId}/download`)
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(formatJobErrorMessage(response.status, body))
      }
      const data = await response.json()
      window.location.href = data.url
    } catch (err) {
      setError(err.message)
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div style={{ marginTop: 12 }}>
      {!hasOutput && <p className="reingest-hint">Download is available once this execution finishes.</p>}
      <PrimaryButton
        label="Download project (.zip)"
        type="button"
        disabled={!hasOutput}
        loading={downloading}
        onClick={download}
      />
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}

// Agent 3 unit flags (unit_executions.flags, design_agent3.md §2): unverified_inputs,
// built_on_failed_dependency, stubbed — all caveats on how trustworthy the verdict is,
// unlike Agent 2's foundation/merged flags, so all three render as a warn-style pill.
function ExecutionUnitRow({ unit, onSelect }) {
  const contract = unit.test_summary?.contract
  const behaviour = unit.test_summary?.behaviour
  return (
    <li className="plan-unit-row">
      <button type="button" className="plan-unit" onClick={() => onSelect(unit.cluster_id)}>
        <span className="plan-unit-order">{unit.unit_order}</span>
        <span className="plan-unit-label">{unit.label || <em>Unlabeled unit</em>}</span>
        <span className="plan-unit-meta">
          <span className={`plan-status plan-status-${unit.status}`}>{unit.status.replace(/_/g, ' ')}</span>
          {unit.flags.map((flag) => (
            <span key={flag} className="plan-flag plan-flag-warn">{flag.replace(/_/g, ' ')}</span>
          ))}
          <span className="plan-unit-size">{unit.attempts} {unit.attempts === 1 ? 'attempt' : 'attempts'}</span>
          {contract && (
            <span className={`plan-flag${contract.failed > 0 ? ' plan-flag-warn' : ''}`}>
              contract {contract.passed}/{contract.passed + contract.failed}
            </span>
          )}
          {behaviour && behaviour.generated === false && (
            <span className="plan-flag plan-flag-warn">behaviour not generated</span>
          )}
          {behaviour && behaviour.generated !== false && (
            <span className={`plan-flag${behaviour.failed > 0 ? ' plan-flag-warn' : ''}`}>
              behaviour {behaviour.passed}/{behaviour.passed + behaviour.failed}
            </span>
          )}
        </span>
      </button>
    </li>
  )
}

// A row of chip buttons (one per file/member) that picks which one's content shows
// below — the same click-to-select-then-render pattern as the "Ask the codebase"
// citation chips, reused here for both the legacy and generated source panes.
function FilePickerPane({ title, items, emptyMessage }) {
  const [selectedKey, setSelectedKey] = useState(items[0]?.key ?? null)
  useEffect(() => { setSelectedKey(items[0]?.key ?? null) }, [items])
  const selected = items.find((item) => item.key === selectedKey)

  return (
    <div className="execution-file-pane">
      <h4>{title}</h4>
      {items.length === 0 ? (
        <p className="entity-table-empty">{emptyMessage}</p>
      ) : (
        <>
          <div className="summary-badges">
            {items.map((item) => (
              <button
                key={item.key}
                type="button"
                className={`citation-chip${item.key === selectedKey ? ' citation-chip-active' : ''}`}
                onClick={() => setSelectedKey(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
          {selected && (
            <figure className="ask-excerpt">
              {selected.meta && <figcaption>{selected.meta}</figcaption>}
              <pre>{selected.content ?? 'No source available for this member.'}</pre>
            </figure>
          )}
        </>
      )}
    </div>
  )
}

// tasks_agent3.md Phase 5 "Unit detail" — GET /api/agent3/executions/{id}/units/{cluster_id}
// (design_agent3.md §9). Reached via ExecutionResultsPage's now-clickable unit rows
// (?execution=&unit=, mirroring PlanPage's ?unit= sub-param mechanism, §12).
function ExecutionUnitPage({ executionJobId, clusterId, onClose }) {
  const [unit, setUnit] = useState(null)
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    setUnit(null)
    setError(null)
    apiFetch(`/api/agent3/executions/${executionJobId}/units/${clusterId}`)
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        return response.json()
      })
      .then((data) => { if (!cancelled) setUnit(data) })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [executionJobId, clusterId])

  const legacyItems = (unit?.legacy || []).map((member) => ({
    key: member.entity_id,
    label: member.name,
    meta: `${member.file_path}${member.line ? ` lines ${member.line}–${member.end_line}` : ''}`,
    content: member.source,
  }))
  const fileItems = (unit?.files || []).map((file) => ({ key: file.path, label: file.path, meta: file.path, content: file.content }))
  const symbolEntries = Object.entries(unit?.symbol_map || {})

  return (
    <main className="page-shell">
      <section className="hero" aria-label="Unit detail">
        <button type="button" className="token-toggle back-link" onClick={onClose}>
          <span>‹</span> Back to results
        </button>
        <p className="eyebrow">UNIT</p>
        <h1><code>{clusterId}</code></h1>
        {error && <p className="error">{error}</p>}
        {!error && !unit && <p className="summary-loading">Loading unit…</p>}
        {unit && (
          <>
            <div className="summary-badges">
              <span className={`plan-status plan-status-${unit.status}`}>{unit.status.replace(/_/g, ' ')}</span>
              <span className="plan-unit-size">{unit.attempts} {unit.attempts === 1 ? 'attempt' : 'attempts'}</span>
              {unit.flags.map((flag) => (
                <span key={flag} className="plan-flag plan-flag-warn">{flag.replace(/_/g, ' ')}</span>
              ))}
            </div>
            {unit.last_error && <p className="error">{unit.last_error}</p>}

            <div className="execution-file-panes">
              <FilePickerPane title="Legacy source" items={legacyItems} emptyMessage="This unit has no member source to show." />
              <FilePickerPane title="Generated code" items={fileItems} emptyMessage="No generated code was uploaded for this unit." />
            </div>

            <h4>Tests</h4>
            {(unit.tests || []).length === 0 ? (
              <p className="entity-table-empty">No test files were uploaded for this unit.</p>
            ) : (
              unit.tests.map((test) => (
                <figure key={test.path} className="ask-excerpt">
                  <figcaption><code>{test.path}</code></figcaption>
                  <pre>{test.content}</pre>
                </figure>
              ))
            )}

            <h4>Log</h4>
            {unit.log ? (
              <details className="coverage-details">
                <summary>Sandbox output</summary>
                <pre>{unit.log}</pre>
              </details>
            ) : (
              <p className="entity-table-empty">No log was recorded for this unit.</p>
            )}

            <h4>Symbol map</h4>
            {symbolEntries.length === 0 ? (
              <p className="entity-table-empty">No symbol map was recorded for this unit.</p>
            ) : (
              <div className="plan-table-scroll">
                <table className="entity-table">
                  <thead><tr><th>Entity</th><th>Module</th><th>Name</th><th>Kind</th></tr></thead>
                  <tbody>
                    {symbolEntries.map(([entityId, symbol]) => (
                      <tr key={entityId}>
                        <td className="entity-file-path">{entityId}</td>
                        <td><code>{symbol.python_module}</code></td>
                        <td><code>{symbol.python_name}</code></td>
                        <td>{symbol.kind}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>
    </main>
  )
}

// Query params that select a page (no router — see tasks_agent2.md conventions).
const PAGE_PARAMS = ['job', 'project', 'planning_job', 'plan', 'execution']
// Params that refine a page: `unit`/`phase_id` alongside `?plan=` (tasks_agent2.md),
// and `unit` alongside `?execution=` too (tasks_agent3.md §12: "?execution=<id>
// (optionally &unit=<cluster_id>)") — every other navigation clears them.
const SUB_PARAMS = ['unit', 'phase_id']

function App() {
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [tab, setTab] = useState('url')
  const [showToken, setShowToken] = useState(false)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [archiveFile, setArchiveFile] = useState(null)
  // One page at a time: at most one of PAGE_PARAMS is ever in the URL.
  const [page, setPage] = useState(() => {
    const params = new URLSearchParams(window.location.search)
    const param = PAGE_PARAMS.find((name) => params.get(name))
    if (!param) return null
    if (param === 'plan') return { param, id: params.get(param), unit: params.get('unit'), phaseId: params.get('phase_id') }
    if (param === 'execution') return { param, id: params.get(param), unit: params.get('unit') }
    return { param, id: params.get(param) }
  })
  const apiFetch = useApiFetch()

  const navigateToPage = (param, id) => {
    const url = new URL(window.location.href)
    PAGE_PARAMS.forEach((name) => url.searchParams.delete(name))
    SUB_PARAMS.forEach((name) => url.searchParams.delete(name)) // only meaningful alongside ?plan=/?execution=
    if (param) url.searchParams.set(param, id)
    window.history.pushState({}, '', url)
    setPage(param ? { param, id } : null)
  }
  // `&unit=` and `&phase_id=` sit alongside `?plan=`/`?execution=` rather than
  // replacing the page (tasks_agent2.md/tasks_agent3.md conventions). A null value
  // removes the param.
  const setSubParam = (param, key, value) => {
    const url = new URL(window.location.href)
    if (value) url.searchParams.set(param, value)
    else url.searchParams.delete(param)
    window.history.pushState({}, '', url)
    setPage((current) => ({ ...current, [key]: value }))
  }
  const selectPlanUnit = (clusterId) => setSubParam('unit', 'unit', clusterId)
  const closePlanUnit = () => setSubParam('unit', 'unit', null)
  const selectPlanPhase = (phaseId) => setSubParam('phase_id', 'phaseId', phaseId)
  const selectExecutionUnit = (clusterId) => setSubParam('unit', 'unit', clusterId)
  const closeExecutionUnit = () => setSubParam('unit', 'unit', null)
  const navigateToJobStatus = (jobId) => navigateToPage('job', jobId)
  const navigateToProject = (projectId) => navigateToPage('project', projectId)
  const navigateToPlanningJob = (planningJobId) => navigateToPage('planning_job', planningJobId)
  const navigateToPlan = (planId) => navigateToPage('plan', planId)
  const navigateToExecution = (executionJobId) => navigateToPage('execution', executionJobId)
  const backToForm = () => navigateToPage(null)

  const submit = async (event) => {
    event.preventDefault()
    if (tab === 'url' && !url.trim()) return
    if (tab === 'archive' && !archiveFile) return

    if (tab === 'url' && !isValidGithubUrl(url.trim())) {
      setError('Enter a valid GitHub repository URL, e.g. https://github.com/owner/repository')
      return
    }

    setError(null)
    setSubmitting(true)
    try {
      const response = tab === 'url'
        ? await apiFetch('/api/agent1/jobs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              source_type: 'github',
              repo_url: url.trim(),
              ...(token.trim() ? { access_token: token.trim() } : {}),
            }),
          })
        : await apiFetch('/api/agent1/jobs', {
            method: 'POST',
            body: (() => {
              const formData = new FormData()
              formData.append('file', archiveFile)
              return formData
            })(),
          })

      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(formatJobErrorMessage(response.status, body))
      }
      const data = await response.json()
      navigateToJobStatus(data.job_id)
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  if (page?.param === 'job') {
    return <JobStatusPage jobId={page.id} onBackToForm={backToForm} />
  }

  if (page?.param === 'planning_job') {
    return (
      <PlanningJobStatusPage
        planningJobId={page.id}
        onBackToProject={navigateToProject}
        onViewPlan={navigateToPlan}
        onBackToForm={backToForm}
      />
    )
  }

  if (page?.param === 'plan') {
    return (
      <PlanPage
        planId={page.id}
        selectedUnitId={page.unit}
        phaseId={page.phaseId}
        onSelectUnit={selectPlanUnit}
        onCloseUnit={closePlanUnit}
        onSelectPhase={selectPlanPhase}
        onBackToProject={navigateToProject}
        onBackToForm={backToForm}
        onOpenPlan={navigateToPlan}
        onExecutionCreated={navigateToExecution}
        onOpenExecution={navigateToExecution}
      />
    )
  }

  if (page?.param === 'execution') {
    return (
      <ExecutionStatusPage
        executionJobId={page.id}
        selectedUnitId={page.unit}
        onSelectUnit={selectExecutionUnit}
        onCloseUnit={closeExecutionUnit}
        onBackToPlan={navigateToPlan}
        onBackToForm={backToForm}
      />
    )
  }

  if (page?.param === 'project') {
    return (
      <ProjectDetailPage
        projectId={page.id}
        onSelectJob={navigateToJobStatus}
        onSelectPlanningJob={navigateToPlanningJob}
        onSelectPlan={navigateToPlan}
        onBackToList={backToForm}
      />
    )
  }

  return (
    <main className="page-shell">


      <section className="hero" id="top">
        <p className="eyebrow">CODEBASE UNDERSTANDING, ON DEMAND</p>
        <h1>See the shape of<br /><em>any codebase.</em></h1>
        <p className="intro">Turn a GitHub repository into an architectural brief, dependency map,<br className="desktop-break" /> and navigable AST overview.</p>

        <form className="analyzer" onSubmit={submit}>
          <div className="tabs" role="tablist" aria-label="Repository source">
            <button className={tab === 'url' ? 'active' : ''} type="button" role="tab" aria-selected={tab === 'url'} onClick={() => { setTab('url'); setError(null) }}>GitHub URL</button>
            <button className={tab === 'archive' ? 'active' : ''} type="button" role="tab" aria-selected={tab === 'archive'} onClick={() => { setTab('archive'); setError(null) }}>Upload archive</button>
          </div>
          {tab === 'url' ? <>
            <label htmlFor="repo-url">GitHub repository URL</label>
            <div className="input-row">
              <div className="url-input"><GithubIcon /><input id="repo-url" value={url} onChange={(e) => { setUrl(e.target.value); setError(null) }} placeholder="https://github.com/owner/repository" /></div>
              <PrimaryButton label="Analyze" type="submit" loading={submitting} className="analyze-button" style={{ minWidth: 150 }} />
            </div>
            <button className="token-toggle" type="button" onClick={() => setShowToken(!showToken)}><span>{showToken ? '⌄' : '›'}</span> Private repository? Add a GitHub token</button>
            {showToken && <input className="token-input" aria-label="GitHub personal access token" placeholder="GitHub personal access token" type="password" value={token} onChange={(e) => setToken(e.target.value)} />}
          </> : <>
            <ArchiveDropZone file={archiveFile} onFileSelected={setArchiveFile} onRejected={setError} />
            <PrimaryButton label="Analyze" type="submit" disabled={!archiveFile} loading={submitting} className="archive-submit" />
          </>}
          {error && <p className="error">{error}</p>}
        </form>
      </section>

      <section className="projects" aria-label="Ingested repositories">
        <ProjectsList onSelectProject={navigateToProject} />
      </section>


    </main>
  )
}

// Gate: renders sign-in until a Supabase session exists, then the real app.
// Every child reads the session via useAuth()/useApiFetch() instead of
// receiving it as a prop, so it stays out of the way of components that
// don't need it.
function SupabaseSetupNeeded({ error }) {
  return (
    <main className="page-shell">

      <section className="hero" id="top">
        <p className="eyebrow">SETUP NEEDED</p>
        <h1>Supabase isn't configured</h1>
        <p className="intro">{error}</p>
        <p className="intro">
          The app reads its Supabase settings from the backend (<code>GET /api/config</code>): set{' '}
          <code>SUPABASE_DB_URL</code> and <code>SUPABASE_ANON_KEY</code> in the repo's <code>.env</code>, restart
          the backend, then reload this page.
        </p>
      </section>
    </main>
  )
}

function Root() {
  const { session, loading, configured, configError } = useAuth()
  if (!configured) return <SupabaseSetupNeeded error={configError} />
  if (loading) return null
  if (!session) return <SignIn />
  return <App />
}

const themeStore = createThemeStore(createBrowserThemeAdapter(window))

createRoot(document.getElementById('root')).render(
  <ThemeProvider store={themeStore}>
    <AuthProvider>
      <ThemeToggle />
      <ProductShell><Root /></ProductShell>
    </AuthProvider>
  </ThemeProvider>,
)
