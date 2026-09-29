// "Generated from <commit> <time ago>" for the outline footer (tasks_frontend.md A2.2).
// Both inputs are optional: an archive upload has no commit, and a plan may not carry a timestamp yet.
const UNITS = [['year', 31536000], ['month', 2592000], ['day', 86400], ['hour', 3600], ['minute', 60]]

export function timeAgo(iso, now = Date.now()) {
  const then = Date.parse(iso)
  if (!Number.isFinite(then)) return null
  const sec = Math.max(0, Math.round((now - then) / 1000))
  if (sec < 60) return 'just now'
  const [unit, size] = UNITS.find(([, s]) => sec >= s)
  const n = Math.floor(sec / size)
  return `${n} ${unit}${n === 1 ? '' : 's'} ago`
}

export const shortSha = (sha) => (typeof sha === 'string' && /^[0-9a-f]{7,40}$/i.test(sha) ? sha.slice(0, 7) : null)

/** `{commit, ago}` pieces for the footer, or null when there is nothing honest to say. */
export function provenance({ commitSha, generatedAt } = {}, now = Date.now()) {
  const commit = shortSha(commitSha)
  const ago = generatedAt ? timeAgo(generatedAt, now) : null
  return commit || ago ? { commit, ago } : null
}
