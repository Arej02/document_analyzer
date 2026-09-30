# tasks_agent2.md — Agent 2 (Plan)

Rule for this file (same as `tasks.md`): **every backend task that adds or changes an `/api/agent2/...` endpoint has a paired frontend task directly under it**, so `frontend/` never drifts out of sync with what the backend can actually do. Don't check off a backend box until its paired frontend task is at least stubbed and can exercise the endpoint end-to-end. Backend tasks with no endpoint (worker stages, migrations) need no frontend pair; each says so explicitly.

Contracts live in `design_agent2.md` §11 (API) and §2 (tables). If a task needs a contract change, update the design doc first, then this file, then code (CLAUDE.md).

### Frontend conventions these tasks assume (from the existing `frontend/src/main.jsx`)
- **No router.** Pages are selected by query param, and `App`'s `navigateToJobStatus`/`navigateToProject`/`backToForm` each clear the others. Agent 2 adds `?planning_job=<id>` and `?plan=<id>` (optionally `&unit=<cluster_id>`) with matching `navigateToPlanningJob`/`navigateToPlan` helpers. All params stay mutually exclusive.
- Every call goes through `useApiFetch()` (attaches the Supabase JWT). Errors render through `formatJobErrorMessage`.
- Reuse, don't copy: the `ProgressStages` stepper (generalize its hardcoded `JOB_STAGES` into a `stages` prop), the `risk-level-*` pills, `UnverifiedBadge`, the `summary-loading`/`error` state classes, `StarfieldButton`'s `loading` prop, and React Flow (`@xyflow/react`, already a dependency, used by `LiveEntityTree`).
- Every new view ships with loading, empty and error states from the start. Don't leave them for a later audit, which is what Agent 1 Phase 6 had to fix.
- Visual check: `vite build`, plus a real browser pass against a mock `/api/agent2` server or the live backend (same approach as `tasks.md` Phases 2–5).

### Prerequisites (outside this file)
- At least one Agent 1 ingestion job must reach `status = done` against a real repo. This is currently blocked on the Azure OpenAI endpoint config (`tasks.md` Phase 4, "Verify a real end-to-end run"). Phases 0–4 can be built and tested against `tests/fakes.py::FakeClient` fixtures before then.
- The Phase 5 unit-detail view links members to Agent 1's entity detail view, which is still unchecked in `tasks.md` Phase 4 (`GET /api/agent1/jobs/{job_id}/entities/{entity_id}` frontend). Build that first, or render members as plain rows until it exists.

---

## Phase 0 — Foundations

