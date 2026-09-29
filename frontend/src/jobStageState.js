// Which state a stage is in, from one current `progressStage` key plus the overall `status`.
// Plain JS (no JSX) so pure modules and their tests can import it; ProgressStages.jsx re-exports it.
export function jobStageState(stages, index, status, progressStage) {
  if (status === 'done') return 'complete'
  const currentIndex = stages.findIndex((stage) => stage.key === progressStage)
  if (currentIndex === -1) return 'pending' // queued, or failed before any stage was recorded
  if (index < currentIndex) return 'complete'
  if (index === currentIndex) return status === 'failed' ? 'failed' : 'active'
  return 'pending'
}
