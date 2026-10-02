# tasks_agent3_delivery_and_parity.md — GitHub PR delivery + proof-of-migration

Personal scope, pulled out of the shared `tasks_aamf.md` backlog so a session working
on this doesn't need to read the other ~50 items in that file (owned by other
people/areas). Same CLAUDE.md rules apply regardless of which file tracks the idea:
update the owning design doc (`design_agent3.md`) **before** writing code, then move
the item into `tasks_agent3.md` proper with real backend+frontend task breakdowns,
then build. This file only tracks the decision and scope, same as `tasks_aamf.md`
does for its own items.

Each item has three checkboxes: **designed** (design doc updated), **tasked**
(backend + frontend tasks added to `tasks_agent3.md`), **done**.

---

## 1. GitHub PRs instead of ZIP files

**Why:** enterprise customers want the generated output integrated into their own
workflow, not a zip they have to manually apply. Generate branches → phase-based
commits → PRs → CI tests → human review, so the product feels like a real
engineering tool rather than a demo.

**Current state:** output today is a zip only. `assemble.py::run_assemble_stage`
builds the project tree; `run_final_test_stage` zips it deterministically and sets
`execution_jobs.output_zip_path`; `GET /api/agent3/executions/{id}/download` hands
back a 5-minute signed URL. None of this touches git or GitHub at all.

