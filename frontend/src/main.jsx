import { useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ReactFlow, Background, Handle, Position } from '@xyflow/react'
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
import { ProgressStages } from './ProgressStages'
import { RunView } from './agent1/RunView.jsx'
import { AppSidebar } from './Sidebar'
import { OutlineSlotContext } from './OutlineSlot.jsx'
import PlanPage from './agent2/PlanPage.jsx'
import GithubConnectionPanel from './agent3/GithubConnectionPanel.jsx'
import DeliverToGithubAction from './agent3/DeliverToGithubAction.jsx'
import PlanningProgress from './agent2/PlanningProgress.jsx'
import {
  ConfidenceBadge, DEFAULT_RISK_NODE_STYLE, LIVE_TREE_NODE_CAP, RISK_NODE_STYLE, UnverifiedBadge, formatJobErrorMessage,
} from './shared.jsx'


const ARCHIVE_EXTENSIONS = ['.zip', '.tar', '.tar.gz', '.tgz']
const isArchiveFilename = (name) => ARCHIVE_EXTENSIONS.some((ext) => name.toLowerCase().endsWith(ext))

const GITHUB_URL_RE = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/?$/
const isValidGithubUrl = (value) => GITHUB_URL_RE.test(value)


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

// Agent 3's execution stages (design_agent3.md §2 execution_stage enum), with the same
// synthetic final 'done' step Agent 1's stepper used to have.
const EXECUTION_STAGES = [
  { key: 'prepare', label: 'Prepare' },
  { key: 'units', label: 'Units' },
  { key: 'assemble', label: 'Assemble' },
  { key: 'final_test', label: 'Final test' },
  { key: 'done', label: 'Done' },
]

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
            from them, the code generated from those plans, and the GitHub connection and stored token used for delivery.
            Pull requests already opened on GitHub are not touched. It can't be undone.
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
        <button type="button" className="nav-btn" onClick={onBackToList}>
          <span aria-hidden="true">←</span> Back to repositories
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
        <section className="job-history" aria-label="GitHub delivery settings">
          <GithubConnectionPanel projectId={projectId} />
        </section>
      )}
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
// The Failed state's "Restart analysis": loads the job's project and offers the existing
// ReingestAction (re-pull the source, or pick a new archive for an upload project).
function JobRestart({ projectId, onReingested }) {
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

  if (error) return <p className="error">{error}</p>
  if (!project) return <p className="summary-loading">Loading restart options…</p>
  return <ReingestAction project={project} onReingested={onReingested} />
}

function JobStatusPage({ jobId, onBackToForm, onReingested, onViewProject, onPlanningJobCreated }) {
  return (
    <RunView
      jobId={jobId}
      onBackToForm={onBackToForm}
      onViewProject={onViewProject}
      slots={{
        diffSummary: (job) => <DiffSummary job={job} />,
        liveTree: () => <LiveEntityTree jobId={jobId} />,
        restart: (projectId) => <JobRestart projectId={projectId} onReingested={onReingested} />,
        // The plan is triggered by the user from here (a POST to Agent 2), not by Agent 1.
        nextStep: (job) => (
          <GeneratePlanAction
            project={{ id: job.project_id, latest_job_status: job.status }}
            onPlanningJobCreated={onPlanningJobCreated}
          />
        ),
        results: () => (
          <>
            <JobSummary jobId={jobId} />
            <ExtractionCoverage jobId={jobId} />
          </>
        ),
        browse: () => (
          <>
            <EntityBrowser jobId={jobId} />
            <AskCodebase jobId={jobId} />
          </>
        ),
      }}
    />
  )
}

