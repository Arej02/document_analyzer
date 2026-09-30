// design.md §4: progress_stage is one of these three while status === 'running'
// (or null before extraction starts). Single source of truth for Agent 1's job-level
// stages: buildRunView's fallback phase list (before the first workflow event arrives) reads it.
export const JOB_PHASES = [
  { key: 'extracting', label: 'Extracting' },
  { key: 'summarizing', label: 'Summarizing' },
  { key: 'grounding', label: 'Grounding' },
]

// Relative cost of each stage, used for the overall percentage and the ETA until the
// backend reports real progress. Keys are the worker's declared stage keys
// (worker/run.py WORKFLOW_STAGES) plus the job-level fallback keys above. Only the
// ratios matter. A stage missing here counts as DEFAULT_STAGE_WEIGHT.
export const STAGE_WEIGHTS = {
  fetch: 5, filter: 3, snapshot: 7, extract: 20, evidence: 10, summarize: 45, fold: 10,
  extracting: 25, summarizing: 60, grounding: 15,
}
export const DEFAULT_STAGE_WEIGHT = 10
