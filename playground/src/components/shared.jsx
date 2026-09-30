// Small shared primitives used by both screens and the tweak bar.
import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, CircleAlert, CircleCheck, CircleDashed, CircleX, LoaderCircle, TriangleAlert } from 'lucide-react'

export const pad2 = (n) => String(Math.floor(n)).padStart(2, '0')
export const fmtInt = (n) => Math.round(n).toLocaleString('en-US')
export function fmtClock(sec) {
  const s = Math.max(0, Math.floor(sec))
  const h = Math.floor(s / 3600)
  return h ? `${h}:${pad2((s % 3600) / 60)}:${pad2(s % 60)}` : `${Math.floor(s / 60)}:${pad2(s % 60)}`
}
export function fmtDuration(sec) {
  const s = Math.max(0, Math.round(sec))
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${pad2(s % 60)}s`
}

/** Copy text; returns a `copied` label that clears itself. Falls back for non-secure contexts. */
export function useCopy() {
  const [message, setMessage] = useState('')
  const timer = useRef()
  useEffect(() => () => clearTimeout(timer.current), [])
  const copy = useCallback(async (text, label = 'Copied') => {
    let ok = true
    try { await navigator.clipboard.writeText(text) } catch {
      const ta = Object.assign(document.createElement('textarea'), { value: text })
      ta.style.position = 'fixed'; ta.style.opacity = '0'
      document.body.appendChild(ta); ta.select()
      try { ok = document.execCommand('copy') } catch { ok = false }
      ta.remove()
    }
    setMessage(ok ? label : 'Copy failed. Select the text and copy manually.')
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setMessage(''), 2200)
  }, [])
  return [message, copy]
}

/** Live-region toast for copy confirmations. */
export function Toast({ message }) {
  return (
    <div role="status" aria-live="polite">
      {message ? <div className="toast"><Check size={14} className="icon" aria-hidden="true" />{message}</div> : null}
    </div>
  )
}

export function Segmented({ name, legend, value, options, onChange }) {
  return (
    <div className="seg" role="radiogroup" aria-label={legend}>
      {options.map((o) => (
        <label key={o.value}>
          <input type="radio" name={name} value={o.value} checked={value === o.value} onChange={() => onChange(o.value)} />
          {o.icon}{o.label}
        </label>
      ))}
    </div>
  )
}

/** Status vocabulary shared by phases and pills: icon + label + tone. */
export const STATUS = {
  queued: { label: 'Queued', tone: 'neutral', Icon: CircleDashed },
  in_progress: { label: 'In progress', tone: 'active', Icon: LoaderCircle, spin: true },
  completed: { label: 'Completed', tone: 'success', Icon: CircleCheck },
  warning: { label: 'Completed with warning', tone: 'warning', Icon: TriangleAlert },
  failed: { label: 'Failed', tone: 'danger', Icon: CircleX },
  attention: { label: 'Needs attention', tone: 'warning', Icon: CircleAlert },
}
export function StatusIcon({ status, size = 16 }) {
  const { Icon, spin } = STATUS[status]
  return <Icon size={size} className={`icon${spin ? ' spin' : ''}`} aria-hidden="true" />
}

export function StatePanel({ tone, icon, title, children, actions }) {
  return (
    <div className="state-panel" data-tone={tone} role={tone === 'danger' ? 'alert' : 'status'}>
      <span className="state-icon" aria-hidden="true">{icon}</span>
      <h2>{title}</h2>
      <p>{children}</p>
      {actions ? <div className="state-actions">{actions}</div> : null}
    </div>
  )
}
