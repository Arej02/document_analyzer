// Collapsed-section state for the plan document. Pure helpers plus one hook.
import { useCallback, useState } from 'react'

export function toggled(set, id) {
  const next = new Set(set)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  return next
}
export function opened(set, id) {
  if (!set.has(id)) return set
  const next = new Set(set)
  next.delete(id)
  return next
}
export const allCollapsed = (set, ids) => ids.length > 0 && ids.every((id) => set.has(id))

/** `ids` are the section ids. `open(id)` is meant for `useOutline`'s `beforeNavigate`. */
export function useCollapsed(ids) {
  const [collapsed, setCollapsed] = useState(() => new Set())
  const toggle = useCallback((id) => setCollapsed((s) => toggled(s, id)), [])
  const open = useCallback((id) => setCollapsed((s) => opened(s, id)), [])
  const all = allCollapsed(collapsed, ids)
  const toggleAll = useCallback(() => setCollapsed(all ? new Set() : new Set(ids)), [all, ids])
  return { collapsed, toggle, open, allCollapsed: all, toggleAll }
}
