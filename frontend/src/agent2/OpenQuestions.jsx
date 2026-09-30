// Open questions body: each open facade cycle is a decision card. Acknowledging is the existing
// `facade_ack` override (a note is required, it makes a new plan version), supplied by the caller as
// `renderAcknowledge(card)` while the plan is editable; this component writes nothing.
import { CircleAlert, CircleCheck } from 'lucide-react'
import { openQuestions } from './openQuestionsModel.js'
import './agent2.css'

export default function OpenQuestions({ plan, onSelectUnit, renderAcknowledge }) {
  const { cards, openFlags, unlisted } = openQuestions(plan)
  if (!cards.length && !openFlags) {
    return <p className="a2-note"><CircleCheck size={14} className="ui-icon" aria-hidden="true" /> No open decisions. Every flagged dependency cycle is acknowledged or was merged away.</p>
  }
  return (
    <>
      <p>
        {openFlags} open facade {openFlags === 1 ? 'flag' : 'flags'}. Approval cannot complete until each flagged
        dependency cycle is acknowledged or overridden, even with every phase approved.
      </p>
      <ul className="a2-question-list" aria-label="Flagged dependency cycles">
        {cards.map((card) => (
          <li key={card.key} className="a2-card a2-question">
            <p className="a2-question-head">
              <span className="ui-pill" data-tone="warning"><CircleAlert size={14} className="ui-icon" aria-hidden="true" />Open</span>
              <strong>{card.level === 'unit' ? 'Units' : 'Phases'} depend on each other in a cycle</strong>
            </p>
            <p className="a2-cycle">
              {card.members.map((m, i) => (
                <span key={m.id}>
                  {i > 0 ? <span aria-hidden="true"> ↔ </span> : null}
                  {m.kind === 'unit' && onSelectUnit
                    ? <button type="button" className="a2-link-btn" onClick={() => onSelectUnit(m.id)}>{m.label}</button>
                    : <span>{m.label}</span>}
                </span>
              ))}
            </p>
            <p className="a2-note">Break the cycle with a facade both sides call, then acknowledge it with a note. Or merge the units into one.</p>
            {renderAcknowledge && card.acknowledgeable ? renderAcknowledge(card) : null}
            {renderAcknowledge && !card.acknowledgeable ? <p className="a2-note">This cycle has no id to acknowledge it by; merge its units instead.</p> : null}
          </li>
        ))}
      </ul>
      {unlisted ? <p className="a2-note">{unlisted} more open {unlisted === 1 ? 'flag is' : 'flags are'} not listed as a cycle (a cycle between units that also crosses phases counts at both levels).</p> : null}
    </>
  )
}
