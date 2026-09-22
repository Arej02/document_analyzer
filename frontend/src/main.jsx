import { useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ReactFlow, Background, Handle, Position } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import './styles.css'
import { AuthProvider, useAuth, useApiFetch } from './AuthProvider'
import { SignIn } from './SignIn'
import StarfieldButton from './StarfieldButton'

const Mark = () => (
  <svg className="brand-mark" viewBox="0 0 30 30" aria-hidden="true">
    <path d="M15 2.5 27.5 15 15 27.5 2.5 15 15 2.5Z" />
    <path d="m15 10 5 5-5 5-5-5 5-5Z" />
  </svg>
)

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
  const [projects, setProjects] = useState([])
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
  if (!projects.length) return null

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

function jobStageState(stageKey, index, status, progressStage) {
  if (status === 'done') return 'complete'
  const currentIndex = JOB_STAGES.findIndex((stage) => stage.key === progressStage)
  if (currentIndex === -1) return 'pending' // queued, or failed before any stage was recorded
  if (index < currentIndex) return 'complete'
  if (index === currentIndex) return status === 'failed' ? 'failed' : 'active'
  return 'pending'
}

function ProgressStages({ status, progressStage }) {
  return (
    <ol className="job-stages" aria-label="Ingestion progress">
      {JOB_STAGES.map((stage, index) => {
        const state = jobStageState(stage.key, index, status, progressStage)
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
      {entityTypeEntries.length > 0 && (
        <ul className="entity-counts">
          {entityTypeEntries.map(([entityType, count]) => (
            <li key={entityType}><strong>{count}</strong> {count === 1 ? entityType : (ENTITY_TYPE_PLURAL[entityType] || `${entityType}s`)}</li>
          ))}
        </ul>
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
              <tr><th>Type</th><th>Name</th><th>File</th><th>Risk</th><th>Grounding</th></tr>
            </thead>
            <tbody>
              {page.entities.map((entity) => (
                <tr key={entity.id}>
                  <td>{entity.entity_type}</td>
                  <td>{entity.name}</td>
                  <td className="entity-file-path">{entity.file_path}</td>
                  <td>{entity.risk_level ? <span className={`risk-level risk-level-${entity.risk_level}`}>{entity.risk_level}</span> : '—'}</td>
                  <td>{entity.grounding_status === 'unverified' ? <span className="unverified-badge unverified-badge-warn">unverified</span> : (entity.grounding_status || '—')}</td>
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
  low: { border: '#63eeaf', background: 'rgba(99,238,175,.14)', color: '#7fe6ae' },
  medium: { border: '#ffcf7a', background: 'rgba(255,207,122,.14)', color: '#ffcf7a' },
  high: { border: '#ff5c4d', background: 'rgba(255,92,77,.16)', color: '#ff9c8a' },
}
const DEFAULT_RISK_NODE_STYLE = { border: 'rgba(255,255,255,.22)', background: 'rgba(255,255,255,.05)', color: '#cdd8f2' }

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
        style: { stroke: 'rgba(255,255,255,.18)' },
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
        <Background gap={16} color="rgba(255,255,255,.06)" />
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
      <StarfieldButton
        label="Re-ingest"
        type="submit"
        disabled={submitting || (project.source_type === 'upload' && !file)}
        style={{ marginTop: 14 }}
      />
      {error && <p className="error">{error}</p>}
    </form>
  )
}

// tasks.md Phase 5's home for the re-ingest action above — GET /api/agent1/projects/
// {project_id} (design.md §4: source info, full job history most-recent-first, and
// the latest job's status) had never had a frontend view built for it before this
// (Phase 1's own note on that endpoint flagged this as still-missing). Each job in
// the history links to its own status page (JobStatusPage) via the same navigation
// ReingestAction's own success handler uses.
function ProjectDetailPage({ projectId, onSelectJob, onBackToList }) {
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
        <p className="eyebrow">{job ? job.status.toUpperCase() : 'LOADING'}</p>
        <h1>Job <code>{jobId}</code></h1>
        {error && <p className="error">{error}</p>}
        {job && <DiffSummary job={job} />}
        {job && <ProgressStages status={job.status} progressStage={job.progress_stage} />}
        {job && (job.status === 'queued' || job.status === 'running') && <LiveEntityTree jobId={jobId} />}
        {failed && (
          <p className="error job-failed-message">
            Ingestion failed: {job.error || 'an unknown error occurred.'}
          </p>
        )}
        {done && <JobSummary jobId={jobId} />}
        <StarfieldButton
          label={failed ? 'Retry' : 'Analyze'}
          type="button"
          onClick={onBackToForm}
          style={{ marginTop: 8 }}
        />
      </section>
      {done && <EntityBrowser jobId={jobId} />}
    </main>
  )
}

function App() {
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [tab, setTab] = useState('url')
  const [showToken, setShowToken] = useState(false)
  const [error, setError] = useState(null)
  const [archiveFile, setArchiveFile] = useState(null)
  const [activeJobId, setActiveJobId] = useState(
    () => new URLSearchParams(window.location.search).get('job'),
  )
  const [activeProjectId, setActiveProjectId] = useState(
    () => new URLSearchParams(window.location.search).get('project'),
  )
  const apiFetch = useApiFetch()
  const { signOut } = useAuth()

  const navigateToJobStatus = (jobId) => {
    const url = new URL(window.location.href)
    url.searchParams.delete('project')
    url.searchParams.set('job', jobId)
    window.history.pushState({}, '', url)
    setActiveProjectId(null)
    setActiveJobId(jobId)
  }

  const navigateToProject = (projectId) => {
    const url = new URL(window.location.href)
    url.searchParams.delete('job')
    url.searchParams.set('project', projectId)
    window.history.pushState({}, '', url)
    setActiveJobId(null)
    setActiveProjectId(projectId)
  }

  const backToForm = () => {
    const url = new URL(window.location.href)
    url.searchParams.delete('job')
    url.searchParams.delete('project')
    window.history.pushState({}, '', url)
    setActiveJobId(null)
    setActiveProjectId(null)
  }

  const submit = async (event) => {
    event.preventDefault()
    if (tab === 'url' && !url.trim()) return
    if (tab === 'archive' && !archiveFile) return

    if (tab === 'url' && !isValidGithubUrl(url.trim())) {
      setError('Enter a valid GitHub repository URL, e.g. https://github.com/owner/repository')
      return
    }

    setError(null)
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
    }
  }

  if (activeJobId) {
    return <JobStatusPage jobId={activeJobId} onBackToForm={backToForm} />
  }

  if (activeProjectId) {
    return (
      <ProjectDetailPage projectId={activeProjectId} onSelectJob={navigateToJobStatus} onBackToList={backToForm} />
    )
  }

  return (
    <main className="page-shell">
      <header className="site-header">
        <a className="brand" href="#top" aria-label="galvanise home"><Mark /><span>galvanise</span></a>
        <p className="status"><i /> AI repository intelligence</p>
        <button type="button" className="token-toggle" onClick={signOut}>Sign out</button>
      </header>

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
              <StarfieldButton label="Analyze" type="submit" className="analyze-button" style={{ minWidth: 150 }} />
            </div>
            <button className="token-toggle" type="button" onClick={() => setShowToken(!showToken)}><span>{showToken ? '⌄' : '›'}</span> Private repository? Add a GitHub token</button>
            {showToken && <input className="token-input" aria-label="GitHub personal access token" placeholder="GitHub personal access token" type="password" value={token} onChange={(e) => setToken(e.target.value)} />}
          </> : <>
            <ArchiveDropZone file={archiveFile} onFileSelected={setArchiveFile} onRejected={setError} />
            <StarfieldButton label="Analyze" type="submit" disabled={!archiveFile} className="archive-submit" />
          </>}
          {error && <p className="error">{error}</p>}
        </form>
      </section>

      <section className="projects" aria-label="Ingested repositories">
        <ProjectsList onSelectProject={navigateToProject} />
      </section>

      <footer><i /> Built for the parts of a codebase that aren’t obvious at first glance.</footer>
    </main>
  )
}

// Gate: renders sign-in until a Supabase session exists, then the real app.
// Every child reads the session via useAuth()/useApiFetch() instead of
// receiving it as a prop, so it stays out of the way of components that
// don't need it.
function SupabaseSetupNeeded() {
  return (
    <main className="page-shell">
      <section className="hero" id="top">
        <p className="eyebrow">SETUP NEEDED</p>
        <h1>Supabase isn't configured</h1>
        <p className="intro">
          Copy <code>frontend/.env.example</code> to <code>frontend/.env</code>, fill in{' '}
          <code>VITE_SUPABASE_URL</code> and <code>VITE_SUPABASE_ANON_KEY</code>, then restart the dev server.
        </p>
      </section>
    </main>
  )
}

function Root() {
  const { session, loading, configured } = useAuth()
  if (!configured) return <SupabaseSetupNeeded />
  if (loading) return null
  if (!session) return <SignIn />
  return <App />
}

createRoot(document.getElementById('root')).render(
  <AuthProvider>
    <Root />
  </AuthProvider>,
)