- [x] **Blocking:** verify that `python-igraph`, `leidenalg` and `networkx` install on the repo's Python 3.14 (`uv add`) and pass a functional smoke test rather than just an import: a seeded Leiden run gives identical partitions twice, and `strongly_connected_components` + `lexicographical_topological_sort` work on a small digraph. If a wheel is missing, record the fallback decision in `skills.md` before building anything on it. No endpoint, so no frontend pair.
- [x] Migration `supabase/migrations/<ts>_agent2_planning_tables.sql`: `planning_jobs` (including the partial unique index "one queued/running per project"), `clusters`, `cluster_members`, `cluster_reports` (`embedding vector(384)`), `cluster_edges` (`design_agent2.md` §2). RLS enabled with `select` policies on `org_id = current_org_id()`. Apply it to the live project and verify columns and policies. No endpoint, so no frontend pair.
- [x] Migration `<ts>_agent2_plan_tables.sql`: `migration_plans`, `migration_plan_items`, `phase_approvals`, `plan_overrides`. Select policies on all four, and additionally `insert` policies (`org_id = current_org_id()`) on the two user-writable tables. No endpoint, so no frontend pair.
- [x] Migration `<ts>_agent2_pgmq_queue.sql`: `pgmq.create('agent2_planning_jobs')`. Add `enqueue_planning_job`/`read_next_planning_job`/`delete_planning_job_message` next to Agent 1's in `queue.py`, with the explicit `$2::bigint` cast (`design.md` §3 bug #2). Verify a live send/read/delete cycle. No endpoint, so no frontend pair.
- [x] Planner worker skeleton `backend/document_analyzer/agent2/worker/run.py` plus the `document-analyzer-planner` script in `pyproject.toml`: `load_dotenv()` at the top (`design.md` §3 bug #1), `assert_embedding_config_in_sync` at startup, a poll loop, message delete in `finally`, and `mark_planning_running`/`mark_planning_stage`/`mark_planning_failed`/`mark_planning_done` filtered by `id` + `org_id`. Stage functions are stubs for now. Add `tests/test_agent2_worker_run.py` covering ordering, failure marking `failed` while still deleting the message, and an unexpected error not crashing the loop. No endpoint, so no frontend pair.
- [x] Extend the live RLS read-path test (`tests/test_rls_multi_tenancy.py` or a sibling) to seed one row per Agent 2 table under org A and confirm org B sees nothing, with org A's positive control passing. No endpoint, so no frontend pair.

## Phase 1 — Trigger & planning-job status

- [x] `POST /api/agent2/projects/{project_id}/plans` (`design_agent2.md` §11): `agent2/router.py`, mounted in `app.py`. Optional body `{source_job_id}`, defaulting to `projects.latest_job_id`. `404` for a missing or other-org project. `409` if the source job isn't `done` or a planning job for the project is already `queued`/`running`. Freezes `params` from the §12 env defaults. Inserts `planning_jobs`, enqueues, returns `202 {planning_job_id, status}`. Tests: happy path, both 409s, 404, 401.

### Frontend sync — Generate plan
- [x] "Generate plan" action on `ProjectDetailPage`, below `ReingestAction`. Enabled only when `latest_job_status === 'done'`; when disabled, explain why ("finish an ingestion run first"). Uses `StarfieldButton`'s `loading` while submitting. Shows the backend's `409` message verbatim through `formatJobErrorMessage`. On `202`, calls `navigateToPlanningJob(planning_job_id)`.

- [x] `GET /api/agent2/planning-jobs/{planning_job_id}`: returns `{planning_job_id, project_id, status, progress_stage, error, plan_id}` (`project_id` added for the status page's "Try again"), with `plan_id` set once `done`. `404` for missing or other-org. Response dict rebuilt field by field (the `_entity_stub`/`get_project` lesson from `tasks.md`). Tests: each status, 404, 401.

### Frontend sync — Planning status
- [x] `PlanningJobStatusPage` (`?planning_job=`): polls every `JOB_POLL_INTERVAL_MS` until the job is terminal and cleans up on unmount. Uses the generalized `ProgressStages` with stages `graph → cluster → report → order → done`. The failed state shows `error` in red with a "Try again" button that goes back to the project page. On `done`, a "View plan" button calls `navigateToPlan(plan_id)`.

## Phase 2 — Graph build & clustering (worker only)

No endpoints in this phase, so no frontend pairs. Progress is already visible through Phase 1's status polling (`graph`/`cluster` stages).

- [x] Stage `graph` (`design_agent2.md` §3), `agent2/worker/graph.py`:
  - Loads entities, entity-level summaries and `calls`/`hook_binding` edges with non-null targets for `source_job_id`, filtered by `org_id`.
  - Aggregates per-pair counts and canonicalizes duplicates on `(file_path, name, entity_type)`.
  - Adds class↔method containment, splits hubs into the foundation set, and applies the §3.5 weight (dropping the similarity term when a summary is `unverified` or missing).
  - Writes `planning_jobs.graph_stats`.
  - Tests: duplicate collapse rewires edges; `include`/null-target edges ignored; the call count equals the row count; α=0 gives structure-only weights; an unverified endpoint drops similarity; a hub is removed; an isolated node is detected.
- [x] Stage `cluster` (§4), `agent2/worker/cluster.py`:
  - Seeded Leiden to convergence, with a recursive split above `max_unit_size` and oversized leftovers recorded in `graph_stats`.
  - Attaches isolated nodes by `file_path`, adds the foundation unit, and runs Leiden over the unit graph to get phases.
  - Persists `clusters`/`cluster_members` (every original entity id plus `canonical_entity_id`) with `insert_many_scoped`, deleting and rewriting for idempotency.
  - Tests: every unit ≤ cap (except foundation or recorded oversized); every unit has exactly one phase; a class is never split; same seed and input give identical output; a re-run leaves no duplicate rows.

## Phase 3 — Cluster reports (worker only)

No endpoint, so no frontend pair. Visible as the `report` stage in Phase 1 polling.

- [x] Stage `report` (§6), `agent2/worker/report.py`:
  - Unit reports are fanned out through a bounded pool (`AGENT2_REPORT_CONCURRENCY`). The payload holds members' type, name, file path and `summary_text` only, capped per §6.1. Phase reports then fan out through the same pool.
  - `risk_level`/`risk_factors` come from `rollup_risk`. Grounding goes through `check_grounding_with_retry`, where an exhausted cap stores `unverified` and never fails the job. Embeddings come from `embed_text`.
  - Tests: the payload has no `raw_facts` or other units' members; LLM-supplied risk is ignored; exhausted retries store `unverified` and the job continues; the concurrency cap is respected (peak in-flight count, same technique as `test_worker_summarize_fanout.py`); a malformed LLM response after retries fails the stage with a clear `error`.

## Phase 4 — Dependency graph, cycles, ordering, test contracts (worker only)

No endpoints, so no frontend pairs. The results are only visible once Phase 5's endpoints exist, which is why Phase 5 must follow directly.

- [x] Stage `order`, part 1 (§7), `agent2/worker/dependencies.py`: collapse directed edges (caller → callee, callback → hook) at unit and phase level; find SCCs; auto-merge an SCC inside one phase whose merged size is ≤ cap (new `kind = merged_scc` cluster, edges flagged `merged`); otherwise flag `needs_facade` (phase-level SCCs always are); condense. Tests: a 2-cycle and a 3-cycle both detected; a mergeable SCC gets merged; an oversized SCC and a cross-phase SCC are both flagged; re-running deletes and rewrites `merged_scc` clusters and `cluster_edges`.
- [x] Stage `order`, part 2 (§8), `agent2/worker/ordering.py`: phase topological sort, then unit sort within each phase, with the tie key `(foundation, risk low→high, size, id)`. Writes `migration_plans` v1 (`draft`, `open_facade_flags` computed) and `migration_plan_items`. Tests: a callee is ordered before its caller; the foundation is first; the order is deterministic; members of a flagged SCC get consecutive `unit_order` values.
- [x] Test-contract builder (§10), `agent2/worker/test_contract.py`, built from facts only (no LLM): public interface = members with a cross-unit incoming edge or hook binding, with `params` from `raw_facts`; incoming call sites; outgoing dependencies; `external_calls` from unresolved `raw_facts.calls`; `hooks_bound`; `side_effect_signals`; `unverified_member_ids`. Tests for each field.
- [x] Wire all four stages into the planner worker and run the full pipeline on a fixture repo with a known circular dependency (a PHP fixture under `tests/fixtures/` with a 3-function call cycle across files). Record the findings in `design_agent2.md` (§14 wiring, §15 findings).
- [x] Directory fallback for phases (§4 step 2): units with no unit edge are pooled into one phase per directory instead of one Leiden singleton phase each, with `graph_stats.directory_phases` counting them. `hospital_test` dry run: 46 phases → 12 (§15). Tests in `tests/test_agent2_cluster.py`. No endpoint, so no frontend pair.
- [ ] Once an Agent 1 job for a **WordPress** repo reaches `done` live, run the planner against it and inspect units and phases by hand to tune the specs §9 defaults. Record the findings in `design_agent2.md` §15. (2026-09-23: the only live `done` repo is the Laravel `hospital_test`. A read-only dry run over it is in §15, and its main finding, one phase per unit on a sparse graph, needs deciding before tuning.)

## Phase 5 — Plan review API + UI

Each endpoint below is followed directly by its frontend pair. Every plan read and write runs the §9.1 stale check first (`projects.latest_job_id ≠ source_job_id` marks the plan `stale`).

- [x] `GET /api/agent2/projects/{project_id}/plans`: `{plans: [{id, version, status, source_job_id, is_stale, created_at}]}`, newest first, including every version. `404` for missing or other-org. Tests: ordering, stale detection after a newer ingestion job becomes `latest_job_id`, org scoping, 401.

### Frontend sync — Plan list
- [x] "Plans" section on `ProjectDetailPage`, under "Run history": each row shows version, status pill and created time, with a distinct stale badge ("built on an older ingestion run"). A row click calls `navigateToPlan`. Empty state: "No plans yet — generate one above."

- [x] `GET /api/agent2/plans/{plan_id}`: overview with phases in `phase_order`, each carrying its report fields, approval state and units in `unit_order` (label, size, risk, grounding, flags), plus `open_facade_flags`. `404` for missing or other-org. Tests: shape, ordering, flags, stale-on-read.

### Frontend sync — Plan overview
- [x] `PlanPage` (`?plan=`):
  - Header: version, status and `open_facade_flags` count, plus a banner when the plan is stale.
  - Phases render as an ordered list, each with its report summary, a risk pill, `UnverifiedBadge` for an unverified report, and its approval state. Each phase expands to its ordered units.
  - A unit with a `needs_facade` edge shows a warning marker.
  - A unit click sets `&unit=<cluster_id>`.
  - Done in `main.jsx::PlanPage` (replaces `PlanPlaceholder`). `&unit=` sits alongside `?plan=` and is cleared by every other navigation. On load, the selected unit's phase is expanded and the unit is highlighted; the detail panel itself is the next task. Verified with `vite build` and in a browser against a mock `GET /plans/{id}` (with a fake local session): the in-review plan with flags, merged/unverified/unlabeled units, the approved and not-reviewed phases, `&unit=` surviving a reload, the stale banner with zero phases, and the 404 error.

- [x] `GET /api/agent2/plans/{plan_id}/units/{cluster_id}`: the report, members (`id`, `entity_type`, `name`, `file_path`, `grounding_status`), `test_contract` and incident `cluster_edges` with flags. `404` for a missing unit or a unit not in this plan. Tests.
  - Done in `router.py::build_unit_detail` (shape and details in `design_agent2.md` §11), with 8 tests in `tests/test_agent2_plans_api.py`.

### Frontend sync — Unit detail
- [x] Unit detail panel on `PlanPage` (`&unit=`):
  - The report, with a member table reusing `EntityBrowser`'s row styling. Each member links to Agent 1's entity detail view, or renders as plain text until that exists (see Prerequisites).
  - The `test_contract` as readable sections: public interface, incoming callers, external calls, hooks bound, side-effect signals, and the unverified members highlighted.
  - Incident edges, with `needs_facade` ones highlighted.
  - Done in `main.jsx::PlanUnitDetail`, shown beside the phases (stacked below them under 1100px) with a Close button that clears `&unit=`. Members are plain text, since Agent 1's entity detail view is still unbuilt. A merged unit shows its `constituent_reports`. The contract also shows outgoing dependencies, and every empty section says "None." Other units are named from the plan overview. Once loaded, the panel scrolls into view if it's off-screen. Verified with `vite build` and in a browser against the real router over `tests/fakes.py` seed data (not the live DB): a unit with its own report and a full contract, the merged unit's constituent reports and `merged` internal edge, an empty contract, Close, and a 390px width (stacked, no horizontal page scroll). That pass caught a singular/plural wording bug and a scroll that fired before load; both fixed.

- [x] `GET /api/agent2/plans/{plan_id}/graph?level=phase|unit` (optional `&phase_id=` to scope units to one phase): `{nodes: [{id, label, size, risk_level}], edges: [{from, to, weight, flag}]}`. `422` on an invalid `level`. Tests.
  - Done in `router.py::build_plan_graph` (details in `design_agent2.md` §11), with 12 tests in `tests/test_agent2_plans_api.py`.


### Frontend sync — Plan graph
- [x] `PlanGraph` component on `PlanPage` (toggle between list and graph), with React Flow and the existing `RISK_NODE_STYLE` colors:
  - Phase-level graph first. Clicking a phase drills into its unit-level graph (`&phase_id=`), and a breadcrumb leads back.
  - `needs_facade` edges drawn in the error color and `merged` edges dashed.
  - Node count capped the same way as `LIVE_TREE_NODE_CAP`, with a "showing N of M" note.
  - Done in `main.jsx::PlanGraph`/`buildPlanGraph`, using a List/Graph toggle in the Phases header. The layout is layered: a node's column is one past its deepest dependency, so left to right reads as migration order, with arrows pointing at dependencies. A cycle's back edge arcs over the top instead of cutting through the nodes (caught in the browser pass). Clicking a unit node opens `PlanUnitDetail` (`&unit=`). `&phase_id=` sits alongside `?plan=` like `&unit=`, is cleared by other navigation and by switching to List, and reopens the graph on reload. The cap is `PLAN_GRAPH_NODE_CAP = LIVE_TREE_NODE_CAP` (70), keeping the earliest nodes in migration order. Verified with `vite build` and in a browser against the real router over `tests/fakes.py` seed data: the phase graph, drilling into a phase with a `needs_facade` cycle, a unit click opening the panel, the breadcrumb back, List clearing `phase_id`, an 80-unit plan showing "Showing 70 of 80 units", and a bad `phase_id` showing its 404 message. Dashed `merged` edges weren't visible: at plan-item level a merged unit's own edges fold inside its node (§11), so seed data can't produce one. Readability at the cap is left to the Phase 6 frontend audit.

- [x] `POST /api/agent2/plans/{plan_id}/submit`: `draft → in_review`. `409` from any other state or when stale. Tests.
  - Done in `router.py::submit_plan` (details in `design_agent2.md` §11), with 11 tests in `tests/test_agent2_plans_api.py`.

### Frontend sync — Submit
- [x] "Submit for review" button on `PlanPage`, shown only for `draft`. It re-fetches the plan on success and shows the 409 message otherwise.
  - Done in `main.jsx::SubmitPlanAction`, in the plan header in the same style as `GeneratePlanAction` (`StarfieldButton` `loading`, errors through `formatJobErrorMessage`). On success `PlanPage::refreshPlan` swaps in the re-fetched plan without the loading state, so open phases and the selected unit stay put, and the button disappears with the status change. Verified with `vite build` and in a browser against the real router over `tests/fakes.py` seed data: submitting a draft sent the POST then a GET and showed "in review" with Phase 2 still open; on a second draft submitted behind the page's back, the button showed the backend's 409 message ("only a draft plan can be submitted for review (status=in_review)").

- [x] `POST /api/agent2/plans/{plan_id}/phases/{phase_cluster_id}/approval`:
  - Body `{status: approved|rejected, note}`, with the reviewer id taken server-side from the JWT (never from the body). Only allowed on `in_review`, `409` if stale.
  - Upserts `phase_approvals`, then applies the auto-transitions: any rejection makes the plan `rejected`; all phases approved with `open_facade_flags = 0` makes it `approved` (`approved_by`/`approved_at` set).
  - Returns the plan's resulting `status`. Tests for each transition, the 409s, and a phase not belonging to the plan returning 404.
  - Done in `router.py::approve_phase` (details in `design_agent2.md` §11), with 20 tests in `tests/test_agent2_plans_api.py`. Its frontend pair is the Approve / Reject controls below.

### Frontend sync — Approval
- [x] Per-phase Approve / Reject controls (with a note field) on `PlanPage`, shown only while `in_review`. After each action the plan status re-renders.
  - Done in `main.jsx::PhaseApprovalControls`, rendered in each phase's body in the List view (not the Graph view) while the plan is `in_review`. A repeat decision replaces the earlier one, so the controls stay up after a vote: the saved decision's button shows as pressed and the note field starts from its note. On success `PlanPage::refreshPlan` re-fetches the plan without the loading state; a failure shows the backend's message (`formatJobErrorMessage`). Verified with `vite build` and in a browser against the real router over `tests/fakes.py` seed data: approving Phase 2 with a typed note saved both and left the plan "in review" (one open facade flag); rejecting Phase 1 turned the plan `rejected` and removed the controls.
- [x] While `open_facade_flags > 0`, a persistent notice says approval can't complete until each flagged cycle is acknowledged or overridden, and links to the flagged units.
  - Done in `main.jsx::PlanFacadeNotice`, in the plan header whenever `open_facade_flags > 0`, whatever the status, with no dismiss. It links to every unit flagged `needs_facade`. A link opens that unit's detail panel (`&unit=`) and expands its phase in the List view. Cycles between phases don't flag any unit (`design_agent2.md` §11), so when no unit is flagged the notice points to the Graph view instead. Verified with `vite build` and in a browser against the real router over `tests/fakes.py` seed data: the notice listed Cart and Pricing (Phase 2); clicking Cart opened its panel with Phase 2 expanded and the row selected; after both phases were approved the plan stayed "in review" and the notice stayed. The no-flagged-unit fallback isn't in the seed data, so it wasn't seen in the browser.
- [x] An `approved` plan shows a clear "Approved by … at …" state, and all controls are hidden.
  - `GET /plans/{plan_id}` didn't return who approved the plan, so it now includes `approved_by` / `approved_at` (the plan row's columns, `null` until approved). This is noted in `design_agent2.md` §11, with the shape test updated and a new test in `tests/test_agent2_plans_api.py` for an approved plan. On the frontend the state is in `PlanPage`'s header: "✓ Approved by you at …" when `approved_by` is the signed-in user, otherwise the reviewer's short id (full id on hover). Every write control is already gated on a status (submit on `draft`, approval on `in_review`), and an approved plan has no open facade flags, so nothing else shows; override controls must use the same gate when they're built. Verified with `vite build`, the full test suite, and in a browser against the real router over `tests/fakes.py` seed data (no open facade flags): approving the last phase turned the plan "approved", showed "Approved by you at …", and left no controls. The other-reviewer wording wasn't seen in the browser.

- [x] `POST /api/agent2/plans/{plan_id}/overrides`:
  - Body `{kind: merge|facade_ack|reorder, payload}` (`split` was dropped from the MVP, see below). Only allowed on `draft`/`in_review`; `409` otherwise or when stale; `422` on an invalid payload or a DAG-inconsistent reorder.
  - Records the override against version *n*, then synchronously recomputes §7 steps 1, 2 and 4 and §8 over the stored clusters and edges with the whole override chain applied. No LLM call, no Leiden. Writes version *n+1* (`draft`, `parent_plan_id` = *n*) and returns `201 {plan_id}`.
  - Tests: each kind; version *n* left untouched; a `facade_ack` decrements `open_facade_flags`; a chained override on *n+1* carries *n*'s overrides forward.
  - Built for `merge`, `facade_ack` and `reorder` (`router.py::create_override`, recomputation in `agent2/overrides.py`, details in `design_agent2.md` §11). `split` answers 422. On 2026-09-23 it was dropped from the MVP (specs §9, `design_agent2.md` §9.2), because it needs a plan-item key without a `clusters` row and entity-level edges Agent 2 doesn't store. If it's revisited, read Agent 1's rows for just the unit's members.
    - New migration `20260923030000_agent2_plan_override_result.sql` adds `plan_overrides.result_plan_id` (the version an override produced). It's needed because two overrides on the same version branch, and each version's chain must replay only its own overrides.
    - Read-path follow-ups in the same change: an acknowledged cycle, or one an override merge folds into a single item, no longer flags its units `needs_facade`. An override merge that takes in a `merged_scc` unit shows that unit's originals' reports.
    - Tests: 33 new in `tests/test_agent2_plans_api.py` (each kind and its 422s, version *n* untouched, the `facade_ack` count, a chained override carrying the chain forward, independent branches, 404/409/401, and no orphan version when the override insert fails). There's also one in `tests/test_agent2_pipeline_fixture.py`: over the real worker's output, a rebuild with no overrides reproduces version 1 exactly. Full suite: 559 passing.
    - Its frontend pair is "Frontend sync — Overrides" below.

### Frontend sync — Overrides
- [x] Override controls on `PlanPage`:
  - **Merge:** multi-select units within a phase, then "Merge".
  - **Acknowledge facade:** a button on each `needs_facade` cycle, with a required note.
  - **Reorder:** move up or down within a phase.
  - Done in `main.jsx`: `useCreateOverride`, the edit controls on `PlanUnitRow`, a per-phase merge bar in `PlanPage`, and `PlanFacadeNotice`/`PlanFacadeCycle`. The controls show in the List view only, and only while the plan takes overrides (`draft`/`in_review`, not stale).
    - Each open phase gets a checkbox and ↑ ↓ buttons per unit (beside the row button, so a row click still opens the detail panel), plus a "Merge N selected" button that needs two or more ticked. The facade notice now lists each open cycle from the overview, with its units linked, a required note and "Acknowledge". A cycle without an `scc_id` can't be acknowledged and says so.
    - Every override makes a new version, so on success the page moves to it (`navigateToPlan`). A 422 shows the backend's message inline, under the phase or in the cycle row. The "Version N (from N-1)" note and the link back are still the next task.
    - Backend additions: `GET /plans/{plan_id}` now returns `facade_cycles: [{level, scc_id, cluster_ids}]` (the open cycles the acknowledge button needs), with a new test in `tests/test_agent2_plans_api.py`. An override-merged item's label now joins every merged unit's label ("Cart + Pricing"); before, it showed only the unit whose id it keeps. Both are noted in `design_agent2.md` §11.
    - Verified with `vite build`, the full test suite (560 passing), and in a browser against the real router over `tests/fakes.py` seed data:
      - acknowledging the Cart ↔ Pricing cycle with a typed note opened the new version with 0 flags and no notice;
      - moving Coupons above Cart (which it depends on) showed "“Coupons” depends on “Cart”, so “Cart” must come first in the phase" and made no version;
      - moving Invoices + Orders up made a new version in the new order;
      - ticking Pricing and Cart and merging made a version with "Cart + Pricing" first, flagged merged, on both a draft and an in-review plan (the new version starts as a draft);
      - the checkbox takes keyboard focus.
- [x] Each successful override navigates to the returned new version, with a "Version N (from N-1)" note and a link back to earlier versions. A 422 shows the backend's message inline.
  - Navigating to the new version and the inline 422 came with the override controls above. This task adds the note: `PlanPage`'s header shows "Version N (from M) · Open version M · All versions" on any version an override made. "Open version M" opens the parent, and "All versions" opens the project page, whose Plans list has every version. Version 1 shows no note.
    - `GET /plans/{plan_id}` now returns `parent_plan_id` / `parent_version` (null for version 1), with the shape test updated and a new test in `tests/test_agent2_plans_api.py`. Noted in `design_agent2.md` §11.
    - Verified with `vite build`, the full test suite (561 passing), and in a browser against the real router over `tests/fakes.py` seed data: a reorder opened the new version with "Version 3 (from 1)" (the fake server's 80-unit plan holds version 2 of the same planning job), "Open version 1" went back to version 1 with no note, and "All versions" opened the project page listing versions 3, 2 and 1.
    - Found while testing: the browser Back button changes the URL but not the page anywhere in the app, because `App` has no `popstate` handler. That predates this work and is left for the Phase 6 frontend audit.
- [x] Merged items show their constituent reports rather than a single one (`design_agent2.md` §9.2).
  - Already built in `main.jsx::PlanUnitDetail` with the "Unit detail" task. A unit with no report of its own (`report: null`) lists `constituent_reports`, each with its label, risk, grounding and summary, under "This unit was merged from N units". Since then the backend has changed:
    - an override merge that takes in a `merged_scc` unit now stands on that unit's originals;
    - an override-merged item is labelled with every merged unit.
  - Both changes came with the overrides work, and their tests are in `tests/test_agent2_plans_api.py`. No new code for this task.
  - Verified in a browser against the real router over `tests/fakes.py` seed data: merging Cart with the auto-merged "Invoices + Orders" unit made a version whose "Cart + Invoices + Orders" item opened with three reports, Cart (medium, unverified), Invoices (low, unverified) and Orders (high), rather than one.

## Phase 6 — Hardening

- [ ] Write-path multi-tenancy test for the planner worker (live, same pattern as `tests/test_worker_write_path_multi_tenancy.py`): entity names or `raw_facts` containing another org's id never change the stamped `org_id` on any Agent 2 table, and every update helper called with a wrong `org_id` is a no-op, with a positive control. No endpoint, so no frontend pair.
- [ ] Determinism test: two planning jobs with the same `params` over the same `source_job_id` produce identical clusters (by member sets) and identical plan order. No endpoint, so no frontend pair.
- [ ] Staleness end-to-end: re-ingest the project until a newer job is `done`; the older plan reads back as `stale`, and submit, approval and overrides all return 409. Frontend check: the stale banner shows on `PlanPage` and the stale badge in the Plans list, verified in a browser.
- [ ] Load test at 5,000–10,000+ entities, reusing `tests/test_load_large_repo.py`'s synthetic WordPress generator. Record stage timings and peak memory, confirm report fan-out never exceeds the cap, and note the results in `design_agent2.md`. No endpoint, so no frontend pair.
- [ ] Frontend audit across `PlanningJobStatusPage`, `PlanPage` (list, graph, unit detail) and the `ProjectDetailPage` additions: loading, empty and error states; keyboard reachability of the approve/override controls; and graph readability at the node cap.

## Phase 7 — Plan document data (from `design_agent2.md` §16)

- [ ] **Agent 1, `tasks.md`:** persist declared packages as `dependency` entities from composer.json / package.json / requirements.txt (`name`, `raw_facts: {ecosystem, version_spec, manifest_path, dev}`), reusing `worker/discovery.py`'s manifest parsing. Frontend pair: none until Agent 2 reads them.
- [ ] `plan_dependency_targets` table and migration (RLS, `org_id`, service-role writes only) and a planner stage that proposes a target per dependency, grounded against the manifest line, `unverified` on failure. Add `GET /plans/{plan_id}/dependencies` (facts plus optional target). Frontend pair: `tasks_frontend.md` A2.3 dependency inventory (target column only when present).
- [ ] Confirm no backend work is needed for the other sections: plan overview, graph and unit detail cover the summary, architecture, risk and migration plan; overview and stats come from Agent 1 endpoints. Free-form open decisions and schema "becomes" mapping are deferred (§16).
- [ ] Expose provenance to the plan document: `source_commit_sha` on `GET /api/agent1/jobs/{id}` (Agent 1, `design.md` §4, `tasks.md`; the column exists, `_JOB_STATUS_COLUMNS` omits it) and a generation time on `GET /plans/{plan_id}` (`created_at`, `design_agent2.md` §11). Frontend pair: `tasks_frontend.md` A2.2 provenance line (`Provenance.jsx`), pass both when available.

---

## Cross-cutting: keeping backend and frontend in sync

Whenever you add or change an `/api/agent2/...` endpoint:
1. Update `design_agent2.md` §11 first, so the contract change is visible before code.
2. Add the backend checkbox here.
3. Add the paired frontend checkbox directly under it, even if it's just "wire up the new field". Don't let it become a follow-up you forget.
4. Don't check off the backend box until the frontend can actually exercise it.
5. If a worker change surfaces a new field a view should show (e.g. a new `graph_stats` value), that's an endpoint change too, so rules 1–4 apply.
