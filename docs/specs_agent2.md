# Agent 2 (Plan) — Specification

Functional and non-functional spec for Agent 2. The *how* (tables, algorithms, API shapes) lives in `design_agent2.md`; the build order lives in `tasks_agent2.md`. Section references (§N) in this file point into this file unless prefixed with a file name.

## 1. Overview

Agent 2 sits between Agent 1 (Understand) and Agent 3 (Execute). It reads the knowledge base Agent 1 persisted for one ingestion job — entities, edges, entity-level summaries + embeddings — and turns it into an **ordered, human-reviewable migration plan**.

Agent 2 owns three outputs:

1. **Migration units** — fine-grained clusters of entities that Agent 3 migrates and verifies one at a time.
2. **Phases** — coarse-grained clusters of migration units that a human reviewer signs off on.
3. **A migration order** — phases in dependency order, and units in dependency order within each phase, with every circular dependency either auto-merged or explicitly flagged (§5 FR5).

Method: a GraphRAG-style pipeline — Leiden community detection over Agent 1's call graph, bottom-up into two levels, plus an LLM-generated, grounding-checked report per cluster used as the review/retrieval unit instead of raw code.

### Glossary

| Term | Meaning |
|---|---|
| **Entity** | One `extracted_entities` row (Agent 1). |
| **Migration unit** (a.k.a. *unit*, *fine cluster*) | A `clusters` row with `level = unit`. The literal boundary of one Agent 3 migration task. |
| **Phase** (a.k.a. *coarse cluster*) | A `clusters` row with `level = phase`. A group of units approved together. Deliberately **not** called "subsystem" — Agent 1 already uses `subsystem` for its directory-based summary level (`design.md` §2), which is unrelated. |
| **Cluster report** | The LLM summary of one cluster (either level), grounding-checked. |
| **Planning job** | One asynchronous run of the Agent 2 pipeline against one Agent 1 ingestion job. |
| **Plan** | One `migration_plans` row: a versioned, reviewable output of a planning job. |
| **Test contract** | Per-unit, fact-derived description of what an equivalence test must verify (§5 FR8). Not a test. |

## 2. Pipeline position & trigger

- Agent 2 is independent of Agents 1 and 3: it communicates **only** through Supabase. It never imports or invokes Agent 1's pipeline, and Agent 1 never enqueues work for Agent 2.
- **Trigger (decided): user-initiated.** A user clicks "Generate plan" on a project; the API enqueues a planning job onto Agent 2's own pgmq queue (`agent2_planning_jobs`). There is no automatic trigger on Agent 1 completion.
- **Gate:** a planning job can only be created against an `ingestion_jobs` row with `status = done`. By default the API uses the project's `projects.latest_job_id`.
- Agent 3 reads **only** plans with `status = approved` (§5 FR7). It never reads drafts, clusters or reports of an unapproved plan as migration instructions.

## 3. Inputs (read-only, owned by Agent 1)

Agent 2 treats Agent 1's output for a given `job_id` as ground truth and **never writes to Agent 1 tables**. It does not re-parse the repo or re-summarize entities.

| Source (Agent 1, `design.md` §2) | What Agent 2 uses |
|---|---|
| `projects` | `id`, `latest_job_id` — plan scoping and staleness (§5 FR7). |
| `ingestion_jobs` | `id`, `project_id`, `status` (must be `done`). |
| `extracted_entities` | Graph nodes: `id`, `entity_type`, `name`, `file_path`, `raw_facts` (signatures, class membership, `extends`/`implements`, unresolved `calls`, line numbers). |
| `entity_edges` | Graph edges. Only `calls` and `hook_binding` rows with a non-null `to_entity_id` become weighted edges. Call frequency = number of `calls` rows per `(from_entity_id, to_entity_id)` pair — Agent 1's PHP extractor emits one row per call site, so no count column is needed. |
| `knowledge_summaries` (`level = entity`) | `summary_text`, `risk_level`, `embedding` (`vector(384)`), `grounding_status` per entity. |

Known properties of this input that the spec must handle (details in `design_agent2.md` §3):

- There are **no inheritance edges** (`extends`/`implements` live in `raw_facts` only) and **no class↔method edges** (a method is a `function` entity naming its class in `raw_facts`).
- `include` edges always have a null target, and repo-external calls are not edges at all — neither can be used as graph edges.
- The PHP and WordPress extractors can emit **duplicate entities** for the same function (not reconciled by Agent 1).
- `hook` entities are high-degree hubs, and `schema` entities (CPTs/meta) usually have no edges.

