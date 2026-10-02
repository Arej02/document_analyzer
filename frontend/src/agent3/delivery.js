// Pure helpers for the GitHub delivery action — design_agent3.md §15.8–§15.12. React-free so `node --test` can run them.

export const POLL_MS = 3000

const ACTIVE = ['queued', 'running']
export const isActive = (status) => ACTIVE.includes(status)

// Only ever link to a real GitHub pull request URL: the value comes from our own API, but it is built from
// what GitHub returned, so it is checked before becoming an href.
const PR_URL_RE = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+$/
export const safePrUrl = (url) => (typeof url === 'string' && PR_URL_RE.test(url) ? url : null)

// GET /executions/{id}/delivery lists newest first.
export const newestDelivery = (deliveries) => (Array.isArray(deliveries) && deliveries.length ? deliveries[0] : null)

// Why the Deliver button is disabled, or null when it can be pressed (same convention as DownloadProjectButton).
export const deliverBlocker = ({ hasOutput, connection, latest }) => {
  if (!hasOutput) return 'Delivery is available once this execution finishes.'
  if (!connection?.connected) return 'Connect a GitHub repository on the project page to deliver as pull requests.'
  if (latest && isActive(latest.status)) return 'A delivery to GitHub is already running.'
  return null
}

// The zip's git history and the pushed commits are the same objects only when the stack's root equalled the zip's
// baseline commit (design §15.14). Say so only when that was observed; otherwise say nothing rather than guess.
export const parityNote = (delivery) =>
  delivery?.status === 'done' && delivery.root_matches_baseline === true
    ? 'Commit hashes match the downloaded zip.'
    : null

export const prLabel = (pr) => (pr.kind === 'scaffold' ? 'Project scaffold' : `Phase ${pr.phase_order}${pr.label ? ` · ${pr.label}` : ''}`)
