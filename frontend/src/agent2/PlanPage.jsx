// The Agent 2 plan page and everything only it uses, moved verbatim out of main.jsx (tasks_frontend.md A2.5).
// Behaviour is unchanged: Submit for approval (`SubmitPlanAction`) and Generate code (`GenerateCodeAction`)
// are the same code. `App` in main.jsx still owns the URL (`?plan=`, `&unit=`, `&phase_id=`) and passes
// handlers in.
import { useEffect, useMemo, useRef, useState } from 'react'
import { ReactFlow, Background, Handle, MarkerType, Position } from '@xyflow/react'
import { useApiFetch, useAuth } from '../AuthProvider'
import PrimaryButton from '../PrimaryButton'
import {
  ConfidenceBadge, DEFAULT_RISK_NODE_STYLE, GroundingIssuesList, LIVE_TREE_NODE_CAP, RISK_NODE_STYLE, UnverifiedBadge, formatJobErrorMessage,
} from '../shared.jsx'
import PlanDocumentView from './PlanDocumentView.jsx'
import PlanStatusBanner from './PlanStatusBanner.jsx'
import { PlanFailed, PlanSkeleton } from './PlanStates.jsx'
import { reviewAccess } from './reviewAccess.js'

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
      <label className="plan-phase-note-label" htmlFor={noteId}>Note (optional, saved with your decision)</label>
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

// tasks_agent2.md Phase 5 "Approval" — the facade acknowledge form. Each open cycle is a decision card in
// the document's "Open questions" section (`OpenQuestions`), and this is the control it slots in: a required
// note, sent as a `facade_ack` override, which makes a new plan version. The cycle is a card from
// `openQuestionsModel` ({level, sccId}).
function FacadeAcknowledgeForm({ cycle, pending, onAcknowledge }) {
  const [note, setNote] = useState('')
  const [error, setError] = useState(null)
  const noteId = `plan-facade-note-${cycle.level}-${cycle.sccId}`

  const acknowledge = async (event) => {
    event.preventDefault()
    setError(null)
    try {
      await onAcknowledge({ level: cycle.level, scc_id: cycle.sccId, note: note.trim() })
    } catch (err) {
      setError(err.message)
    }
  }

  return (
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
  )
}

// The merge tick and move buttons that used to sit in the unit row (design_agent2.md §9.2 `merge`, `reorder`).
function UnitEditControls({ unit, edit }) {
  const label = unit.label || `Unit ${unit.unit_order}`
  return (
    <>
      <input
        type="checkbox"
        className="plan-unit-select"
        checked={edit.checked}
        onChange={() => edit.onToggle(unit.cluster_id)}
        disabled={edit.disabled}
        aria-label={`Select ${label} to merge`}
      />
      <span className="plan-unit-move" role="group" aria-label={`Move ${label}`}>
        <button type="button" onClick={() => edit.onMove(unit, -1)} disabled={edit.disabled || edit.isFirst} aria-label={`Move ${label} up`} title="Move up">↑</button>
        <button type="button" onClick={() => edit.onMove(unit, 1)} disabled={edit.disabled || edit.isLast} aria-label={`Move ${label} down`} title="Move down">↓</button>
      </span>
    </>
  )
}