## 4. Outputs

Written by Agent 2's worker into its own tables (`design_agent2.md` §2), all scoped by `org_id` and a `planning_job_id`:

- Units and phases (`clusters`, `cluster_members`), with nesting: every unit belongs to exactly one phase.
- A grounding-checked report per cluster (`cluster_reports`), with `risk_level` and an embedding from the same pinned model Agent 1 uses.
- The directed cluster dependency graph with cycle flags (`cluster_edges`).
- A versioned plan (`migration_plans`, `migration_plan_items`) holding the order and one test contract per unit.
- The review trail (`phase_approvals`, `plan_overrides`).

## 5. Functional requirements

### FR1 — Planning job lifecycle
- `POST` creates a `planning_jobs` row (`queued`) and enqueues it. The worker moves it through `running` with a `progress_stage` of `graph → cluster → report → order`, ending in `done` (a `draft` plan exists) or `failed` (with `error`).
- Rejected with `409` if the source ingestion job isn't `done`. Only one non-terminal planning job may exist per project at a time.
- Every stage is idempotent for its planning job: re-running a stage deletes and rewrites that stage's own rows for that `planning_job_id` only.

### FR2 — Graph construction
- Build one weighted graph per planning job from §3's inputs. Nodes are **canonical entities**: duplicates across extractors, matched on `(file_path, name, entity_type)`, collapse to one node, and every original entity id is kept as a member.
- Edge weight blends structural strength and semantic similarity: `w = log1p(call_count) + α · cos_sim(embedding_a, embedding_b)`. The similarity term is dropped (structure only) when either endpoint's entity summary is `unverified` or has no embedding. This is the decided default for low-confidence summaries.
- Methods of the same class are tied together with a synthetic containment weight, so a class is never split across units.
- Hub nodes (degree above a configurable percentile, typically hooks such as `init` and widely used helpers) are removed before clustering. They form a **foundation unit** that is ordered first.
- Isolated nodes (no edges after filtering) are attached to the unit that holds most of their file's other entities, or grouped per file if that file has no clustered entities.

### FR3 — Hierarchical clustering
- Exactly **two levels**: units (fine) and phases (coarse).
- Units come from Leiden run on the undirected, symmetrized graph. Any community larger than `max_unit_size` is split by re-running Leiden on its induced subgraph until it fits. Size is measured in entity count.
- Phases come from running Leiden on the graph of units with their aggregated weights. By construction every unit nests in exactly one phase.
- Clustering is **deterministic**: a fixed seed and the full parameter set are persisted on the planning job, so identical input and parameters give identical clusters.

### FR4 — Cluster reports
- Every unit gets one LLM report built from its members' names, types and entity-level `summary_text` (never `raw_facts`, never another cluster's members). Every phase gets one built from its units' reports. The payload is capped, with the same "every call stays small" rule as Agent 1's FR4.
- `risk_level` is the deterministic maximum of the children's risk (Agent 1's rollup rule), never an LLM judgment.
- Each report goes through the grounding check with a retry cap. A report still ungrounded after the cap is stored `unverified` and **never blocks** the planning job.
- Each report is embedded with the pinned embedding model from `embedding_config`.

### FR5 — Dependency graph & circular dependencies
- The directed cluster graph comes from collapsing cross-cluster entity edges. Direction: if A `calls` B, A depends on B and B migrates first (callee-first). For `hook_binding` (callback → hook), the unit that owns the hook migrates first.
- Cycles of **any length** are found as strongly connected components (SCCs), not just two-way edges.
- **Cycle policy (decided):**
  1. A unit-level cycle whose units all sit in one phase and whose merged size is ≤ `max_unit_size` is **auto-merged** into one unit (`is_merged_scc = true`), and the edges are flagged `merged`.
  2. Otherwise (the merged unit would be too large, or the cycle is between phases), the edges are flagged `needs_facade`. The plan **cannot be approved** until a human resolves each flag, either by acknowledging a facade/adapter or by recording a merge/split override (FR7).

### FR6 — Migration order
- Phases are topologically sorted on the phase-level graph after SCC condensation. Units are then sorted within each phase. The foundation unit comes first.
- Ties are broken deterministically: lower `risk_level` first, then smaller size, then cluster id.
- The order is persisted per unit as `(phase_order, unit_order)`.

