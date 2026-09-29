// Shared horizontal stepper, extracted from main.jsx (tasks.md "Live workflow run
// view" — "Generalize main.jsx::ProgressStages to accept precomputed stage states and
// reuse the .job-stage* CSS"). Moved into its own file, not just generalized in place,
// so frontend/src/workflow/WorkflowRun.jsx can import the generalized piece without a
// circular import back through main.jsx (main.jsx itself imports WorkflowRun once it's
// wired into JobStatusPage, a later task).
//
// Two entry points share the same `.job-stage*` markup/CSS:
// - ProgressStages: the original job/planning/execution-status-poll usage (JobStatusPage,
//   PlanningJobStatusPage, ExecutionStatusPage) — derives each stage's state from one
//   current `progressStage` key plus the overall `status`.
// - StageList: takes stages that already carry their own resolved `state`
//   (pending/active/complete/failed) per stage — what workflowReducer.js's
//   `state.stages` already is, since a live event stream resolves each stage
//   individually rather than from one current key.

import { jobStageState } from './jobStageState.js'

export { jobStageState }

export function StageList({ stages, label }) {
  return (
    <ol className="job-stages" aria-label={label}>
      {stages.map((stage) => (
        <li
          key={stage.key}
          className={`job-stage job-stage-${stage.state}`}
          aria-current={stage.state === 'active' || stage.state === 'failed' ? 'step' : undefined}
        >
          <span className="job-stage-dot" aria-hidden="true" />
          {stage.label}
        </li>
      ))}
    </ol>
  )
}

export function ProgressStages({ stages, status, progressStage, label }) {
  return (
    <StageList
      stages={stages.map((stage, index) => ({ ...stage, state: jobStageState(stages, index, status, progressStage) }))}
      label={label}
    />
  )
}
