# tasks_frontend.md — Frontend redesign (Agent 1 run view, Agent 2 plan document)

Implements the designs prototyped in `playground/` (standalone Vite app, mock data, no imports from `frontend/`) inside the real app in `frontend/`. The playground is the visual and behavioural reference; this file is the migration checklist. Keep the checkboxes current (CLAUDE.md).

## Rules for this file

- **Frontend only.** A task that needs a new endpoint, column or event field is marked **Needs design** and points at the design doc that must change first (`design.md` for Agent 1, `design_agent2.md` for Agent 2). Update the design doc, then `tasks.md` / `tasks_agent2.md` for the backend half, then build. Don't invent endpoints from the frontend (CLAUDE.md).
- **Don't ship controls that can't work.** If the backend can't pause or cancel a job, the button is absent, not disabled.
- **Reuse before porting.** Existing pieces stay: `StageList`/`ProgressStages`, `WorkflowRun` feed, `UnverifiedBadge`, `risk-level-*` pills, `PlanGraph` (React Flow), `PhaseApprovalControls`, `useApiFetch()`, `formatJobErrorMessage`. Port playground components only where they replace something or fill a gap.
- **Tokens only.** No hardcoded colours in components. Everything resolves through `theme/themes.js` variables, in both light and dark.
- **Every view ships with loading, empty and error states** (`tasks_agent2.md` convention).
- **Accessibility bar for every task:** WCAG AA contrast in both themes, visible focus, keyboard operable, status = icon + text + colour, `prefers-reduced-motion` respected, tabular numbers for timers/counts/percentages.
- **Testing:** pure logic (view-model builders, Markdown export) gets `node --test` unit tests next to the code, like `theme/themeStore.test.js`. Visual work is checked with `npm run build --prefix frontend` plus a real browser pass in light and dark at 1440, 1024 and 390px.

## Reference map

| Playground file | Becomes (in `frontend/src/`) |
|---|---|
| `settings.js` (`cssVars`, options) | Locked values in `theme/themes.js` and `styles.css`; no runtime tweaks in production |
| `styles/base.css` | Tokens and primitives in `styles.css` / `ui/ui.css` |
| `components/shared.jsx` | `ui/` (StatusPill, StatePanel, Segmented, Toast, `useCopy`, format helpers) |
| `agent1/*` | `agent1/` (RunView, ProgressViz, PhaseTimeline, StatChips, LiveLog, `buildRunView.js`) |
| `agent2/*` | `agent2/` (PlanDocument, Toc, section components, `toMarkdown.js`) |
| `agent1/runModel.js`, `agent2/planData.js` | Replaced by real API data; kept as test fixtures only |

---

## Phase 0 — Verify and lock the design

- [x] **Blocking:** run `cd playground && npm install && npm run dev` and walk both screens in light and dark. The playground was written but its first full compile and browser pass were not completed in the authoring session, so fix any build or runtime errors first.
- [x] Review every state in the tweak bar: Agent 1 Live/Queued/Empty/Error, Agent 2 Ready/Loading/Empty/Error. Fix anything that breaks the accessibility bar above (contrast in both themes for accent text, warning and success; focus order; reduced-motion).
- [x] Choose and record the locked defaults with **Copy JSON** / **Copy CSS**, and paste them under a "Locked config" heading at the bottom of this file. Recommended starting point: Geist + Geist Mono, 14px / 1.5, comfortable density, lavender accent, 10px radius, dark default, segmented progress, progress sidebar.
- [x] Decide which variants ship. Anything not chosen is deleted from the port (no dead variant code in the app). Default assumption: one progress style and one sidebar style ship.

## Phase 1 — Shared foundations

- [x] **Tokens.** Add the new semantic tokens to both `light` and `dark` in `frontend/src/theme/themes.js`, renamed from `--pg-*` to product names (`accent-ui`, `accent-ui-foreground`, `success`, `surface-sunken`, radius and spacing scale). Reuse existing tokens wherever one already exists (`danger`, `warning`, `border`, `muted-foreground`, `workflow-accent`). Update `theme/README.md` and extend `themeStore.test.js` if token keys are asserted. Verify with `node --test frontend/src/theme/themeStore.test.js`.
- [x] **Scales in `styles.css`.** Add the 4/8 spacing scale, the type scale (12px floor), motion tokens (150ms / 220ms, no bounce) and one global `prefers-reduced-motion` rule.
- [x] **Fonts.** Keep the local Geist and Geist Mono files. Only add `@fontsource` packages if a different pairing was locked in Phase 0.
- [x] **`frontend/src/ui/` primitives:** `StatusPill` (icon + text + tone), `StatePanel` (loading/empty/error), `Segmented`, `Toast` with a polite live region, `useCopy` (clipboard with a fallback), `formatClock` / `formatDuration` / `formatInt`. Adopt existing classes instead of duplicating (`risk-level-*`, `summary-loading`, `error`).
- [x] **Icons:** `lucide-react` only (already a dependency). No emoji, no second icon set. Checked: no other icon package is installed, and `ui/` and `ThemeProvider` use lucide. Seven text glyphs remain in `main.jsx` (`⚠`, `✓`, `✕`, `☰`); replace each with a lucide icon when its screen is moved out of `main.jsx`.
- [x] **Extract from `main.jsx`.** Create `frontend/src/agent1/` and `frontend/src/agent2/` folders and move code in as each phase below lands. Don't do a big-bang split. `main.jsx` keeps routing (`PAGE_PARAMS`, `App`) only. Folders created with a README each; nothing moved yet.
- [x] Generalize the new progress components over a `stages` prop, like `ProgressStages` already is, so `PlanningJobStatusPage` (`PLANNING_STAGES`) and `ExecutionStatusPage` (`EXECUTION_STAGES`) can reuse them later without a fork. These components don't exist in `frontend/` yet, so this is recorded as a contract on A1.2: `ProgressViz` and `PhaseTimeline` take `phases` (each with `id`, `label`, `status`, `progress`, `weight`) and an `aria-label` as props, and never import the Agent 1 phase list. The playground versions hardcode `PHASES`, `TOTAL_SECONDS` and the label "Overall analysis progress"; don't copy that.