### FR7 — Human review gate
- Plan states: `draft → in_review → approved | rejected`, plus `stale`.
- Approval happens **per phase**. A plan becomes `approved` only when every phase is approved **and** there are no unresolved `needs_facade` flags.
- Reviewer edits never mutate `clusters` or `cluster_reports`. They are recorded as `plan_overrides` (`merge`, `facade_ack`, `reorder`). Splitting a unit is out of the MVP (decided 2026-09-23, `design_agent2.md` §9.2). Applying overrides re-runs only the dependency-graph and ordering stages and creates a **new plan version**, and the old version is kept.
- A plan is pinned to its source `job_id`. When `projects.latest_job_id` moves to a newer job, every older non-rejected plan for that project is marked `stale`. It is not deleted and not rewritten, and it cannot be newly approved.
- Approval is recorded with the reviewer's `auth.uid()` and a timestamp. Any authenticated member of the org can review for the MVP (there is no role model yet).

### FR8 — Test contract (validation hand-off)
- For each unit, Agent 2 emits a test contract taken from grounded Agent 1 facts only:
  - the unit's public interface (signatures of members called from outside the unit, from `raw_facts`);
  - incoming call sites from other units;
  - side effects and risk factors (from entity risk and `raw_facts` signals);
  - external dependencies (unresolved calls in `raw_facts.calls`, hooks it binds).
- **Agent 3 owns** turning a contract into tests and running them. Agent 2 never executes, samples or records legacy code (see Agent 1 `specs.md` NFR2: no sandbox).

## 6. Non-functional requirements

- **NFR1 — Async.** All work runs on the `agent2_planning_jobs` pgmq queue and a separate worker process. The API only enqueues and reads.
- **NFR2 — Multi-tenancy.** Every Agent 2 table carries `org_id` with an RLS policy (`org_id = current_org_id()`). The worker writes through the service-role connection, stamps `org_id` from the planning job row (never from repo content), and filters updates by both `id` and `org_id`. Same trust boundary as Agent 1 NFR3.
- **NFR3 — Determinism & auditability.** The seed and parameters are persisted, and every plan version keeps the planning job that produced it plus the overrides it applied.
- **NFR4 — Cost control.** Report generation runs with a concurrency cap at the unit level, bounded payloads, and one call per cluster plus grounding retries. Phases are few and generated sequentially.
- **NFR5 — Embedding consistency.** Cluster report embeddings use the active `embedding_config` pin. The worker refuses to start on drift, the same check Agent 1's worker makes.
- **NFR6 — Scale target.** Must handle Agent 1's load-test scale (5,000–10,000+ entities) in memory in one worker process.

## 7. Out of scope

- Generating, executing or verifying code (Agent 3).
- Writing to any Agent 1 table, or fixing Agent 1 data gaps inside Agent 2 (see `design_agent2.md` §13).
- Query-time retrieval ("local/global search"). Agent 2 produces the retrieval units (reports + embeddings); the query mechanics belong to whoever consumes them (review UI, Agent 3).
- Multi-reviewer roles and permissions beyond "authenticated org member".
- Automatic re-planning on re-ingestion (a stale plan is only marked, never regenerated automatically).

## 8. Decided

- Trigger: user-initiated `POST`, own pgmq queue `agent2_planning_jobs`.
- Levels: exactly two (unit, phase), built bottom-up so they nest.
- Cycle policy: SCC condensation; auto-merge when the merged unit fits under `max_unit_size` within one phase, otherwise flag `needs_facade`, which blocks approval.
- Unverified entity summaries: drop the similarity term, keep structural weight.
- Validation: Agent 2 emits test contracts only, and Agent 3 owns test generation and execution.
- Clustering library: `leidenalg` + `python-igraph`, with `networkx` for SCCs and topological sort (see `skills.md`).
- Plans are versioned, pinned to a `job_id`, and marked `stale` rather than mutated.

## 9. Open questions (need empirical tuning, not design decisions)

- Default `α` (similarity weight) — starting default 0.5; tune on a real WordPress repo.
- Default `max_unit_size` — starting default 25 entities as a "one day of work" proxy; revisit once Agent 3 throughput data exists. A LOC-based measure (from `raw_facts` line spans) may replace entity count.
- Default hub percentile — starting default 99th percentile of degree.