// tasks_agent2.md Phase 1 "Planning status" — polls GET /api/agent2/planning-jobs/
// {id} (design_agent2.md §11) until the job is terminal, same loop shape as
// JobStatusPage. Unlike that page, the response carries project_id, so "Try again"
// can go straight back to the project page (where "Generate plan" lives) even
// after a refresh.
function PlanningJobStatusPage({ planningJobId, onBackToProject, onViewPlan, onBackToForm, onProjectId }) {
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
        onProjectId?.(data.project_id)
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
        <h1>{job && (job.status === 'queued' || job.status === 'running') ? 'Creating a detailed migration plan…' : 'Planning job'}</h1>
        <p className="a1-sub">Job <code>{planningJobId}</code></p>
        {error && <p className="error">{error}</p>}
        {!error && !job && <p className="summary-loading">Loading planning job…</p>}
        {job && (
          <PlanningProgress job={job} />
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
          className="nav-btn"
          onClick={() => (job ? onBackToPlan(job.plan_id) : onBackToForm())}
        >
          <span aria-hidden="true">←</span> {job ? 'Back to plan' : 'Back to repositories'}
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
        <DeliverToGithubAction executionJobId={executionJobId} projectId={job?.project_id} hasOutput={Boolean(job?.has_output)} />
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
          <div className="summary-badges" role="group" aria-label={`${title} files`}>
            {items.map((item) => (
              <button
                key={item.key}
                type="button"
                className={`citation-chip${item.key === selectedKey ? ' citation-chip-active' : ''}`}
                aria-pressed={item.key === selectedKey}
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
                <figure key={test.path} className="ask-excerpt execution-long-pre">
                  <figcaption><code>{test.path}</code></figcaption>
                  <pre>{test.content}</pre>
                </figure>
              ))
            )}

            <h4>Log</h4>
            {unit.log ? (
              <details className="coverage-details execution-long-pre">
                <summary>Sandbox output ({unit.log.length.toLocaleString()} characters)</summary>
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
// Params that refine a page: `unit` alongside `?plan=` (tasks_agent2.md),
// and `unit` alongside `?execution=` too (tasks_agent3.md §12: "?execution=<id>
// (optionally &unit=<cluster_id>)") — every other navigation clears them.
const SUB_PARAMS = ['unit']

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
    if (param === 'plan') return { param, id: params.get(param), unit: params.get('unit') }
    if (param === 'execution') return { param, id: params.get(param), unit: params.get('unit') }
    return { param, id: params.get(param) }
  })
  // Best-effort project id for the sidebar's "which repo is this" highlight — set
  // directly when navigating to a project, or reported back by PlanPage/
  // PlanningJobStatusPage once their fetch tells them (see AppSidebar's own note on
  // why a bare job page can't always resolve this).
  const [activeProjectId, setActiveProjectId] = useState(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  // The element in the sidebar where the open page draws its outline (see OutlineSlot.jsx).
  const [outlineEl, setOutlineEl] = useState(null)
  const apiFetch = useApiFetch()

  const navigateToPage = (param, id) => {
    const url = new URL(window.location.href)
    PAGE_PARAMS.forEach((name) => url.searchParams.delete(name))
    SUB_PARAMS.forEach((name) => url.searchParams.delete(name)) // only meaningful alongside ?plan=/?execution=
    if (param) url.searchParams.set(param, id)
    window.history.pushState({}, '', url)
    setPage(param ? { param, id } : null)
    setSidebarOpen(false)
  }
  // `&unit=` sits alongside `?plan=`/`?execution=` rather than
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
  const selectExecutionUnit = (clusterId) => setSubParam('unit', 'unit', clusterId)
  const closeExecutionUnit = () => setSubParam('unit', 'unit', null)
  const navigateToJobStatus = (jobId) => navigateToPage('job', jobId)
  const navigateToProject = (projectId) => navigateToPage('project', projectId)
  const navigateToPlanningJob = (planningJobId) => navigateToPage('planning_job', planningJobId)
  const navigateToPlan = (planId) => navigateToPage('plan', planId)
  const navigateToExecution = (executionJobId) => navigateToPage('execution', executionJobId)
  const backToForm = () => { navigateToPage(null); setActiveProjectId(null) }
  // page?.param === 'project' is authoritative even before any fetch resolves (the id
  // is right there in the URL); other pages rely on activeProjectId being reported
  // back once their own data arrives.
  const effectiveActiveProjectId = page?.param === 'project' ? page.id : activeProjectId

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

  let content
  if (page?.param === 'job') {
    content = <JobStatusPage jobId={page.id} onBackToForm={backToForm} onReingested={navigateToJobStatus} onViewProject={navigateToProject} onPlanningJobCreated={navigateToPlanningJob} />
  } else if (page?.param === 'planning_job') {
    content = (
      <PlanningJobStatusPage
        planningJobId={page.id}
        onBackToProject={navigateToProject}
        onViewPlan={navigateToPlan}
        onBackToForm={backToForm}
        onProjectId={setActiveProjectId}
      />
    )
  } else if (page?.param === 'plan') {
    content = (
      <PlanPage
        planId={page.id}
        selectedUnitId={page.unit}
        onSelectUnit={selectPlanUnit}
        onCloseUnit={closePlanUnit}
        onBackToProject={navigateToProject}
        onBackToForm={backToForm}
        onOpenPlan={navigateToPlan}
        onExecutionCreated={navigateToExecution}
        onProjectId={setActiveProjectId}
      />
    )
  } else if (page?.param === 'execution') {
    content = (
      <ExecutionStatusPage
        executionJobId={page.id}
        selectedUnitId={page.unit}
        onSelectUnit={selectExecutionUnit}
        onCloseUnit={closeExecutionUnit}
        onBackToPlan={navigateToPlan}
        onBackToForm={backToForm}
      />
    )
  } else if (page?.param === 'project') {
    content = (
      <ProjectDetailPage
        projectId={page.id}
        onSelectJob={navigateToJobStatus}
        onSelectPlanningJob={navigateToPlanningJob}
        onSelectPlan={navigateToPlan}
        onBackToList={backToForm}
      />
    )
  } else {
    content = (
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

  return (
    <div className="app-shell">
      <button type="button" className="sidebar-mobile-toggle" onClick={() => setSidebarOpen(true)}>
        <span aria-hidden="true">☰</span> Menu
      </button>
      {sidebarOpen && <div className="sidebar-backdrop" onClick={() => setSidebarOpen(false)} />}
      <AppSidebar
        className={sidebarOpen ? 'sidebar-open' : ''}
        currentPage={page}
        activeProjectId={effectiveActiveProjectId}
        onNewAnalysis={backToForm}
        onSelectProject={navigateToProject}
        onSelectJob={navigateToJobStatus}
        onSelectPlan={navigateToPlan}
        onOutlineEl={setOutlineEl}
      />
      <OutlineSlotContext.Provider value={{ el: outlineEl, close: () => setSidebarOpen(false) }}>
        <div className="app-content">{content}</div>
      </OutlineSlotContext.Provider>
    </div>
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
