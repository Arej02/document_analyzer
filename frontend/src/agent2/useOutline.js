// Outline state for the plan document: scrollspy, expanded groups, navigate-and-focus.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

// px from the viewport top: a heading above this line is "current". It sits below the 89px sticky site header
// (`--a2-header-h` in agent2.css) with some room, so a heading you can actually see counts as current.
const SPY_LINE = 130

export function outlineIds(sections) {
  return sections.flatMap((s) => [s.id, ...s.subs.map((x) => x.id)])
}
export function sectionOf(sections, id) {
  return sections.find((s) => s.id === id || s.subs.some((x) => x.id === id))?.id
}

/**
 * `beforeNavigate(sectionId)` lets the caller open a collapsed section before we scroll to it.
 * `enabled` is false while the plan isn't rendered (loading, empty, error).
 */
export function useOutline(sections, { enabled = true, beforeNavigate } = {}) {
  const ids = useMemo(() => outlineIds(sections), [sections])
  const [activeId, setActiveId] = useState(ids[0])
  const [seen, setSeen] = useState(0)
  const [expanded, setExpanded] = useState(() => new Set())
  const [tocOpen, setTocOpen] = useState(false)
  // A navigation click scrolls programmatically; the spy must not overwrite the clicked target mid-scroll.
  const lockUntil = useRef(0)

  useEffect(() => {
    if (!enabled) return undefined
    let raf = 0
    const run = () => {
      raf = 0
      if (performance.now() < lockUntil.current) return
      const present = ids.filter((id) => document.getElementById(id))
      if (!present.length) return
      let current = present[0]
      for (const id of present) if (document.getElementById(id).getBoundingClientRect().top <= SPY_LINE) current = id
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) current = present[present.length - 1]
      setActiveId(current)
      const idx = sections.findIndex((s) => s.id === sectionOf(sections, current))
      setSeen((n) => Math.max(n, idx))
    }
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(run) }
    run()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => { window.removeEventListener('scroll', onScroll); window.removeEventListener('resize', onScroll); cancelAnimationFrame(raf) }
  }, [enabled, ids, sections])

  const activeSection = sectionOf(sections, activeId)
  // Keep the active group open in the outline as the reader scrolls.
  useEffect(() => { if (activeSection) setExpanded((s) => (s.has(activeSection) ? s : new Set(s).add(activeSection))) }, [activeSection])

  const toggle = useCallback((id) => setExpanded((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n }), [])

  const navigate = useCallback((id) => {
    const section = sectionOf(sections, id)
    beforeNavigate?.(section)
    setExpanded((s) => (s.has(section) ? s : new Set(s).add(section)))
    setActiveId(id)
    setTocOpen(false)
    lockUntil.current = performance.now() + 600
    // Wait a frame so a just-opened section is in the DOM before we look for the target.
    requestAnimationFrame(() => {
      const el = document.getElementById(id)
      if (!el) return
      const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
      el.scrollIntoView({ block: 'start', behavior: reduce ? 'auto' : 'smooth' })
      const target = el.matches('[tabindex]') ? el : el.querySelector('h3[tabindex], h2 button') || el
      target.focus?.({ preventScroll: true })
    })
  }, [sections, beforeNavigate])

  return { activeId, activeSection, seen, expanded, toggle, navigate, tocOpen, toggleTocOpen: () => setTocOpen((o) => !o) }
}
