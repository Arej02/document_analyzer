// GET /api/agent1/jobs/{id} `queue_position` (design.md §4): jobs ahead of this one, or null when unknown.
export function queueText(position) {
  if (!Number.isInteger(position) || position < 0) return null
  if (position === 0) return 'This job is next in line.'
  return `${position} ${position === 1 ? 'job is' : 'jobs are'} ahead of this one.`
}
