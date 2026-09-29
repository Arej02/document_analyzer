/** Loading / empty / error messaging. `tone="danger"` is announced as an alert, everything else as status. */
export function StatePanel({ tone, icon, title, children, actions }) {
  return (
    <div className="ui-state-panel" data-tone={tone} role={tone === 'danger' ? 'alert' : 'status'}>
      <span className="ui-state-icon" aria-hidden="true">{icon}</span>
      <h2>{title}</h2>
      <p>{children}</p>
      {actions ? <div className="ui-state-actions">{actions}</div> : null}
    </div>
  )
}
