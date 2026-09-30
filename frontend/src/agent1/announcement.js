// Screen-reader text for the run. It depends only on the run status and the current phase,
// never on percentage, elapsed time or log lines, so it is identical between ticks and the
// live region announces only when the phase (or run status) actually changes.

export function runAnnouncement(view) {
  const { status, current } = view
  if (status === 'done') return 'Analysis complete.'
  if (status === 'cancelled') return 'Analysis cancelled.'
  if (status === 'failed') return current ? `Analysis failed during ${current.label}.` : 'Analysis failed.'
  if (current) return `Step ${current.step} of ${current.total}: ${current.label}.`
  return 'Analysis queued.'
}
