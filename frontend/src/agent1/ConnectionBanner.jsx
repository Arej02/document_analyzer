import { WifiOff } from 'lucide-react'
import './agent1.css'

/** Shown by the run view when `useJobRun().connection.lost`. The last known state stays on screen underneath. */
export function ConnectionBanner({ onRetry }) {
  return (
    <p className="connection-banner" role="status">
      <WifiOff size={16} className="ui-icon" aria-hidden="true" />
      <span>Connection lost, retrying. What you see may be out of date.</span>
      <button type="button" className="log-btn" onClick={onRetry}>Retry now</button>
    </p>
  )
}