---

## Agent 1

Real target: `JobStatusPage` (`main.jsx`, polling `GET /api/agent1/jobs/{job_id}` every `JOB_POLL_INTERVAL_MS`) plus the `WorkflowRun` live feed (`workflow_events`, `shared/workflow_events.py`).

### A1.1 View model

- [x] `agent1/buildRunView.js`: a pure function `buildRunView({ job, events, now })` returning `{ status, phases[], pct, elapsed, remaining, current, stats, log, warnings, error }`. Replaces the playground's deterministic `runModel.js`. Phase list comes from the existing `JOB_STAGES` (one source of truth); each phase is `queued | in_progress | completed | warning | failed`. Built: `events` is the folded `workflowReducer` state; `pct` is equal-weight and `remaining` is `null` until the next task; `JOB_STAGES` moved to `agent1/stages.js`, used as the fallback phase list before the first event arrives (once events arrive the workflow's own declared stages are used).
- [x] Percentage and ETA from per-stage weights in a single constant, until the backend reports real progress. Show "Estimating…" for the ETA until at least one stage has completed. Never show a fake precise ETA. Built: weights are `STAGE_WEIGHTS` in `agent1/stages.js`; `remaining` is `null` (show "Estimating…") until a stage completes, then rounded to 10 s (under 10 min) or a minute. The active stage counts as 0 until it completes, so `pct` steps rather than creeps.
- [x] Unit tests: each status maps correctly, a failed job freezes elapsed at its failure time, warnings only come from `warn` events, and the ETA is absent before the first completed stage. Written alongside the two tasks above in `agent1/buildRunView.test.js` (11 tests).
- [x] `useJobRun(jobId)` hook wrapping polling and event subscription, cleaned up on unmount (same rule as `PlanningJobStatusPage`). Built in `agent1/useJobRun.js`: returns `{ job, view, error }`, ticks once a second while the run is live, and corrects for clock skew. Not wired into `JobStatusPage` yet; that happens with the A1.2/A1.3 components.

### A1.2 Components (port from `playground/src/agent1/`)

- [x] `ProgressViz` (props: `phases` with `weight`, `pct`, `label`; no hardcoded phase list) with the locked variant: overall percentage, `role="progressbar"` with `aria-valuenow`/`aria-valuetext`, segments proportional to stage weight, hatch for warning and outline for failed so state never relies on colour. Built in `agent1/ProgressViz.jsx` + `agent1/agent1.css` (segmented only; each segment also has screen-reader text). Not yet seen in a browser: it renders once the overall card and `RunView` wire it up.
- [x] Overall card: percentage, current phase and "Step n of N", elapsed, estimated remaining, start time. Tabular numbers. Built in `agent1/OverallCard.jsx` (wraps `ProgressViz`); `buildRunView` now also returns `startedAt`. Shows "Estimating…" until the first stage completes. Not yet seen in a browser.
- [x] `PhaseTimeline`: icon + label per state, connector line, current phase expanded, inline warning and failure notes, `aria-current="step"`. Built in `agent1/PhaseTimeline.jsx`. The running phase shows its latest activity message (the view has no per-phase percentage or path). No per-phase duration column, since the view carries none. The restart button appears only when `onRetry` is passed. Not yet seen in a browser.
- [x] `StatChips`: files, lines of code, languages, frameworks, dependencies, each showing "Pending" (dashed, with text) until known. Source the values from the partial stats the job already exposes (see `ExtractionStats` / `DiffSummary`); list any chip with no data source in A1.6 instead of faking it. Built in `agent1/StatChips.jsx` + `agent1/statChips.js` (with tests). Only **Files** has a source today (changed + unchanged counts, from the `stats.updated` event or the job row); the other four chips render "Pending" and are listed in A1.6. Not yet seen in a browser.
- [x] `LiveLog`: collapsible, `role="log"`, monospace, auto-follow that pauses when the user scrolls up with a "Resume auto-scroll (n new)" button, copy button, capped at a fixed line count. Feed it from the existing `workflow_events` stream rather than a second source; retire `WorkflowFeed` once it has parity (timestamps, refs, warn/error styling). Built in `agent1/LiveLog.jsx` (fed by `buildRunView().log`; cap is the reducer's `MAX_ACTIVITY_ENTRIES`, with "n earlier events hidden"; `aria-live="off"` so the log isn't read out line by line). It has parity with `WorkflowFeed`, but `WorkflowFeed` stays in place until `WorkflowRun` is replaced by the new run view. Not yet seen in a browser.
- [x] Screen-reader announcements: a single polite live region that updates only on phase change (not on every tick). Built as `agent1/PhaseAnnouncer.jsx` (one `role="status"` region) over `agent1/announcement.js`, whose text depends only on run status and current phase. `LiveLog` and the timeline stay silent. Render it once in the run view.

### A1.3 States

