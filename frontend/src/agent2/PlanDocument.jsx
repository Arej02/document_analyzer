// Three-column plan document shell: outline | document | rail (tasks_frontend.md A2.2).
// Layout only. The outline, sections and rail cards are passed in by the caller.
import './agent2.css'

/**
 * `outline` and `rail` are optional nodes. Without an `outline` (it lives in the app sidebar) the shell has two columns. `glance` is the same content as `rail`, rendered inline
 * at the top of the document; it is only shown below 1280px, where the rail is hidden.
 */
export default function PlanDocument({ outline, rail, glance, header, children, label = 'Migration plan' }) {
  return (
    <div className="a2">
      <div className="a2-shell" data-rail={rail ? 'on' : 'off'} data-outline={outline ? 'on' : 'off'}>
        {outline}
        <div className="a2-doc" id="plan-document" role="region" aria-label={label}>
          {header}
          {glance ? <div className="a2-glance-inline">{glance}</div> : null}
          {children}
        </div>
        {rail ? <aside className="a2-rail" aria-label="Plan at a glance">{rail}</aside> : null}
      </div>
    </div>
  )
}
