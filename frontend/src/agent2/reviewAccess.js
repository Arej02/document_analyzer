// What a reviewer may do on a plan, in one place (tasks_frontend.md A2.4, design_agent2.md §9, §11).
// The rules mirror the backend, which stays the authority (it answers 409 otherwise):
//   submit    only a `draft`                              (POST /submit)
//   decide    only an `in_review` plan   (approve/reject) (POST /phases/{id}/approval)
//   override  only a `draft` or `in_review` plan          (POST /overrides: merge, reorder, facade_ack; each makes a new version)
//   stale     nothing at all, whatever the status         (built on an older ingestion run)
// The plan is never hidden. A control that cannot act is disabled and says why (`reasons`).

export function reviewAccess(plan) {
  if (!plan) return { state: 'loading', readOnly: true, canSubmit: false, canDecide: false, canOverride: false, banner: null, reasons: {} }
  const stale = Boolean(plan.is_stale) || plan.status === 'stale'
  const status = plan.status
  const none = 'Not available on a stale plan.'
  const canSubmit = !stale && status === 'draft'
  const canDecide = !stale && status === 'in_review'
  const canOverride = !stale && (status === 'draft' || status === 'in_review')

  const reasonFor = (allowedStatus, verb) => {
    if (stale) return none
    if (status === 'approved') return 'This plan is approved and cannot be changed. Generate a new plan to change it.'
    if (status === 'rejected') return 'This plan was rejected. Open another version or generate a new plan.'
    if (allowedStatus === 'draft' && status === 'in_review') return 'This plan is already in review.'
    if (allowedStatus === 'in_review' && status === 'draft') return `Submit this draft for review before you ${verb}.`
    return null
  }

  let banner = null
  if (stale) {
    banner = { key: 'stale', tone: 'warning', title: 'This plan is out of date', text: 'It was built on an older ingestion run. It stays readable, but it cannot be submitted, approved or edited. Generate a new plan from the project page.' }
  } else if (status === 'approved') {
    banner = { key: 'approved', tone: 'success', title: 'Approved', text: 'An approved plan does not change. Generate a new plan to make changes.' }
  } else if (status === 'rejected') {
    banner = { key: 'rejected', tone: 'danger', title: 'Rejected', text: 'A phase was rejected, so this version is closed. Open another version or generate a new plan.' }
  } else if (status === 'draft') {
    banner = { key: 'draft', tone: 'neutral', title: 'Draft', text: 'You can restructure units below. Each change makes a new version. Submit for approval when it looks right.' }
  }

  return {
    state: stale ? 'stale' : status,
    readOnly: !canOverride && !canDecide && !canSubmit,
    canSubmit,
    canDecide,
    canOverride,
    banner,
    reasons: {
      submit: canSubmit ? null : reasonFor('draft', 'submit'),
      decide: canDecide ? null : reasonFor('in_review', 'approve or reject'),
      override: canOverride ? null : reasonFor('override', 'edit'),
    },
  }
}