function MergeBar({ count, pending, onMerge }) {
  return (
    <div className="plan-merge-bar">
      <button type="button" className="plan-phase-decide" disabled={pending || count < 2} onClick={onMerge}>
        {pending ? 'Saving…' : `Merge ${count} selected`}
      </button>
      <span className="plan-merge-hint">
        Tick two or more units to merge them, or move one with ↑ ↓. Each change makes a new version.
      </span>
    </div>
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
// is already `stale` where it applies. The header keeps Submit for approval and Generate code
// exactly as before; the plan itself is the document in PlanDocumentView (outline, eight sections,
// rail). What each control may do comes from `reviewAccess`, which mirrors the backend's rules.
// A unit click sets `&unit=`, which opens PlanUnitDetail at the top of the document; a
// `&phase_id=` link scrolls to the graph, drilled into that phase.
export default function PlanPage({
  planId, selectedUnitId, phaseId, onSelectUnit, onCloseUnit, onSelectPhase, onBackToProject, onBackToForm, onOpenPlan,
  onExecutionCreated, onOpenExecution, onProjectId,
}) {
  const [plan, setPlan] = useState(null)
  const [error, setError] = useState(null)
  const [attempt, setAttempt] = useState(0)
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
  // the reader's place in the document and the selected unit stay as they were.
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
        onProjectId?.(data.project_id)
      })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [planId, attempt])

  const unitLabels = useMemo(() => {
    const labels = new Map()
    plan?.phases.forEach((phase) => phase.units.forEach((unit) => {
      labels.set(unit.cluster_id, unit.label || `Unit ${phase.phase_order}.${unit.unit_order}`)
    }))
    return labels
  }, [plan])
  const unitLabel = (clusterId) => unitLabels.get(clusterId) || `Unit ${clusterId.slice(0, 8)}`

  // What each control may do (design_agent2.md §9.1, §9.2, §11); the backend still enforces it.
  const access = reviewAccess(plan)
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

  const approvedExtra = plan?.status === 'approved' && !plan.is_stale ? (
    <>
      by{' '}
      {plan.approved_by === session?.user?.id
        ? 'you'
        : plan.approved_by ? <code title={plan.approved_by}>{plan.approved_by.slice(0, 8)}</code> : 'an unknown reviewer'}
      {plan.approved_at && <> at {new Date(plan.approved_at).toLocaleString()}</>}
    </>
  ) : null

  // Everything the document cannot own: existing controls that write, handed in as slots.
  const slots = plan && {
    graph: (
      <PlanGraph
        planId={planId}
        phases={plan.phases}
        phaseId={phaseId}
        selectedUnitId={selectedUnitId}
        onSelectPhase={onSelectPhase}
        onSelectUnit={onSelectUnit}
      />
    ),
    unitDetail: selectedUnitId ? (
      <PlanUnitDetail planId={planId} clusterId={selectedUnitId} unitLabel={unitLabel} onClose={onCloseUnit} />
    ) : null,
    renderControls: (card) => {
      const phase = plan.phases.find((p) => p.phase_cluster_id === card.id)
      return (
        <>
          {access.canDecide
            ? <PhaseApprovalControls planId={planId} phase={phase} onDecided={refreshPlan} />
            : access.state === 'draft' && access.reasons.decide && <p className="a2-note">{access.reasons.decide}</p>}
          {access.canOverride && phase.units.length > 1 && (
            <MergeBar count={mergeCount(phase, mergeSelection)} pending={overridePending} onMerge={() => mergeSelected(phase)} />
          )}
          {overrideErrors[card.id] && <p className="error" role="alert">{overrideErrors[card.id]}</p>}
        </>
      )
    },
    renderUnitExtra: (unit, index, card) => {
      if (!access.canOverride || card.units.length < 2) return null
      return (
        <UnitEditControls
          unit={unit}
          edit={{
            checked: mergeSelection.has(unit.cluster_id),
            onToggle: toggleMergeUnit,
            onMove: (movedUnit, delta) => moveUnit(plan.phases.find((p) => p.phase_cluster_id === card.id), movedUnit, delta),
            isFirst: index === 0,
            isLast: index === card.units.length - 1,
            disabled: overridePending,
          }}
        />
      )
    },
    renderAcknowledge: access.canOverride
      ? (card) => <FacadeAcknowledgeForm cycle={card} pending={overridePending} onAcknowledge={(payload) => createOverride('facade_ack', payload)} />
      : undefined,
  }

  return (
    <main className="page-shell">

      <section className="hero project-detail" aria-label="Migration plan">
        <button
          type="button"
          className="nav-btn"
          onClick={() => (plan ? onBackToProject(plan.project_id) : onBackToForm())}
        >
          <span aria-hidden="true">←</span> {plan ? 'Back to project' : 'Back to repositories'}
        </button>
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
            <PlanStatusBanner access={access} extra={approvedExtra} />
          </>
        )}
      </section>
      {!plan && !error && <PlanSkeleton />}
      {error && (
        <PlanFailed
          title="The plan could not be loaded"
          cause={error}
          action={(
            <>
              <PrimaryButton label="Retry" type="button" onClick={() => setAttempt((n) => n + 1)} />
              <PrimaryButton label="Back" type="button" onClick={onBackToForm} />
            </>
          )}
        />
      )}
      {plan && (plan.status === 'approved' || plan.is_stale) && (
        // An approved plan that later went stale keeps its earlier runs (design_agent3.md §13).
        <ExecutionRunsList planId={planId} onOpenExecution={onOpenExecution} />
      )}
      {plan && (
        <PlanDocumentView
          plan={plan}
          planId={planId}
          selectedUnitId={selectedUnitId}
          phaseId={phaseId}
          onSelectUnit={onSelectUnit}
          slots={slots}
        />
      )}
      {plan && (
        // Next steps sit after the document, so the review happens before the button that ends it.
        <section className="next-step" aria-label="Next steps">
          <button type="button" className="nav-btn" onClick={() => onBackToProject(plan.project_id)}>
            <span aria-hidden="true">←</span> Back to project
          </button>
          <div className="next-step-actions">
            {plan.status === 'draft' && <SubmitPlanAction planId={planId} onSubmitted={refreshPlan} />}
            <GenerateCodeAction plan={plan} onExecutionCreated={onExecutionCreated} />
          </div>
        </section>
      )}
    </main>
  )
}
