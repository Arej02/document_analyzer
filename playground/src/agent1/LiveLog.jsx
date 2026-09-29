// Collapsible live log. Auto-follows the tail until the user scrolls up; a button resumes following.
import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react'
import { ArrowDown, ChevronDown, ChevronRight, CircleX, Copy, TriangleAlert } from 'lucide-react'
import { clockAt } from './runModel.js'

const lineText = (e) => `${clockAt(e.t)} ${e.level.toUpperCase().padEnd(5)} ${e.verb}${e.text ? ` ${e.text}` : ''}${e.path ? ` ${e.path}` : ''}`

const LiveLog = forwardRef(function LiveLog({ entries, onCopy }, ref) {
  const [open, setOpen] = useState(true)
  const [following, setFollowing] = useState(true)
  const [unseen, setUnseen] = useState(0)
  const bodyRef = useRef(null)
  const lastCount = useRef(entries.length)
  const panelRef = useRef(null)

  useImperativeHandle(ref, () => ({
    reveal() {
      setOpen(true)
      requestAnimationFrame(() => { panelRef.current?.scrollIntoView({ block: 'center' }); bodyRef.current?.focus({ preventScroll: true }) })
    },
  }))

  const toEnd = useCallback(() => {
    const el = bodyRef.current
    if (el) el.scrollTop = el.scrollHeight
    setFollowing(true); setUnseen(0)
  }, [])

  useLayoutEffect(() => {
    const added = entries.length - lastCount.current
    lastCount.current = entries.length
    if (added < 0) { toEnd(); return }
    if (following) { const el = bodyRef.current; if (el) el.scrollTop = el.scrollHeight }
    else if (added > 0) setUnseen((n) => n + added)
  }, [entries.length, following, toEnd])

  useEffect(() => { if (open) toEnd() }, [open, toEnd])

  const onScroll = () => {
    const el = bodyRef.current
    if (!el) return
    const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 24
    setFollowing(atEnd)
    if (atEnd) setUnseen(0)
  }

  return (
    <section className="card log" ref={panelRef} aria-labelledby="log-title">
      <header className="log-head">
        <button type="button" className="log-toggle" aria-expanded={open} aria-controls="log-body" onClick={() => setOpen((o) => !o)}>
          {open ? <ChevronDown size={16} className="icon" aria-hidden="true" /> : <ChevronRight size={16} className="icon" aria-hidden="true" />}
          <h2 id="log-title">Live log</h2>
          <span className="muted tnum">{entries.length} events</span>
        </button>
        <span className="log-follow muted" aria-live="polite">{open ? (following ? 'Following latest' : 'Auto-scroll paused') : ''}</span>
        <button type="button" className="btn btn-sm" onClick={() => onCopy(entries.map(lineText).join('\n'), 'Log copied')}>
          <Copy size={14} className="icon" aria-hidden="true" />Copy log
        </button>
      </header>
      {open ? (
        <div className="log-wrap">
          <div id="log-body" ref={bodyRef} className="log-body" role="log" aria-label="Analysis log" tabIndex={0} onScroll={onScroll}>
            {entries.length === 0 ? <p className="muted log-empty">Waiting for the worker to emit events.</p> : entries.map((e, i) => (
              <div key={i} className="log-line" data-level={e.level}>
                <span className="log-time tnum">{clockAt(e.t)}</span>
                <span className="log-level">
                  {e.level === 'warn' ? <TriangleAlert size={12} className="icon" aria-hidden="true" /> : e.level === 'error' ? <CircleX size={12} className="icon" aria-hidden="true" /> : null}
                  {e.level === 'info' ? 'info' : e.level}
                </span>
                <span className="log-msg"><strong>{e.verb}</strong>{e.text ? ` ${e.text}` : ''}{e.path ? <> <code className="chip">{e.path}</code></> : null}</span>
              </div>
            ))}
          </div>
          {!following ? (
            <button type="button" className="btn btn-sm btn-primary log-resume" onClick={toEnd}>
              <ArrowDown size={14} className="icon" aria-hidden="true" />Resume auto-scroll{unseen ? ` (${unseen} new)` : ''}
            </button>
          ) : null}
        </div>
      ) : null}
    </section>
  )
})
export default LiveLog
