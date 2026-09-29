import { Ban, CircleAlert, CircleCheck, CircleDashed, CircleX, LoaderCircle, TriangleAlert } from 'lucide-react'

/** Status vocabulary shared by phases and pills: icon + label + tone, never colour alone. */
export const STATUS = {
  queued: { label: 'Queued', tone: 'neutral', Icon: CircleDashed },
  in_progress: { label: 'In progress', tone: 'active', Icon: LoaderCircle, spin: true },
  completed: { label: 'Completed', tone: 'success', Icon: CircleCheck },
  warning: { label: 'Completed with warning', tone: 'warning', Icon: TriangleAlert },
  failed: { label: 'Failed', tone: 'danger', Icon: CircleX },
  cancelled: { label: 'Cancelled', tone: 'neutral', Icon: Ban },
  attention: { label: 'Needs attention', tone: 'warning', Icon: CircleAlert },
}

export function StatusIcon({ status, size = 16 }) {
  const { Icon, spin } = STATUS[status]
  return <Icon size={size} className={`ui-icon${spin ? ' ui-spin' : ''}`} aria-hidden="true" />
}

export function StatusPill({ status, label }) {
  const s = STATUS[status]
  return (
    <span className="ui-pill" data-tone={s.tone}>
      <StatusIcon status={status} size={14} />
      {label ?? s.label}
    </span>
  )
}
