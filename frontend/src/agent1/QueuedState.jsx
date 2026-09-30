import { queueText } from './queueText.js'
import './agent1.css'

/**
 * Loading / queued: the job exists but no worker has started it (or the first poll is still in flight).
 * `position` is the API's `queue_position`; nothing about the queue is shown when it is null.
 */
export function QueuedState({ loading = false, position = null }) {
  const ahead = loading ? null : queueText(position)
  return (
    <section className="queued" aria-busy="true" aria-label={loading ? 'Loading analysis' : 'Analysis queued'}>
      <p className="queued-title">{loading ? 'Loading analysis…' : 'Waiting for a worker.'}</p>
      {loading ? null : <p className="queued-note">{ahead ? `${ahead} ` : ''}Analysis starts as soon as a worker picks the job up. This page updates by itself.</p>}
      <div className="queued-skel" aria-hidden="true">
        <div className="skel" style={{ height: 40, width: 120 }} />
        <div className="skel" style={{ height: 12, width: '100%' }} />
        <div className="skel" style={{ height: 12, width: '78%' }} />
        <div className="skel" style={{ height: 12, width: '64%' }} />
      </div>
    </section>
  )
}
