// Loading, empty and error states for the plan document (tasks_frontend.md A2.5). The retry / generate
// action is passed in: it is the existing `GeneratePlanAction` (POST /projects/{id}/plans), which still
// lives in `main.jsx`, so these states never issue a request of their own.
import { CircleX, FileText } from 'lucide-react'
import { StatePanel } from '../ui/StatePanel.jsx'
import { useOutlineSlot } from '../OutlineSlot.jsx'
import PlanDocument from './PlanDocument.jsx'
import './agent2.css'

const Lines = ({ n, widths = ['100%', '92%', '96%', '70%'] }) =>
  Array.from({ length: n }, (_, i) => <div key={i} className="a2-skel" style={{ height: 12, width: widths[i % widths.length] }} />)

/** Same three columns as the finished document, so nothing jumps when the plan arrives. */
export function PlanSkeleton() {
  // The outline lives in the app sidebar when there is one, so the placeholder has no outline column then either.
  const slot = useOutlineSlot()
  return (
    <div aria-busy="true" aria-label="Loading migration plan" role="status">
      <PlanDocument
        outline={slot.el ? null : <div className="a2-skel-col a2-skel-outline"><Lines n={10} widths={['80%', '60%', '70%', '55%']} /></div>}
        rail={<div className="a2-skel-col"><div className="a2-skel" style={{ height: 120 }} /><div className="a2-skel" style={{ height: 88 }} /></div>}
      >
        <div className="a2-skel-col">
          <div className="a2-skel" style={{ height: 28, width: '55%' }} />
          <Lines n={5} />
          <div className="a2-skel" style={{ height: 160 }} />
          <Lines n={4} />
        </div>
      </PlanDocument>
      <span className="a2-sr">Loading migration plan</span>
    </div>
  )
}

/** No plan for this project yet. `action` is the Generate plan control. */
export function PlanEmpty({ action }) {
  return (
    <StatePanel icon={<FileText size={20} />} title="No migration plan yet" actions={action}>
      A plan is built from a completed repository analysis. Run an analysis first, then generate the plan to review its phases, risks and open decisions here.
    </StatePanel>
  )
}

/** Planning failed, or the plan could not be loaded. `cause` is shown as the backend gave it. */
export function PlanFailed({ cause, action, title = 'Planning failed' }) {
  return (
    <StatePanel tone="danger" icon={<CircleX size={20} />} title={title} actions={action}>
      {cause || 'An unknown error occurred.'} Nothing was changed; the analysis is unaffected.
    </StatePanel>
  )
}
