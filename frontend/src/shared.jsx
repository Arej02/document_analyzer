// Small pieces used by both the Agent 1 pages (main.jsx) and the Agent 2 plan page (agent2/PlanPage.jsx).
// Moved verbatim out of main.jsx so neither imports the other.

// FastAPI error bodies are either {"detail": "some message"} (HTTPException) or
// {"detail": [{"msg": "...", ...}, ...]} (pydantic validation errors) — read
// whichever shape actually came back instead of dumping raw JSON at the user.
export const formatJobErrorMessage = (status, body) => {
  const detail = body?.detail
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message
  if (Array.isArray(detail) && detail.length) {
    const messages = detail.map((item) => item?.msg).filter(Boolean)
    if (messages.length) return messages.join('; ')
  }
  return `Request failed (${status})`
}

// tasks.md Phase 4 "Surface unverified summaries distinctly ... so it's visible that
// manual review may be needed — don't hide this": a dedicated badge, not folded into
// plain body text, and shown either way — a reassuring "all verified" state when
// unverified_count is 0, not just a warning that vanishes when there's nothing to
// report. Per-entity grounding-status detail (which requires the not-yet-built
// GET /jobs/{job_id}/entities endpoint) is a separate task, not built here.
export function UnverifiedBadge({ count }) {
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
export function ConfidenceBadge({ confidence }) {
  if (!confidence) return null
  return <span className={`confidence confidence-${confidence}`}>{confidence} confidence</span>
}

export function GroundingIssuesList({ issues }) {
  if (!issues || issues.length === 0) return null
  return (
    <ul className="grounding-issues">
      {issues.map((issue, index) => <li key={index}>{issue}</li>)}
    </ul>
  )
}

export const RISK_NODE_STYLE = {
  low: { border: 'var(--ring)', background: 'rgba(160,161,207,.14)', color: 'var(--emphasis)' },
  medium: { border: 'var(--warning)', background: 'rgba(255,207,122,.14)', color: 'var(--warning)' },
  high: { border: '#ff5c4d', background: 'rgba(255,92,77,.16)', color: 'var(--danger)' },
}
export const DEFAULT_RISK_NODE_STYLE = { border: 'rgba(var(--neutral-rgb),.22)', background: 'rgba(var(--neutral-rgb),.05)', color: 'var(--emphasis)' }

// Rendered-node cap (module nodes + entity nodes) — once exceeded, entities in the
// least-recently-active modules collapse back down to just their module node with a
// count badge, so the canvas stays legible instead of growing forever.
export const LIVE_TREE_NODE_CAP = 70
