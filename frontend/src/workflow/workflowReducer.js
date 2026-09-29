// Live workflow run view — design.md §2, §4; tasks.md "Live workflow run view".
//
// Pure (state, event) -> state reducer over GET /api/agent1/jobs/{job_id}/events'
// `events[]` (any agent's `workflow_events` rows — this file is agent-agnostic, per
// design.md §2's "Agent 1 is the first producer. Agents 2 and 3 plug in later without
// UI changes"). No fetching, no timers, no DOM — those live in useWorkflowEvents and
// WorkflowRun.jsx, so this module stays trivially testable with node:test (no React,
// no jsdom) and reusable by both.
//
// Event fields (`event_type`, `stage`, `message`, `data`, `created_at`) are kept in
// their original API-response snake_case, matching how the rest of the frontend reads
// API responses (e.g. `job.progress_stage`, `summary.unverified_count` in main.jsx) —
// only the reducer's own invented state fields (`startedAt`, `finishedAt`, ...) are
// camelCase.

// design.md §2: activity/output/stats/stage.* events pile up fast during entity-summary
// fan-out — keep only the most recent ones and say how many were dropped, rather than
// growing the feed unboundedly for a long-running job.
export const MAX_ACTIVITY_ENTRIES = 500

export function createInitialWorkflowState() {
  return {
    // 'queued' (no workflow.started event seen yet) mirrors design.md §4's job status
    // enum (queued|running|done|failed) — a run with no events yet really is still
    // queued, since worker/run.py only emits workflow.started once it dequeues the job.
    status: 'queued',
    startedAt: null,
    finishedAt: null,
    error: null,
    // Ordered [{key, label, state}], state one of pending|active|complete|failed
    // (main.jsx's `.job-stage-${state}` CSS vocabulary) — empty until workflow.started
    // declares the stage list (design.md §2's "the workflow declares its own stages").
    stages: [],
    activity: { entries: [], hiddenCount: 0 },
    // Keyed by output_id, updated in place; outputOrder is first-seen order, since a
    // plain object's key order isn't something callers should have to rely on.
    outputs: {},
    outputOrder: [],
    // A flat, ever-merged {name: number} map — design.md §2: "the UI renders whatever
    // keys arrive", so nothing here is specific to any one agent's stat names.
    stats: {},
    // Latest progress.updated per stage key: { [stage]: { done, total, unit, currentPath } }.
    progress: {},
    // The last event id folded in, for dedup below — 0 sorts before every real
    // bigserial id, so the very first event is never mistaken for a duplicate.
    lastEventId: 0,
  }
}

function withStage(stages, key, state) {
  return stages.map((stage) => (stage.key === key ? { ...stage, state } : stage))
}

// Called once per event; not exported on its own — callers fold a batch with
// applyWorkflowEvents below, since that's the shape a poll response actually delivers.
function applyOne(state, event) {
  // Cursor pagination guarantees ids only increase and never repeat across polls, but
  // dedup here anyway (a caller retrying a request, React effects re-running, ...)
  // rather than trusting every caller to get that right.
  if (!event || event.id == null || event.id <= state.lastEventId) return state
  const next = { ...state, lastEventId: event.id }

  switch (event.event_type) {
    case 'workflow.started':
      return {
        ...next,
        status: 'running',
        startedAt: event.created_at,
        stages: (event.data?.stages || []).map((stage) => ({ key: stage.key, label: stage.label, state: 'pending' })),
      }

    case 'workflow.completed':
      return {
        ...next,
        status: 'done',
        finishedAt: event.created_at,
        // Defensive, not just decorative: a job with e.g. zero entities never opens
        // its last stage at all (worker/run.py only calls emitter.stage("fold") when
        // there were entity_outcomes to fold) — without this, that stage would stay
        // "pending" forever on an otherwise-done run.
        stages: next.stages.map((stage) => (stage.state === 'complete' ? stage : { ...stage, state: 'complete' })),
      }

    case 'workflow.failed':
      return {
        ...next,
        status: 'failed',
        finishedAt: event.created_at,
        error: event.data?.error ?? null,
        // Only the stage that was actually open when it failed is marked — earlier
        // stages are already 'complete' and later ones correctly stay 'pending'
        // (never reached), matching WorkflowEventEmitter.fail()'s own contract.
        stages: event.stage ? withStage(next.stages, event.stage, 'failed') : next.stages,
      }

    case 'workflow.cancelled':
      // design.md §4 (cancel): the open stage never completed and is marked cancelled; earlier
      // stages stay complete and later ones pending.
      return {
        ...next,
        status: 'cancelled',
        finishedAt: event.created_at,
        stages: event.stage ? withStage(next.stages, event.stage, 'cancelled') : next.stages,
      }

    case 'stage.started':
      return { ...next, stages: withStage(next.stages, event.stage, 'active') }

    case 'stage.completed':
      return { ...next, stages: withStage(next.stages, event.stage, 'complete') }

    case 'activity': {
      const entry = {
        id: event.id,
        stage: event.stage,
        message: event.message,
        level: event.data?.level || 'info',
        refs: event.data?.refs || [],
        created_at: event.created_at,
      }
      const entries = [...next.activity.entries, entry]
      let hiddenCount = next.activity.hiddenCount
      while (entries.length > MAX_ACTIVITY_ENTRIES) {
        entries.shift()
        hiddenCount += 1
      }
      return { ...next, activity: { entries, hiddenCount } }
    }

    case 'output.created':
    case 'output.updated': {
      const outputId = event.data?.output_id
      if (!outputId) return next
      const isNew = !(outputId in next.outputs)
      return {
        ...next,
        outputs: { ...next.outputs, [outputId]: { ...event.data } },
        outputOrder: isNew ? [...next.outputOrder, outputId] : next.outputOrder,
      }
    }

    case 'progress.updated': {
      const { done, total, unit, current_path: currentPath } = event.data || {}
      if (!event.stage || !Number.isFinite(done) || !Number.isFinite(total)) return next
      // The summarizer's threads can emit out of order: never let a stage's count go backwards.
      if (next.progress[event.stage] && next.progress[event.stage].done > done) return next
      return { ...next, progress: { ...next.progress, [event.stage]: { done, total, unit: unit ?? '', currentPath: currentPath ?? null } } }
    }

    case 'stats.updated':
      return { ...next, stats: { ...next.stats, ...(event.data || {}) } }

    default:
      // Forward-compatible: an event type this frontend build doesn't know how to
      // render yet still advances lastEventId (via `next`) so it isn't reprocessed,
      // but otherwise leaves state untouched rather than throwing.
      return next
  }
}

export function workflowReducer(state, event) {
  return applyOne(state, event)
}

// Folds a whole poll response's events[] in order — what useWorkflowEvents actually
// calls; workflowReducer/applyOne stay the single-event primitive underneath.
export function applyWorkflowEvents(state, events) {
  return (events || []).reduce(applyOne, state)
}