**Nearest existing idea (not this one — a prerequisite):** `tasks_aamf.md` D4 ("Git
history inside the zip") proposes making the assembled project a real git repo with
one commit per unit, in plan order — but explicitly scopes out pushing anywhere
("pushing to remote repos stays out of scope"). This item picks up exactly where D4
leaves off.

- [x] Designed — `design_agent3.md` §15, 2026-10-01
- [x] Tasked — `tasks_agent3.md` Phase 7, 2026-10-01
- **Decisions made 2026-10-01** (all recorded in design §15.1): separate target repo, never the legacy repo; stacked PRs (scaffold PR + one per phase), not flat; D4 folded in via one shared commit plan; `BackgroundTasks` plus a lazy 15-minute stale sweep, not a pgmq queue; Git Data REST API, no local push; a fixed `ci.yml` in the scaffold; the zip is unchanged. **Added the same day:** `delete_branch_on_merge` is enforced at connect and on every delivery (design §15.13), and commit/tree SHAs come from one pure object builder with every GitHub create call verified against it (§15.14); what is and isn't SHA-identical to the zip is in §15.14 (contents always; trees only for a truly empty repo; commits for a truly empty repo only if GitHub stores the initial commit exactly as sent, unverified until the live test; no force-update).
- [ ] Done
- Scope to work out during design (not yet decided — these are the open questions,
  not a spec):
  - **Recommended:** reuse Agent 1's exact token storage mechanism
    (`backend/document_analyzer/github_token.py`, Supabase Vault via
    `vault.secrets` — `store_github_token`/`resolve_github_token`/
    `delete_github_token`), but with a **different lifecycle**. Agent 1's token is
    per-job, read-only (clone), deleted the moment that job ends. This one needs
    write access (repo + pull-request scope) and must persist across many
    executions of the same project, so: one `github_delivery_token_ref` per
    **project** (not per execution job), stored the same Vault way, kept until the
    user disconnects/rotates it. Same storage tech, different scope + lifetime —
    don't build a second secrets mechanism.
  - D4's git-history-in-the-zip piece is the natural first half: real commits, one
    per unit or per phase, in plan order, inside the assembled tree — decide whether
    to build D4 first as its own standalone piece or fold it into this item.
  - **Recommended:** don't gate delivery on all-green. This system's whole
    philosophy (CLAUDE.md: "grounding failures never block... the rest keeps
    processing") is to surface problems, not refuse to proceed — a hard gate here
    would be inconsistent with that, and a reviewer may want to see/iterate on
    partial progress. Require only that the job reached `done` (not still
    running); put the real per-unit verdict table (same one `assemble.py` already
    writes into `README.md`) directly in the PR description, and actually trigger
    the customer's real CI to run the tests — that CI result is the strongest
    "proof it works" signal, stronger than Agent 3 asserting it.
  - **Recommended:** one PR per phase, not one for the whole job. Agent 2 already
    approves plans phase by phase, so this matches the existing review mental
    model and keeps each PR a reviewable size instead of one giant diff. Branch
    name like `migrate/phase-{n}-{slug}`, PR description linking back to the
    plan/phase.
  - **Recommended:** leave existing PRs alone when a plan goes stale — once
    something is pushed to the customer's real repo, it's no longer Agent 3's to
    manage (don't auto-close a PR a human might be reviewing). Apply the same
    `409` + banner pattern already used everywhere else for a stale plan (submit,
    approval, overrides, Generate Code) to block *new* delivery attempts only.
  - New endpoint(s) needed (e.g. `POST /executions/{id}/deliver` or similar) and the
    paired frontend action, per CLAUDE.md's "every endpoint needs a frontend task"
    rule. **Recommended:** make this a lightweight async background task (FastAPI
    `BackgroundTasks` or similar), not a synchronous call in the request handler —
    CLAUDE.md's "async by default" rule applies, even though pushing commits +
    opening a PR is bounded (seconds), not the minutes-to-hours ingestion/
    planning/execution already use a full queue+worker for. Return `202` and let
    the frontend poll, same pattern as every other async action in this app.
  - ~~Whether this replaces the zip download or sits alongside it~~ **Decided:**
    keep both. The zip stays exactly as-is (`output_zip_path`,
    `GET /executions/{id}/download`) for a customer with no GitHub connected, or
    for quick inspection; GitHub PR delivery is a new, additional path, not a
    replacement. `GenerateCodeAction`/`ExecutionResultsPage` should offer PR
    delivery as a separate action alongside the existing download button, gated
    on whether the project has a GitHub connection configured — not gated on
    (and not replacing) the zip at all.

## 2. Proof that the migration works

**Why:** buyers care whether the new system actually behaves like the old one, not
just that it "has tests." Show the evidence trail: legacy code → new code → tests →
logs → results. Longer term, differential testing against the real PHP system could
become a major advantage (no one else doing this kind of migration can easily prove
behavioral equivalence).

**Current state — the easy half already exists:** `ExecutionUnitPage` (frontend)
already shows exactly this evidence trail, per unit: legacy source next to the
generated code (via `GET /api/agent3/executions/{id}/units/{cluster_id}`), the
contract + behaviour tests, the sandbox log, and the symbol map. This was reviewed
and had its readability/accessibility issues fixed already (Phase 6 task 6 — long
logs now scroll in a bounded box, the file picker has proper keyboard state). This
part doesn't need rebuilding, just maybe surfacing more prominently as a sales
artifact (e.g. a shareable report view, not just an authenticated in-app page).

**The hard half — differential testing — does not exist, but has a reserved spot:**
`design_agent3.md` §14 ("Later: differential testing") already anticipates this:
`unit_executions.test_summary` is deliberately keyed by suite name (`contract`,
`behaviour`) specifically so a third `differential` suite can be added later without
a schema migration. It also already names the real constraint: a differential suite
would need **its own separate, network-isolated, resource-limited runner — with real
PHP, a WordPress core stub, and a database — and it must never share the Python
runner** (CLAUDE.md's "Agent 3 runs only the Python it generated, never the legacy
code" rule would otherwise be violated). This is tracked in the shared backlog as
`tasks_aamf.md` E1, under "Phase E — Long-term parity" — i.e. already flagged there
as the single hardest, furthest-out item in the whole file. Zero design exists beyond
that one paragraph.

- [ ] Designed (`design_agent3.md` §14 needs to become a real section, not a
  placeholder — this needs its own design pass, separate from the Python sandbox's
  design in §7)
- [ ] Tasked (`tasks_agent3.md`)
- [ ] Done
- Scope to work out during design:
  - The PHP sandbox: same container-security bar as the Python one (§7) — no
    network, read-only root, capped resources, a timeout — but running real PHP
    plus whatever WordPress-core/`$wpdb` stub is needed to execute the *original*
    function with the *same* recorded inputs.
  - What "same inputs" means for a `public_interface` entry: probably recorded from
    real traffic/fixtures during Agent 1 ingestion, or synthesized from the
    `test_contract`'s own shape — needs a concrete decision, not just "replay it."
  - Noise-aware comparison: PHP and Python will never be byte-identical (timestamps,
    object identity, float formatting) — the comparison needs to tolerate expected
    noise while still catching real behavioral divergence. This is the actual hard
    design problem here, not the sandboxing.
  - The parity report shape: a new row/column somewhere reviewers can see per unit —
    "differential: N/M inputs matched" — and how a mismatch should be surfaced (a
    new flag? a new status? advisory only, same as the current "sandbox verdicts are
    advisory" caveat in §7?).
  - Near-term, cheaper version to consider before the full PHP sandbox: a smaller
    "proof" artifact that doesn't require differential testing at all — e.g. a
    shareable, read-only report page built from data that already exists (legacy
    source, generated code, test results, pass/fail counts) as a sales/demo
    artifact, decoupled from whether real differential testing ever gets built.