- [x] **Loading / queued:** skeleton plus "waiting for a worker" (queue position only if the API provides it). Built as `agent1/QueuedState.jsx` (`loading` prop for the first poll). The API gives no queue position, so none is shown, and there is no cancel button. Not yet seen in a browser.
- [x] **Empty:** project has no run; explain what analysis does and link to the project page's ingestion form. Built as `agent1/EmptyState.jsx`; `onStart` should be `backToForm` (the ingestion form is the app's start page, there is no separate project-page form). Not yet seen in a browser.
- [x] **Failed:** the failed phase, `job.error` verbatim through `formatJobErrorMessage`, and a "Restart analysis" action wired to the existing re-ingest path (`ReingestAction`). Built: `agent1/FailedState.jsx` shows the failed phase and the error text as-is. `GET /api/agent1/jobs/{id}` now returns `project_id` (`design.md` §4, `tasks.md`), and `JobStatusPage` renders `FailedState` with `JobRestart`, which loads the project and offers `ReingestAction`. Not yet seen in a browser (needs a live backend and a failed job).
- [x] **Completed:** summary state with "View migration plan" (goes to `GeneratePlanAction`'s flow; Agent 1 never enqueues Agent 2, per CLAUDE.md, so this is a navigation, not a trigger) and the existing `JobSummary`, `ExtractionCoverage` and `AskCodebase` below it. Built: `agent1/CompletedState.jsx` (time taken, warning count) wraps `JobSummary` and `ExtractionCoverage` in `JobStatusPage`, with `EntityBrowser` and `AskCodebase` still below the hero as before. "View migration plan" navigates to the project page (`navigateToProject`) using `job.project_id`. Not yet seen in a browser (needs a live backend and a done job).
- [x] **Stale connection:** if polling fails repeatedly, show "Connection lost, retrying" with a manual retry, and keep the last known state visible. Built: `useJobRun` and `useWorkflowEvents` now retry a failed poll with backoff instead of stopping (this also changes `JobStatusPage`'s events feed, which used to stop on the first error). `useJobRun().connection` is `{ lost, retry }`; `lost` is true after 2 failed polls in a row. `agent1/ConnectionBanner.jsx` shows "Connection lost, retrying" with a "Retry now" button, and the last known job and events stay in `view`. Not yet seen in a browser.

### A1.4 Controls

- [x] **View logs** (scroll to and focus the log panel) and **Copy log**: frontend only, ship now. Built: **Copy log** is in `LiveLog` (uses `useCopy`'s copy). **View logs** is `agent1/ViewLogsButton.jsx`, which calls `LiveLog`'s new ref method `reveal()` (expands the log, scrolls it into view, focuses it). Not yet wired into a page or seen in a browser.
- [x] **Cancel (Pause and resume dropped) — designed and built 2026-09-29.** No endpoints exist. Before any UI: add `POST /api/agent1/jobs/{job_id}/cancel` (and pause/resume if wanted) to `design.md` §API, define worker semantics (finish the current batch, delete the queue message, mark `cancelled`, org-scoped), then add the backend tasks to `tasks.md`. Frontend follows with the inline two-step confirm ("Cancel analysis" / "Keep running", no browser `confirm()`), a `cancelled` state and its banner. Until then these buttons are not rendered. **Done for Cancel only**, by decision: `design.md` §4, `tasks.md` "Cancel a run", `POST /api/agent1/jobs/{job_id}/cancel`, a `cancelled` status and worker checks, then `agent1/CancelControl.jsx` (two-step confirm, shown while the job is queued or running) and `agent1/CancelledState.jsx`, both wired into `JobStatusPage`. Pause/resume is not built and there is no Pause button. Not yet seen in a browser; the migration must be applied to the database first.

### A1.5 Wire-up and cleanup

- [x] Replace the body of `JobStatusPage` with `RunView`, keeping its props and the navigation helpers unchanged. Done: `agent1/RunView.jsx` composes `useJobRun` with the overall card, timeline, stat chips, log, banners and states. `JobStatusPage` in `main.jsx` is now a thin wrapper with the same props; the pieces still in `main.jsx` (job summary, coverage, entity browser, ask, live entity tree, diff summary, re-ingest) are passed in as `slots`. Server-rendered check of every component in running, failed, cancelled and done states was clean; not yet seen in a real browser (needs sign-in and a live backend). `useWorkflowEvents`, `WorkflowRun` and `ExtractionStats` are now unused in `main.jsx`; removing them belongs to the next item.
- [x] Remove `ProgressStages` usage from Agent 1 only after the new view has parity; leave it for any page still using it. Done: Agent 1 no longer uses `ProgressStages` (only `PlanningJobStatusPage` and `ExecutionStatusPage` do). Removed the now-unused `JOB_STAGES`, `useWorkflowEvents` and `WorkflowRun` imports from `main.jsx`. Left in place: `workflow/WorkflowRun.jsx` (with its `WorkflowFeed`) is now unreferenced but is meant for Agent 2 and Agent 3 status pages to reuse, and the dead `ExtractionStats` component in `main.jsx` (it reads `job.extraction_stats`, which the API does not return); delete either when you want.
- [x] Delete replaced CSS from `workflow.css` / `styles.css` (`.job-stage*`, `workflow-feed*`) once unused. Done: deleted `workflow/WorkflowRun.jsx` and `workflow/workflow.css` (unreferenced once `RunView` replaced them; the reducer, hook and tests stay). `.job-stage*` in `styles.css` is kept: `ProgressStages` still uses it on the Planning and Execution status pages. The `--workflow-accent` theme token is now unused and was left alone.

### A1.6 Backend gaps found by the prototype (all **Needs design**, `design.md`)

- [x] **Per-step progress detail.** The playground shows "Indexing 1,284 / 3,902 files" and the current file path. That needs `progress_detail { done, total, unit, current_path }` on the job or on `workflow_events`, throttled. Until it exists the live detail renders an indeterminate bar with the stage name only. **Done for `summarize` and `fold`** (2026-09-29): a `progress.updated` workflow event (`design.md` §2, `tasks.md` "Per-step progress"), not a field on the job. The five earlier stages (fetch to evidence) have no counts and keep the indeterminate bar. Not yet seen in a browser against a live run.
- [x] **Warnings as first-class events.** A `warn` level on `workflow_events` with `phase`, `message` and `files[]`, so the timeline can show "Completed with warning". Until it exists, no warning state is shown. **Done 2026-09-29** without a new event type: a warning is an `activity` event with `data.level = warn`, its phase is the event's `stage` and its files are the `file` refs (`design.md` §2, `tasks.md` "Warnings"). The worker now warns when Graphify is unavailable, when it only partly extracted files, and when files were skipped building evidence, on top of the existing `unverified` summary warning. Not yet seen in a browser against a live run.
- [x] **Discovered stats while running** (languages, frameworks, dependency count as they are found). Confirm what `jobs` exposes during a run; add fields only if missing. Confirmed for the StatChips task: during a run only file counts (changed / unchanged / removed) and the summary/evidence counters are exposed. **Lines of code, frameworks and dependencies have no source.** Languages exist only in the `/coverage` report after extraction, not in the live stats. (`ExtractionStats` in `main.jsx` reads `job.extraction_stats`, which `GET /api/agent1/jobs/{id}` does not return.) **Done 2026-09-29:** the worker now computes and emits them once, after the `filter` stage (`worker/discovery.py`; `design.md` §2, `tasks.md` "Discovered stats"), so all five `StatChips` have a source. Languages here are source files by extension; frameworks are the ones the extractors recognise (WordPress, Laravel). Not yet seen in a browser against a live run.
- [x] **Job to project link** (`design.md` §4, done 2026-09-29). Add `project_id` to `GET /api/agent1/jobs/{id}` (the column is already on `ingestion_jobs`). Unblocked the Failed state's "Restart analysis" (`ReingestAction`) and lets the sidebar highlight the right repo on a bare job page.
- [x] **Queue position** for the queued state (optional). **Done 2026-09-29:** `queue_position` on `GET /api/agent1/jobs/{id}` (`design.md` §4, `tasks.md` "Queue position"). It is a global count across all orgs (the worker queue is shared), returned as a bare number. Not yet seen in a browser against a live queue.

---

## Agent 2

Real target: `PlanPage` (`main.jsx`, ~300 lines) with `PlanGraph`, `PlanUnitRow`, `PlanUnitDetail`, `PhaseApprovalControls`, `PlanFacadeNotice`, `SubmitPlanAction`, `GenerateCodeAction`. Plans are immutable, pinned to one Agent 1 job, and editable only via a new plan version.

### A2.1 Section-to-data mapping (do this first)

The playground shows eight sections. Only some have real data behind them today. Confirm each row against `design_agent2.md` §2/§11 and Agent 1's tables before building, and record the decision here.

| Section | Data source | Status |
|---|---|---|
| Executive summary | Phase and unit cluster reports (`cluster_reports`) plus Agent 1 `JobSummary` | Available; compose, mark `unverified` reports with `UnverifiedBadge` (confirmed 2026-09-29) |
| Repository overview | Agent 1 job stats and extraction stats | Available; read `GET /api/agent1/jobs/{source_job_id}` and its `stats.updated` events directly |
| Architecture and module map | Clusters (units), phases, `cluster_edges`, existing `PlanGraph` | Available |
| Dependency inventory | Agent 1 `dependency` entities | **Not available today.** No extractor emits `dependency` entities; only a count exists. Needs an Agent 1 persist task, then a target-mapping design (decided in `design_agent2.md` §16, tasks in `tasks_agent2.md` Phase 7). Show the count only until then |
| Data layer and schema | Agent 1 `schema` entities | Available only as WordPress post types and meta keys (not tables, no row counts). No "becomes" mapping (`design_agent2.md` §16) |
| Risk assessment | Cluster `risk_level` / `risk_factors` (rolled up by `rollup_risk`, LLM never sets risk) | Available. **Decided:** drop "critical"; scale is `low/medium/high` |
| Migration plan | Phases, units, `phase_approvals`, `plan_overrides`, test contracts | Available |
| Open questions / decisions | Facade cycles (`needs_facade`) and acknowledgements | Facade cycles only. **Decided:** free-form decisions are deferred, not built |

- [x] (done 2026-09-29, `design_agent2.md` §16, `tasks_agent2.md` Phase 7) Write the decisions for the three gaps above (dependency target mapping, schema mapping, open decisions) into `design_agent2.md`, then add backend tasks to `tasks_agent2.md`. Until then the frontend renders those sections from available facts only, or omits the missing columns.
- [x] (decided 2026-09-29: Agent 1 endpoints directly, `design_agent2.md` §16) Decide whether the "Repository overview" and "Dependency inventory" sections read Agent 1 endpoints directly (allowed: read-only) or through a plan-scoped read model. Prefer existing Agent 1 read endpoints.

### A2.2 Document shell (port from `playground/src/agent2/OutputView.jsx`, `Toc.jsx`)

- [x] (2026-09-29: layout shell, now mounted and seen in a browser. **Breakpoints are container queries, not the viewport widths above:** the app's own 260px sidebar and page padding take ~370px, so a 1440px viewport left the document column ~400px. Now three columns need ~1180px of content width, two need ~820px, otherwise one; that is ~1910px, ~1440px and below, in viewport terms. Also `.page-shell:has(.a2-shell)` is `overflow: clip` because `overflow: hidden` stopped `position: sticky`, and the outline and rail stick 105px down, below the 89px site header) `agent2/PlanDocument.jsx`: three-column layout (outline, document, rail), max-width from the locked config, prose measure ≤ 72ch, inner scroll for wide tables.
- [x] (2026-09-29: `Toc.jsx` + `useOutline.js`, progress variant only; not mounted, no browser pass yet, so scrollspy and focus are unverified) `Toc.jsx`: sticky outline, collapsible subsections, scrollspy that highlights the active section and subsection, `aria-current="location"`, the locked variant (recommended: progress indicators, with read state and counters). Selecting an item expands a collapsed section, scrolls, and moves focus to the heading.
- [x] (2026-09-29: `Section.jsx` + `collapse.js`; the Collapse all / Expand all button is `useCollapsed().toggleAll`, wired to a button when the header is built; unmounted, no browser pass yet) Collapsible sections with `aria-expanded` / `aria-controls`; "Collapse all / Expand all".
- [x] (2026-09-29: `Rail.jsx` + `railModel.js`; effort card dropped, the plan has no effort data; review progress counts phase approvals; unmounted, no browser pass yet) Right rail on ≥1280px: overall risk (from the roll-up), estimated effort, review progress, key metrics. Below 1280px the same content renders inline at the top. Effort figures must come from plan data; if the backend has no effort estimate, drop that card rather than invent one.
- [x] (2026-09-29: built with `Toc.jsx`: `.a2-toc-toggle` with `aria-expanded` / `aria-controls`, the nav hidden until opened, closes on navigate, breakpoint matches the shell's single-column one; not seen in a browser yet) Below 960px the outline becomes an "On this page" disclosure.
- [x] (2026-09-29: `Provenance.jsx` + `provenance.js`, pass as `Toc`'s `footer`; it renders only the pieces it has. **Data not wired:** the API exposes neither the commit nor a time yet, see `tasks_agent2.md` Phase 7; unmounted, no browser pass) Provenance line in the outline footer: "Generated from `<commit>` `<time ago>`" from the plan's pinned job.
- [x] (2026-09-29: by construction plus a guard test, `agent2/deepLinks.test.js`: the outline, sections and shell never read or write the URL or history, so `navigateToPage` / `setPlanParam` stay the only writers of `PLAN_SUB_PARAMS`. Still to confirm in a browser once `PlanPage` mounts the shell: opening `?plan=<id>&unit=<id>` and scrolling leaves the URL unchanged) Keep `?plan=<id>&unit=<cluster_id>` deep links working; scrollspy must not fight `PLAN_SUB_PARAMS`.

### A2.3 Sections

- [x] (2026-09-29: `agent2/overviewModel.js`, `useRepoOverview.js`, `ExecutiveSummary.jsx`, `RepositoryOverview.jsx`; real data only: Agent 1 `/summary`, `stats.updated`, route entities and the plan's phase reports. **No Structure table and no Target column**: the data has neither, see `design_agent2.md` §16. Unmounted, no browser pass yet) Executive summary and Repository overview (stack, structure, size, entry points) from real data, with tabular numbers and per-language bars (direct labels, not colour-only legends).
- [x] (2026-09-29: `agent2/Architecture.jsx`, `architectureModel.js`, `useUnitGraph.js`. `PlanGraph` is passed in as the `graph` slot because it still lives in `main.jsx` until A2.5 splits it out. **Columns are entities, risk, depended-on-by (fan-in) and depends-on (fan-out)**: the plan has no files, LOC, coupling label or migration target per unit, so those are dropped (`design_agent2.md` §16). Rows and graph nodes open `PlanUnitDetail` via `onSelectUnit`. Unmounted, no browser pass yet) Architecture: embed the existing `PlanGraph` (React Flow) and add the module table (units, files, LOC, coupling, fan-in, target). Keep `PlanUnitDetail` reachable from a table row and from the graph.
- [x] (2026-09-29: `agent2/DependencyInventory.jsx` + `dependencyModel.js`. **No real rows exist yet**: the planned `GET /plans/{id}/dependencies` is an open task in `tasks_agent2.md` Phase 7, so with `rows={null}` it shows only the declared-package count from job stats. Once rows arrive: sortable with `aria-sort`, filterable by ecosystem and status, status = icon + text, and only columns that have data (target and status appear only when some row has a target). Unmounted, no browser pass; the table path is unit-tested for logic only, never rendered) Dependency inventory: sortable (`aria-sort`) and filterable table with status = icon + text. Show only columns that have data (see A2.1).
- [x] (2026-09-29: `agent2/DataLayer.jsx`, `dataLayerModel.js`, `useSchemaEntities.js`. **No row counts and no "becomes" column**: the `schema` entities are WordPress post types and meta keys, not tables, and Agent 1 reads no database (`design_agent2.md` §16). Shows name, file, risk and the entity summary as notes, unverified marked, run-time-named entries counted, list cut at 200 with a note. Post types and meta keys are not told apart: the list endpoint omits `raw_facts.kind`. Unmounted, no browser pass yet) Data layer and schema: table list with row counts and notes from Agent 1 schema entities.
- [x] (2026-09-29: `agent2/RiskAssessment.jsx`, `riskModel.js`, `useRiskDetails.js`. One item per medium/high unit, icon + label, `low/medium/high` only (no "critical"), the phase that migrates it, factor names from `risk_factors`, files as chips from the unit's members. Factors and files need one unit-detail request per unit, so they load for the first 30 risky units (4 at a time) and the rest say to open the unit. No "mitigation" text: the data has none. Unmounted, no browser pass yet) Risk assessment: severity-tagged items (icon + label), affected files as chips, the phase that addresses each, using the existing risk values.
- [x] (2026-09-29: `agent2/MigrationPlan.jsx`, `migrationModel.js`. Cards show risk, approval state (icon + text) and note, the phase report as the goal, depends-on / depended-on-by from the phase graph, affected files loaded on request (one unit-detail request per unit), and units as rows. **Slots, not moved code:** `renderControls` takes the existing `PhaseApprovalControls` (only while `in_review`) and `renderUnitExtra` takes the existing merge/move override controls, since both still live in `main.jsx` until A2.5; the section itself writes nothing. **No effort and no goals list**: the data has neither. A per-unit "Needs changes" is not added, the existing override flow is the way to request one. `useUnitGraph` now takes a `level`. Unmounted, no browser pass yet) Migration plan: one card per phase with goals, units as the reviewable items, affected files, effort (if available), dependencies, and `PhaseApprovalControls` for the real phase approval. The playground's per-task approve / needs-changes maps as follows: **phase approval stays the authoritative action** (existing endpoints); a per-unit "Needs changes" opens the existing override flow (`plan_overrides`, new plan version), it does not mutate the plan in place.
- [x] (2026-09-29: `agent2/OpenQuestions.jsx`, `openQuestionsModel.js`. One card per open facade cycle from `plan.facade_cycles`, members resolved to unit links and phase names, the approval-blocking rule stated, a cycle with no id says to merge instead. Acknowledging is the existing `facade_ack` override, passed in as `renderAcknowledge(card)` because its form still lives inside `PlanFacadeCycle` in `main.jsx` until A2.5; this component writes nothing. Acknowledged cycles are not in the API response, so only open ones appear, and with none it says so. Free-form decisions are deferred (`design_agent2.md` §16). Unmounted, no browser pass yet) Open questions: render facade cycles and their acknowledgement (`PlanFacadeNotice` / `PlanFacadeCycle`) as decision cards; free-form decisions wait on A2.1.

### A2.4 Reviewer controls

- [x] (2026-09-29: decided, `design_agent2.md` §16 point 6. `phase_approvals.note` already exists and is saved by the approval endpoint, so no migration. **A note is saved only together with an approve or reject**, and deciding again overwrites it; no standalone notes. The existing textarea in `PhaseApprovalControls` is that control, and its label now says "saved with your decision". No "Local draft" label is needed because nothing is UI-only.) **Notes on a phase — Needs design.** Check whether `phase_approvals` (or another table) has a comment field. If not, add it to `design_agent2.md` §2/§11 and the migration to `tasks_agent2.md` (RLS, `org_id`, insert policy) before building the textarea. If notes are UI-only, they must be labelled "Local draft, not saved".
- [x] (2026-09-29: `agent2/toMarkdown.js`, 9 tests. It reuses the section models, so the export and the screen cannot disagree; eight numbered sections in outline order, an empty section keeps its heading and says why, approval state and saved phase notes as stored (note as a blockquote). Everything from the repo or an LLM is escaped: pipes, newlines, markup and block markers, HTML, and code fences that survive backticks. Input is the bundle the sections already loaded (`plan`, `repoSummary`, `stats`, `routes`, `unitGraph`, `phaseGraph`, `dependencies`, `schema`, `riskDetails`, `commitSha`, `generatedAt`, `repoName`); the Copy button is not wired, that needs the mounted page (`useCopy` exists in `ui/`). Risk files and factors appear only for units whose details had loaded) **Copy as Markdown:** port `toMarkdown` to real plan data (`agent2/toMarkdown.js`), including approval state and any saved notes; unit-tested for section order, empty sections and escaping of table cells and paths.
- [x] (2026-09-29: `agent2/reviewAccess.js` (6 tests) + `PlanStatusBanner.jsx`. One pure function holds the rules, mirroring the backend which stays the authority: submit only on `draft`, approve/reject only on `in_review`, overrides (each makes a new version) on `draft` or `in_review`, and nothing when stale, by flag or by status. It returns `canSubmit` / `canDecide` / `canOverride` plus a reason string per control for a disabled control to show, and a banner (stale, approved, rejected, draft; none while in review). **Not wired:** the page still hides `PhaseApprovalControls` outside `in_review` instead of disabling it, and shows its own stale banner in `main.jsx`; switching those to `reviewAccess` needs the mounted page. Approved plans stay approved: the banner says to generate a new plan) **Stale and read-only plans:** if the plan is `stale` or not editable, show a banner explaining why and disable review controls (never hide the plan). Approved plans stay approved; edits create a new version.
- [x] (2026-09-29: decision, guarded by `agent2/noReset.test.js`, which fails if any `agent2/` source offers a review reset; a phase decision is changed by deciding again, never cleared) Reset review is **not** ported: server-side approvals are not a local toggle.

### A2.5 States and cleanup

- [x] (2026-09-29: `agent2/PlanStates.jsx`: `PlanSkeleton` (built on `PlanDocument`, so the same three columns, `aria-busy`, shimmer off under reduced motion), `PlanEmpty` and `PlanFailed` (`StatePanel`, the failure announced as an alert, cause shown as given, a fallback when there is none). The Generate plan / retry control is a slot: it is the existing `GeneratePlanAction`, still in `main.jsx`, so these make no requests. Checked by server-rendering all four states, not in a browser; not yet mounted, and the skeleton has not been compared against the real document for jump) Loading skeleton matching the three-column layout; empty ("no plan yet", with the existing Generate plan action); error (planning failed, cause, retry through `GeneratePlanAction`).
- [x] (2026-09-29: **a pure move, no behaviour change.** `agent2/PlanPage.jsx` holds `PlanPage` and everything only it used (`PlanUnitRow`, `PlanUnitDetail`, `PlanGraph`, `SubmitPlanAction`, `GenerateCodeAction`, `ExecutionRunsList`, `PhaseApprovalControls`, `PlanFacadeNotice` / `PlanFacadeCycle`, `useCreateOverride`), checked to be byte-identical to the original span. Seven small things both sides need (`formatJobErrorMessage`, `UnverifiedBadge`, `ConfidenceBadge`, `GroundingIssuesList`, `RISK_NODE_STYLE`, `DEFAULT_RISK_NODE_STYLE`, `LIVE_TREE_NODE_CAP`) moved verbatim into `frontend/src/shared.jsx`, so neither file imports the other. `App` in `main.jsx` still owns the URL and passes the same props. `main.jsx` went from 2780 to 1552 lines. **Mounted afterwards, same day:** `PlanPage` now renders the new document (`PlanDocumentView`: outline, eight sections, rail) in place of the old phase list, with `PlanGraph`, `PhaseApprovalControls`, the merge/move controls, the facade acknowledge form and `PlanUnitDetail` passed in as slots. `SubmitPlanAction` and `GenerateCodeAction` are unchanged, and the old `PlanUnitRow`, `PlanApprovalState`, `PlanFacadeNotice` and `PlanFacadeCycle` were removed as dead code (the acknowledge form lives on as `FacadeAcknowledgeForm`). Build passes and 121 unit tests pass; the browser pass is under Definition of done) Split `PlanPage` out of `main.jsx` into `frontend/src/agent2/`; keep behaviour identical for Submit for approval and Generate code (`SubmitPlanAction`, `GenerateCodeAction`).
- [x] (2026-09-29: `PlanningJobStatusPage` now renders `agent2/PlanningProgress.jsx`, which uses the shared `ProgressViz` and `PhaseTimeline` (generalised over `phases`) in place of the dot stepper; the `PLANNING_STAGES` list moved to `agent2/planningPhases.js` as four stages (graph, cluster, report, order), dropping the synthetic `done` step. **Job-row driven only:** the planner emits no workflow events and has no events endpoint, so progress is whole stages (a running stage gets no made-up fraction, `pct` is completed over total) and there is no live log, stat chips or ETA. Showing those needs the planner to emit workflow events (`agent2_planning` is already a valid workflow kind), a backend task not in `tasks_agent2.md` yet. `jobStageState` moved to a plain `jobStageState.js` (re-exported by `ProgressStages.jsx`) so pure code can import it. Rendered server-side and unit-tested (5 tests), not seen in a browser) Reuse the new progress components in `PlanningJobStatusPage` (`PLANNING_STAGES`) once Phase 1's generalization is done.

---

### One sidebar (requested 2026-09-29, not one of the planned tasks)

The repository sidebar and the plan outline were two separate columns. They are now one sidebar.

- [x] **Repository dropdown.** `Sidebar.jsx`: a `RepositoryPicker` trigger showing the current repository (`owner/name`, full URL in the tooltip), and a panel with one item per repository and **New analysis inside it**. It is a disclosure of buttons, not a listbox: Enter or click opens it on the current repository, Arrow keys, Home and End move, Escape closes and returns focus to the trigger, and clicking or tabbing outside closes it.
- [x] **Picking a repository opens its newest plan** (`GET /api/agent2/projects/{id}/plans`, newest first), so the plan's sections appear straight away. A repository with no plan opens its page, where Generate plan lives. The old per-repository expand/collapse list is gone.
- [x] **Plan sections below the dropdown.** `OutlineSlot.jsx` gives a page a slot in the sidebar; `PlanDocumentView` portals the outline (`Toc variant="sidebar"`) into it, so the document is now two columns (document and rail) instead of three. With no slot the outline falls back to its own column. On narrow screens the sidebar is the existing drawer (button renamed "Menu"), and choosing a section closes it.
- [x] The current repository's **Runs and plans** stay, collapsed by default, under the dropdown.
- Breakpoints for the document without its outline column: the rail needs ~900px of content width (it was ~1180px with the outline as a column).
- **Checked in a browser (2026-09-29)**, on the same fake-data harness as the Definition of done pass: dropdown open, Arrow/Home/End/Escape/Enter with real key presses, pick a repository with no plan (its page, empty slot), pick one with a plan (newest plan, 8 sections in the sidebar), New analysis (start page), light and dark, and layout metrics at 1910/1440/1100/1024/390 with no horizontal scroll. The drawer's state (Menu opens it with a backdrop, choosing a section or the backdrop closes it) was checked by class, not by watching it slide, because the automation tab is hidden and freezes transitions.
- **Not done:** the plan's `Runs and plans` block was only looked at while collapsed; `Sidebar.css` still has old hardcoded colours in the rules I kept (Definition of done, colours).

---

## Definition of done (each task)

- [x] (checked 2026-09-29: build passes, 121 of 121 `node --test` tests pass across `src/`, re-checked after the mounting and CSS fixes) `npm run build --prefix frontend` passes; `node --test` passes for touched pure modules.
- [ ] (mostly done 2026-09-29, left open. **Method:** real Chrome, the real FastAPI routers over the test suite's fake DB (`_seed_full_plan` plus Agent 1 rows), auth bypassed, Vite dev server; the harness was scratch and is not in the repo. **Seen:** dark at full window (1910px), light at 390px; layout metrics at 1910/1440/1024/768/390 in both themes' iframes, no horizontal page scroll at any of them; keyboard Tab order (outline, toolbar, section headings) with a visible 2px outline on every stop and on the section controls; outline click scrolls the heading to 105px and moves focus; no console errors. **Not done:** light at 1440/1024 was measured but not looked at; reduced motion was not exercised (the shimmer and chevron rules exist, and `styles.css` has a global one); the Tab walk covered the read-only controls, and the write controls' focus styles were only read from `styles.css`. **Found and fixed here:** sticky outline/rail scrolled away, headings landed under the header, scrollspy ran late, breakpoints too wide for the sidebar. **Test-tab quirk:** the automation tab reports `visibilityState: hidden`, so `requestAnimationFrame` and smooth scroll do not run; the outline was tested with rAF and smooth scroll polyfilled) Browser pass in light and dark at 1440 / 1024 / 390px: no horizontal page scroll, focus visible everywhere, keyboard-only walkthrough works, reduced motion honoured.
- [ ] (partly done 2026-09-29, left open: **loading** was seen (skeleton, same 248 / 821 / 280px columns as the loaded document, shimmer running) and so was **error** (an alert with the cause, Retry and Back, for an unknown plan id). Also seen and working: stale (banner, all 8 sections still shown, no write controls), draft, in review, approved ("Approved by you at ...", controls gone, Generate code enabled), the facade acknowledge, reorder and merge overrides each opening a new version, and Copy as Markdown (8 sections, note included). **Empty is not mounted:** `PlanEmpty` exists but `PlanPage` never shows it, since a plan page always has a plan; "no plan yet" is the project page's plans list, which keeps its own text) Loading, empty and error states exist and were seen.
- [ ] (partly checked 2026-09-29, left open: **new tables/endpoints: none added**, the frontend calls only existing routes and the planned ones are in `design_agent2.md` §16 and `tasks_agent2.md` Phase 7. **Colours:** the new `agent2/` code has none, only two black shadow `rgba(0,0,0,.x)` in `agent1.css:104` and `ui/ui.css:37`. Still hardcoded elsewhere: `#ff5c4d` and `rgba(...)` in `shared.jsx` (`RISK_NODE_STYLE`) and `PlanPage.jsx` (`PLAN_GRAPH_FACADE_COLOR`), both moved verbatim from `main.jsx`, and many in `Sidebar.css`, which is not part of this redesign. Fixing those means choosing tokens, not done) No hardcoded colours; no new table or endpoint without a design-doc change.

## Suggested order

Phase 0 → Phase 1 → **A1.1–A1.3** (visible, no backend changes) → **A2.1** (decisions) → **A2.2–A2.3** on available data → A1.6 / A2.4 design changes in parallel with their backend work → A1.4 Pause/Cancel and notes last.

## Locked config

Locked with the recommended defaults (2026-09-29). Source: the playground's `configJson` / `configCss` (`playground/src/settings.js`).

**Copy JSON**

```json
{
  "theme": "dark",
  "sans": "geist",
  "mono": "geist-mono",
  "fontSize": 14,
  "lineHeight": 1.5,
  "density": "comfortable",
  "accent": "lavender",
  "radius": "rounded",
  "maxWidth": 1200,
  "progressStyle": "segmented",
  "sidebarStyle": "progress"
}
```

**Copy CSS** (dark, the default theme)

```css
:root[data-theme="dark"] {
  --pg-font-sans: 'Geist Variable', 'Geist', system-ui, sans-serif;
  --pg-font-mono: 'Geist Mono Variable', 'Geist Mono', ui-monospace, monospace;
  --pg-fs: 14px;
  --pg-lh: 1.5;
  --pg-r-sm: 6px;
  --pg-r: 10px;
  --pg-r-lg: 14px;
  --pg-pad-row: 8px;
  --pg-pad-card: 16px;
  --pg-gap: 16px;
  --pg-gap-lg: 24px;
  --pg-section: 32px;
  --pg-maxw: 1200px;
  --pg-accent: #a0a1cf;
  --pg-accent-fg: #1d2130;
  --pg-success: #7fd6a0;
}
```

**Copy CSS** (light, same settings with the theme switched)

```css
:root[data-theme="light"] {
  --pg-font-sans: 'Geist Variable', 'Geist', system-ui, sans-serif;
  --pg-font-mono: 'Geist Mono Variable', 'Geist Mono', ui-monospace, monospace;
  --pg-fs: 14px;
  --pg-lh: 1.5;
  --pg-r-sm: 6px;
  --pg-r: 10px;
  --pg-r-lg: 14px;
  --pg-pad-row: 8px;
  --pg-pad-card: 16px;
  --pg-gap: 16px;
  --pg-gap-lg: 24px;
  --pg-section: 32px;
  --pg-maxw: 1200px;
  --pg-accent: #55569a;
  --pg-accent-fg: #ffffff;
  --pg-success: #16724a;
}
```

**Variants that ship:** segmented progress and the progress sidebar, one each. The linear and ring progress styles and the minimal and numbered sidebar styles are not ported (no dead variant code). Font, accent, density and radius alternatives, plus the tweak bar itself, stay in `playground/` only; production uses the locked values above.
