import { runAnnouncement } from './announcement.js'
import './agent1.css'

/** The one polite live region for the run view. Its text only changes on a phase or status change. */
export function PhaseAnnouncer({ view }) {
  return <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">{runAnnouncement(view)}</p>
}
