const pad2 = (n) => String(Math.floor(n)).padStart(2, '0')

export const formatInt = (n) => Math.round(n).toLocaleString('en-US')

/** Elapsed time as a clock: `3:07`, or `1:02:03` past an hour. */
export function formatClock(sec) {
  const s = Math.max(0, Math.floor(sec))
  const h = Math.floor(s / 3600)
  return h ? `${h}:${pad2((s % 3600) / 60)}:${pad2(s % 60)}` : `${Math.floor(s / 60)}:${pad2(s % 60)}`
}

/** Duration in words: `42s`, `3m 07s`. */
export function formatDuration(sec) {
  const s = Math.max(0, Math.round(sec))
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${pad2(s % 60)}s`
}
