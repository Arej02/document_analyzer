import { ScrollText } from 'lucide-react'
import './agent1.css'

/** "View logs": scrolls to the LiveLog and focuses it. `logRef` is the ref passed to <LiveLog ref={logRef} />. */
export function ViewLogsButton({ logRef }) {
  return (
    <button type="button" className="log-btn" onClick={() => logRef.current?.reveal()}>
      <ScrollText size={14} className="ui-icon" aria-hidden="true" />View logs
    </button>
  )
}
