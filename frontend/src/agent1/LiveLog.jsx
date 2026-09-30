// Collapsible live log over the workflow_events activity stream (buildRunView().log).
// Follows the newest line until the user scrolls up; a button then resumes following.
import { forwardRef, useCallback, useEffect, useId, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react'
import { ArrowDown, ChevronDown, ChevronRight, CircleX, Copy, TriangleAlert } from 'lucide-react'
import { MAX_ACTIVITY_ENTRIES } from '../workflow/workflowReducer.js'
import './agent1.css'

// The reducer already keeps only the newest MAX_ACTIVITY_ENTRIES and reports the rest as hiddenCount.
export const MAX_LOG_LINES = MAX_ACTIVITY_ENTRIES

const clock = (iso) => {
  const d = iso ? new Date(iso) : null
  return d && !Number.isNaN(d.getTime()) ? d.toLocaleTimeString([], { hour12: false }) : ''
}
const refText = (ref) => (ref && typeof ref === 'object' ? ref.value : String(ref))
export const lineText = (e) =>
  `${clock(e.created_at)} ${e.level.toUpperCase().padEnd(5)} ${e.message ?? ''}${e.refs?.length ? ` ${e.refs.map(refText).join(' ')}` : ''}`

/**
 * `log` is buildRunView().log ({ entries, hiddenCount }); `onCopy(text, label)` is useCopy's copy.
 * A ref exposes `reveal()`: expand the log, scroll it into view and focus it (the "View logs" control).
 */
export const LiveLog = forwardRef(function LiveLog({ log, onCopy, title = 'Live log' }, ref) {
  const entries = log.entries.slice(-MAX_LOG_LINES)
  const bodyId = useId()
  const [open, setOpen] = useState(true)
  const [following, setFollowing] = useState(true)
  const [unseen, setUnseen] = useState(0)
  const bodyRef = useRef(null)
  const panelRef = useRef(null)
  const lastCount = useRef(entries.length)

  useImperativeHandle(ref, () => ({
    reveal() {
      setOpen(true)
      // Wait a frame so a just-expanded body exists before it is scrolled to and focused.
      requestAnimationFrame(() => {
        panelRef.current?.scrollIntoView({ block: 'center' })
        bodyRef.current?.focus({ preventScroll: true })
      })
    },
  }))

  const toEnd = useCallback(() => {
    const el = bodyRef.current
    if (el) el.scrollTop = el.scrollHeight
    setFollowing(true)
    setUnseen(0)
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
    <section className="log" ref={panelRef} aria-label={title}>
      <header className="log-head">
        <button type="button" className="log-toggle" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((o) => !o)}>
          {open ? <ChevronDown size={16} className="ui-icon" aria-hidden="true" /> : <ChevronRight size={16} className="ui-icon" aria-hidden="true" />}
          <span className="log-title">{title}</span>
          <span className="log-count tnum">{entries.length + log.hiddenCount} events</span>
        </button>
        <span className="log-follow">{open ? (following ? 'Following latest' : 'Auto-scroll paused') : ''}</span>
        <button type="button" className="log-btn" onClick={() => onCopy(entries.map(lineText).join('\n'), 'Log copied')}>
          <Copy size={14} className="ui-icon" aria-hidden="true" />Copy log
        </button>
      </header>
      {open ? (
        <div className="log-wrap">
          {/* aria-live off: a busy log must not be read out line by line; phase changes are announced elsewhere. */}
          <div id={bodyId} ref={bodyRef} className="log-body" role="log" aria-live="off" aria-label="Analysis log" tabIndex={0} onScroll={onScroll}>
            {log.hiddenCount > 0 ? (
              <p className="log-hidden">{log.hiddenCount} earlier {log.hiddenCount === 1 ? 'event' : 'events'} hidden</p>
            ) : null}
            {entries.length === 0 ? <p className="log-empty">Waiting for the worker to emit events.</p> : entries.map((e) => (
              <div key={e.id} className="log-line" data-level={e.level}>
                <span className="log-time tnum">{clock(e.created_at)}</span>
                <span className="log-level">
                  {e.level === 'warn' ? <TriangleAlert size={12} className="ui-icon" aria-hidden="true" /> : null}
                  {e.level === 'error' ? <CircleX size={12} className="ui-icon" aria-hidden="true" /> : null}
                  {e.level}
                </span>
                <span className="log-msg">
                  {e.message}
                  {e.refs?.map((ref, i) => <code key={i} className="log-ref">{refText(ref)}</code>)}
                </span>
              </div>
            ))}
          </div>
          {!following ? (
            <button type="button" className="log-resume" onClick={toEnd}>
              <ArrowDown size={14} className="ui-icon" aria-hidden="true" />Resume auto-scroll{unseen ? ` (${unseen} new)` : ''}
            </button>
          ) : null}
        </div>
      ) : null}
    </section>
  )
})
