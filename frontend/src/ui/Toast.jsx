import { Check } from 'lucide-react'

/** Polite live region; the region stays mounted so screen readers announce each new message. */
export function Toast({ message }) {
  return (
    <div role="status" aria-live="polite">
      {message ? <div className="ui-toast"><Check size={14} className="ui-icon" aria-hidden="true" />{message}</div> : null}
    </div>
  )
}
