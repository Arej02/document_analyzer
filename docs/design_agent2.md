# Agent 2 (Plan) — Technical Design

Architecture detail supporting `specs_agent2.md` (referenced as *specs FRn*). Like Agent 1's `design.md`, this is the doc to update whenever a design decision changes: code follows this file, not the other way around. Section references (§N) point into this file unless prefixed.

## 1. High-level flow

```
User clicks "Generate plan" on a project
        │
        ▼
 [FastAPI: POST /api/agent2/projects/{project_id}/plans]
        │  gate: source ingestion_jobs.status = done (409 otherwise)
        ├─► planning_jobs row (status: queued)
        └─► pgmq.send('agent2_planning_jobs', {planning_job_id, org_id})
        │
        ▼
 [Agent 2 worker: `uv run document-analyzer-planner`]
        │
        ├─ stage graph   ─► load entities/edges/summaries → canonical weighted graph (in memory)
        ├─ stage cluster ─► clusters, cluster_members (units, then phases)
        ├─ stage report  ─► cluster_reports (LLM + grounding + embedding)
        └─ stage order   ─► cluster_edges (SCC / cycle flags) → migration_plans v1 (draft)
                             + migration_plan_items (order + test_contract)
        │
        ▼
 planning_jobs.status = done | failed
        │
        ▼
 Frontend: plan review (phase → unit → entity drill-down), overrides, per-phase approval
        │
        ▼
 migration_plans.status = approved  ──►  (Agent 3 reads approved plans only; not built)
```

Agent 2 never reads Agent 1 code paths at runtime and never writes Agent 1 tables. The only coupling is (a) reading Agent 1 tables and (b) importing a small set of **pure** helper functions (§6.3).

**Orchestration:** same pattern as Agent 1 (`design.md` §3), a single long-lived worker process polling its own pgmq queue and running stage functions in order. Agent 1 turned out not to need LangGraph, and Agent 2 doesn't either. If LangGraph is adopted later, each stage becomes one node, with no change to this data model.

**Code location:** `backend/document_analyzer/agent2/` (API router `agent2/router.py`, worker `agent2/worker/`), with a `[project.scripts]` entry `document-analyzer-planner`.

## 2. Data model (Supabase Postgres)

Rules shared by every table below:

- Columns `org_id uuid not null`, `created_at`, `updated_at`.
- RLS enabled, with a `select` policy `org_id = current_org_id()` (reusing Agent 1's `current_org_id()`, `design.md` §1a). Writes are service-role only, except the three review tables marked *user-writable*, which also get `insert` policies `org_id = current_org_id()`.
- The worker writes on the service-role connection with `insert_scoped`/`insert_many_scoped` (`supabase_client.py`), stamping `org_id` from the **planning job row**. Every update filters on both `id` and `org_id`.
- One migration file per table group, under `supabase/migrations/2026MMDD..._agent2_*.sql`.

### `planning_jobs`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | returned to the frontend on submit |
| `project_id` | uuid, FK → `projects` | |
| `source_job_id` | uuid, FK → `ingestion_jobs` | must be `done` at creation; defaults to `projects.latest_job_id` |
| `status` | enum(`queued`,`running`,`done`,`failed`) | |
| `progress_stage` | enum(`graph`,`cluster`,`report`,`order`), nullable | |
| `error` | text, nullable | |
| `params` | jsonb | `{seed, alpha, max_unit_size, hub_percentile, leiden_resolution}`, frozen at creation from §12 env defaults |
| `graph_stats` | jsonb, nullable | node/edge counts, canonicalized-duplicate count, hub count, isolated count, for audit |

Partial unique index: one row with `status in (queued, running)` per `project_id` (specs FR1).

### `clusters`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | |
| `planning_job_id` | uuid, FK | |
| `level` | enum(`unit`,`phase`) | |
| `parent_cluster_id` | uuid, FK → `clusters`, nullable | set for units (→ phase); null for phases |
| `kind` | enum(`community`,`foundation`,`merged_scc`,`isolated_file`) | how it was formed (§3, §7) |
| `size` | int | canonical member count |
| `label` | text, nullable | short title from the report |

### `cluster_members`
| column | type | notes |
|---|---|---|
| `cluster_id` | uuid, FK → `clusters` (unit level only) | |
| `entity_id` | uuid, FK → `extracted_entities` | **every** original entity id, including duplicates collapsed into one canonical node |
| `canonical_entity_id` | uuid, FK → `extracted_entities` | the node representative (§3.2) |

PK `(cluster_id, entity_id)`. Phase membership is derived through `parent_cluster_id`, never stored twice.

### `cluster_reports`
| column | type | notes |
|---|---|---|
| `cluster_id` | uuid, PK, FK → `clusters` | one report per cluster |
| `summary_text` | text | |
| `risk_level` | enum(`low`,`medium`,`high`) | deterministic rollup (§6) |
| `risk_factors` | jsonb | children that carried the max level |
| `embedding` | vector(384) | pinned model, same as `knowledge_summaries.embedding` |
| `grounding_status` | enum(`verified`,`unverified`) | |

### `cluster_edges`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | |
| `planning_job_id` | uuid, FK | |
| `level` | enum(`unit`,`phase`) | |
| `from_cluster_id` / `to_cluster_id` | uuid, FK → `clusters` | `from` **depends on** `to` (`to` migrates first) |
| `call_count` | int | sum of underlying `calls` rows |
| `hook_binding_count` | int | |
| `weight` | float | aggregated §3 weight |
| `flag` | enum(`none`,`merged`,`needs_facade`) | §7 |
| `scc_id` | int, nullable | shared by edges of the same SCC |

### `migration_plans`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | |
| `project_id` | uuid, FK | |
| `planning_job_id` | uuid, FK | the clustering this plan is built on |
| `source_job_id` | uuid, FK → `ingestion_jobs` | pinned snapshot (specs FR7) |
| `version` | int | 1 from the worker, then +1 per override application; unique `(planning_job_id, version)` |
| `parent_plan_id` | uuid, FK → `migration_plans`, nullable | previous version |
| `status` | enum(`draft`,`in_review`,`approved`,`rejected`,`stale`) | §9 |
| `open_facade_flags` | int | count of `needs_facade` edges without a `facade_ack`; must be 0 to approve |
| `approved_by` | uuid, FK → `auth.users`, nullable | |
| `approved_at` | timestamptz, nullable | |

### `migration_plan_items`
| column | type | notes |
|---|---|---|
| `plan_id` | uuid, FK | |
| `cluster_id` | uuid, FK → `clusters` (unit) | |
| `phase_cluster_id` | uuid, FK → `clusters` (phase) | may differ from `clusters.parent_cluster_id` after a `reorder`/`merge` override |
| `phase_order` | int | |
| `unit_order` | int | within the phase |
| `test_contract` | jsonb | §10 |

PK `(plan_id, cluster_id)`. Override-merged units appear as a synthetic row (`cluster_id` = the lowest merged id, with the merged ids in `test_contract.merged_cluster_ids`), so `clusters` stays immutable.

### `phase_approvals` *(user-writable)*
`plan_id`, `phase_cluster_id`, `status` enum(`approved`,`rejected`), `reviewer_id` (= `auth.uid()`, set server-side), `note`. Unique `(plan_id, phase_cluster_id)`.

### `plan_overrides` *(user-writable)*
`id`, `plan_id` (the version it was made against), `kind` enum(`merge`,`split`,`facade_ack`,`reorder`; `split` stays in the enum but the API refuses it, §9.2), `payload` jsonb (shape per kind, §11), `created_by` (= `auth.uid()`), `result_plan_id` (the version it produced; unique, so each version after 1 has exactly one override). `result_plan_id` was added once overrides were built: two overrides made against the same version *n* branch into separate versions, and a version's chain must only replay its own ancestors' overrides, which `plan_id` alone can't tell apart.

### Queue
`agent2_planning_jobs`, created with `pgmq.create` in its own migration. It is never shared with `agent1_ingestion_jobs` (`design.md` §3).

## 3. Stage `graph` — build the canonical weighted graph

### 3.1 Load (one planning job, one `source_job_id`, all queries filtered by `org_id`)
- Nodes: `extracted_entities where job_id = source_job_id`.
- Entity summaries: `knowledge_summaries where job_id = source_job_id and level = 'entity'` → `summary_text`, `risk_level`, `embedding`, `grounding_status` keyed by `entity_id`.
- Edges: `entity_edges where job_id = source_job_id and edge_type in ('calls','hook_binding') and to_entity_id is not null`, grouped by `(from_entity_id, to_entity_id, edge_type)` → `count`. Agent 1's PHP extractor emits one `calls` row per call site (`worker/php_extractor.py::_resolve_edges`), so `count` is the static call frequency.
- Ignored as edges: `include` (target always null), `global`, and null-target rows. Their facts remain available to test contracts through `raw_facts`.

### 3.2 Canonicalize duplicates
`PhpExtractor` and `WordPressExtractor` each emit their own entity for a hook callback (`tasks.md` Phase 2). Group nodes by `(file_path, name, entity_type)`. The representative is the one with a non-null entity summary, preferring `PhpExtractor`'s (it has the richer `raw_facts`), and all its duplicates' edges are rewritten onto it. `graph_stats.canonicalized` records the count. Methods additionally key on `raw_facts.class`, so two classes in one file that each define e.g. `__construct` stay separate nodes (WordPressExtractor names method callbacks `Class::method`, so this never blocks a real duplicate from collapsing). Self-loops created by the collapse are dropped.

### 3.3 Containment
For each class entity and each `function` entity whose `raw_facts` names that class (same `file_path`), add an undirected containment edge of weight `W_CONTAIN` (= the max observed edge weight). This is used **for clustering only** and never appears in `cluster_edges`, so a class is not split across units.

### 3.4 Hubs & isolated nodes
- **Hubs:** nodes whose total degree (distinct neighbours over `calls`/`hook_binding` edges, containment excluded) is ≥ the `hub_percentile` of the degree distribution (nearest rank over all nodes) and ≥ 10. If a hub is a class or one of its methods, the whole class (class + contained methods) moves to the foundation with it, so §3.3's "a class is never split" holds. Hubs keep their directed counts for §7; only their clustering weights are dropped. They are removed before Leiden and placed in a single `kind = foundation` unit. The foundation unit ignores `max_unit_size`; a reviewer can split it by override. It is ordered first (§8).
- **Isolated:** nodes with no remaining edges (typically `schema` CPT/meta entities and unbound hooks). After stage `cluster`, each goes to the unit holding the most entities from the same `file_path`, or else into one `kind = isolated_file` unit per file.

### 3.5 Weight
```
w(a,b) = log1p(call_count(a,b) + call_count(b,a) + hook_binding_count(a,b) + hook_binding_count(b,a))
       + α · max(0, cos_sim(emb_a, emb_b))      -- only if both summaries exist and are 'verified'
```
Clustering uses this symmetrized, undirected weight. The **directed** counts are kept separately for §7.

**Output.** `agent2/worker/graph.py::run_graph_stage` returns an in-memory `CanonicalGraph` (nodes, canonical → member ids, directed counts, undirected weights, foundation, isolated) for stage `cluster`, and writes `planning_jobs.graph_stats` = `{entities, nodes, canonicalized, call_rows, hook_binding_rows, directed_edges, weighted_edges, containment_edges, hubs, foundation, isolated}` (stage `cluster` adds `oversized_units`). Every load pages by `id` past PostgREST's 1000-row cap.

## 4. Stage `cluster` — Leiden, bottom-up, two levels

Leiden (via `leidenalg`, objective `RBConfigurationVertexPartition` with `resolution = params.leiden_resolution`, `seed = params.seed`, `n_iterations = -1` meaning run to convergence) partitions a graph into communities that are dense inside and sparse between. Each Leiden *run* repeats local moving → refinement → aggregation until no improvement remains. The refinement step (the improvement over Louvain) guarantees every community is internally connected.

1. **Units.** Run Leiden on the §3 graph. Any community with more than `max_unit_size` members is split by running Leiden again on its induced subgraph with resolution doubled, recursively, until every community fits. If a connected component still can't be split (a clique-like core), it stays oversized and is recorded in `graph_stats.oversized_units`. Then attach isolated nodes (§3.4) and add the foundation unit.
2. **Phases.** Collapse each unit to a super-node, with the edge weight between units = the sum of member weights, and run Leiden once more (same seed). Each resulting community is a phase. The foundation unit is always its own phase. **Directory fallback:** a unit with no edge to any other unit can only ever be its own Leiden community, which on a sparse graph means one approval per unit. Those units skip Leiden and are pooled into one phase per directory (the directory holding most of the unit's members, ties to the smallest path). Units with at least one unit edge are unaffected. These phases keep `kind = community`; `graph_stats.directory_phases` counts them.

**Implementation notes** (`agent2/worker/cluster.py`):
- **Classes are atomic.** Before Leiden, each class and its contained methods (§3.3) are contracted into one vertex, and community size counts its canonical members. That enforces "a class is never split" instead of just encouraging it with `W_CONTAIN`. A single class larger than `max_unit_size` becomes an oversized unit.
- **Split depth.** An oversized community is re-split with the resolution doubled up to 10 times (`MAX_SPLIT_DEPTH`). If it still doesn't fit, it is recorded in `graph_stats.oversized_units` as `[{cluster_id, size}]`.
- **Isolated attachment respects the cap.** An isolated node joins the unit with the most entities from its `file_path` only if that unit is still under `max_unit_size`. Otherwise it goes to that file's `isolated_file` unit, which is chunked at the cap.
- **Determinism.** Vertices are ordered by canonical entity id, and groups are sorted by member ids, so the output doesn't depend on row order. Cluster uuids are random, but member sets are identical.
- **Persistence.** The worker first deletes `clusters where planning_job_id and org_id`, and the `on delete cascade` FKs clear units, `cluster_members`, `cluster_reports` and `cluster_edges`. It then inserts phases, units and members with `insert_many_scoped`. `graph_stats` gains `units`, `phases` and `oversized_units`.

This is **bottom-up**, so units nest in phases by construction. It is intentionally not graspologic's top-down `hierarchical_leiden` (see `skills.md` for the library decision). There are exactly two levels (specs FR3), and a third is out of scope.

## 5. Two levels, two purposes

| Level | Used for | Sizing rule |
|---|---|---|
| Unit | What Agent 3 migrates and verifies one at a time | ≤ `max_unit_size` canonical entities (default 25, a "one day of work" proxy — specs §9) |
| Phase | What a reviewer approves | Emerges from Leiden on the unit graph; not set directly |

A unit-level cycle often lies entirely inside one phase. §7 uses exactly that property to decide between auto-merge and a flag.

## 6. Stage `report` — cluster reports

### 6.1 Units
One LLM call (Azure OpenAI, same client as Agent 1's `summarize.py::get_azure_openai_client`) per unit. The payload holds members' `entity_type`/`name`/`file_path`/`summary_text`, capped at `max_unit_size` entries (the foundation unit is truncated to the 50 highest-degree members plus a count), plus the unit's external dependency names (§10). It never includes `raw_facts` or other units' members. The prompt is adapted from GraphRAG's community-report prompt and asks for `{label, summary_text}`. Calls fan out through a bounded thread pool (`AGENT2_REPORT_CONCURRENCY`), the same pattern as `summarize_fanout.py`. Members are ranked by distinct-neighbour degree (then id) before the cap is applied. The external dependency names are the members' `raw_facts.calls` entries that didn't resolve to a `calls` edge (a name only, never the rest of `raw_facts`). The LLM calls run in the pool, and every Supabase read/write stays on the worker thread. The returned `label` is written to `clusters.label`.

A response that isn't `{label, summary_text}` JSON is retried twice (`REPORT_PARSE_RETRIES`). If it's still malformed, the stage raises and the planning job fails with that message. This is a broken LLM call, not a grounding failure, so the "never block" rule doesn't apply. A rerun deletes this job's `cluster_reports` first.

### 6.2 Phases
One call per phase, after every unit report exists, fanned out through the same `AGENT2_REPORT_CONCURRENCY` pool as unit reports (phases are independent of each other). The payload is the phase's unit labels and `summary_text`, capped at 50 units (largest first, `PHASE_REPORT_UNIT_CAP`) plus an omitted count.

### 6.3 Risk, grounding, embedding: reuse Agent 1's pure helpers
These are imported as library functions. Importing them is not an agent-to-agent call: they take inputs and return values with no Supabase access and no Agent 1 pipeline state.
- `risk_level`/`risk_factors`: `worker/risk_rollup.py::rollup_risk` over the children's risk (entity `risk_level` for units, unit reports for phases). The LLM never judges risk.
- Grounding: `worker/ground_retry.py::check_grounding_with_retry(facts=<the exact payload sent>, generate=<closure over the report call>)`. An exhausted cap stores `unverified` and never fails the planning job. The retry cap reuses `GROUNDING_RETRY_CAP`.
- Embedding: `worker/embed.py::embed_text(summary_text)`. The worker calls `worker/embedding_config_check.py::assert_embedding_config_in_sync` once at startup.

If any of these helpers need an Agent-2-specific change, move it into `document_analyzer/shared/` first instead of forking it.

## 7. Stage `order`, part 1 — cluster dependency graph & cycles

1. **Collapse.** For every underlying directed edge `a → b` (a calls b, or callback a binds hook b) with `unit(a) ≠ unit(b)`, add to unit edge `unit(a) → unit(b)`, meaning `unit(a)` depends on `unit(b)`. Do the same at phase level. Containment edges (§3.3) are excluded.
2. **SCCs.** Run `networkx.strongly_connected_components` on the unit graph. Every SCC with more than one unit is a cycle, whatever its length.
3. **Policy (decided, specs FR5):**
   - All SCC units share one phase **and** their combined size ≤ `max_unit_size` → **auto-merge**: write a new `clusters` row (`kind = merged_scc`, same phase, members re-pointed in `cluster_members`), and flag the SCC's edges `merged`. The originals are kept for audit, with a `merged_into` field in `graph_stats`.
   - Otherwise → flag every edge in the SCC `needs_facade`, and `migration_plans.open_facade_flags += 1` per SCC. Phase-level SCCs (cycles between phases) are always `needs_facade`.
4. **Condense.** For ordering, each remaining SCC is treated as one node in the condensation DAG (`networkx.condensation`), so a topological sort always exists, even while flags are open.

Auto-merge writes to `clusters` during the initial planning job only. Re-running stage `order` for the same planning job first deletes that job's `kind = merged_scc` clusters and its `cluster_edges`, which keeps the stage idempotent (specs FR1). Human-driven merges after that are overrides (§9) and never touch `clusters`.

**Implementation notes** (`agent2/worker/dependencies.py`, pinned down while building it):
- **Edge weight.** A `cluster_edges.weight` is the sum of the §3.5 weights of its underlying canonical pairs. A pair with a hub endpoint has no clustering weight (§3.4), so it contributes the structural term `log1p(counts in both directions)` alone.
- **Merged edges.** A merged SCC's internal edges are kept between the **original** units, flagged `merged` and sharing one `scc_id`. Every other unit edge is recomputed over the post-merge unit set, so edges into or out of the SCC point at the `merged_scc` unit. Contracting an SCC can't create a new cycle, so the unit SCCs left after merging are exactly the non-mergeable ones.
- **Members.** Re-pointing updates `cluster_members.cluster_id` from the originals to the merged unit. The originals keep their `clusters` row (and `size`) for audit but end up with no members. `graph_stats` gains `unit_edges`, `phase_edges`, `merged_sccs`, `facade_sccs` and `merged_into` (`{original unit id: merged unit id}`).
- **Re-run.** A `merged_scc` delete cascades to its `cluster_members`, so a re-run first re-inserts those members under their original unit (from stage `cluster`'s in-memory result), then deletes the merged clusters and this job's `cluster_edges`.
- **Flag count.** `scc_id` is unique across both levels within a planning job and assigned in a content-derived order (lowest canonical entity id), so it's stable across runs. `open_facade_flags` counts each `needs_facade` SCC once per level. A cross-phase unit cycle therefore counts twice (its unit SCC plus the phase SCC it causes), and each is acknowledged separately by `facade_ack`.
- **No report for `merged_scc` units.** Stage `report` runs before stage `order`, so a merged unit has no `cluster_reports` row or `label`. As with override merges (§9.2), its constituent (original) units' reports stand in, and part 2 (§8) rolls up their risk for the tie key.

## 8. Stage `order`, part 2 — ordering & plan emission

1. Topologically sort the phase condensation DAG with `networkx.lexicographical_topological_sort`, using the tie key `(foundation first, risk_level low→high, size, cluster id)`. Dependencies (`to` side) come first. This gives `phase_order`.
2. Within each phase, sort the unit condensation sub-DAG (only edges between units of that phase) with the same tie key. Members of one flagged SCC get consecutive `unit_order` values. This gives `unit_order`.
3. Build `test_contract` per unit (§10).
4. Insert `migration_plans` (`version = 1`, `status = draft`) and all `migration_plan_items`, then set `planning_jobs.status = done`.

**Implementation notes** (`agent2/worker/ordering.py`, pinned down while building it):
- **Tie key.** The last element is a cluster's lowest canonical entity id, not its uuid. Cluster uuids are random per run, so ordering on them would break determinism. A condensation node (an SCC) uses the combined key of its members: foundation if any member is, their highest risk, their total size and their lowest content key. Members inside one node are then sorted by their own key.
- **Risk.** A unit's risk is its `cluster_reports.risk_level`. A `merged_scc` unit has no report (§7), so it rolls up its original units' risk with `rollup_risk`. A phase's risk is its own report's, or, if that's missing, the rollup of its units' risk.
- **Unit sub-DAG.** Built per phase from the post-merge `unit` edges whose ends are both in that phase (excluding `merged` edges), then condensed again. A cross-phase SCC's members therefore split by phase, and they stay consecutive only within each phase.
- **Numbering.** `phase_order` and `unit_order` are 1-based.
- **Re-run.** Before inserting, the stage deletes any version 1 plan (and its items) that this planning job already has. That plan can only come from an earlier attempt of a job that isn't `done` yet, so it was never handed out (`plan_id` stays null until `done`).
- **Test contracts.** `run_ordering_stage` takes them as input, keyed by unit id (§10). A unit without one gets `{}`.

## 9. Review gate

### 9.1 State machine
```
draft ──submit──► in_review ──(all phases approved ∧ open_facade_flags = 0)──► approved
                     │
                     └──any phase rejected──► rejected
any non-terminal state ──project.latest_job_id moves past source_job_id──► stale
```
- The transition to `approved` is automatic, computed in the API after each `phase_approvals` write.
- `approved`, `rejected` and `stale` are terminal for that version. Changing anything means a new version.
- **Staleness:** there is no trigger across agents. The API checks on read and on every write, and when `projects.latest_job_id ≠ source_job_id` it marks the plan `stale` (from any state except `rejected`) and refuses approvals with `409`. A newly created plan for the new job is the way forward.

### 9.2 Overrides → new version
`POST .../overrides` records the override against version *n* and then synchronously (it's cheap: no LLM, no Leiden) re-runs **only** §7 steps 1, 2 and 4 plus §8 on the stored `clusters`/`cluster_edges`, applying every override in the chain. The result is version *n+1* (`draft`, `parent_plan_id` = *n*), and version *n* is left untouched.
- `merge` combines units into one plan item. `facade_ack` clears one `needs_facade` SCC from `open_facade_flags`. `reorder` pins an item's position, and only if the new position is DAG-consistent (`422` otherwise).
- **`split` is out of the MVP** (decided 2026-09-23). Moving members into a new plan item needs two things this design doesn't have. First, a key for an item with no `clusters` row: `migration_plan_items` is keyed `(plan_id, cluster_id)` with an FK to `clusters`, and every read endpoint and the `&unit=` URL key on `cluster_id`. Second, entity-level edges to re-derive the pieces' edges and contracts, and Agent 2 only stores unit-level `cluster_edges`. If it's revisited, the data would come from reading Agent 1's rows for just that unit's members, pinned to `source_job_id` (read-only, allowed by CLAUDE.md), and the keying still needs deciding. The API answers `422` for `kind = split`.
- Merged items have no cluster report of their own. The UI shows the constituent reports, which avoids an LLM call on the request path. This is the one exception to "the API never runs pipeline work": a pure-Python recomputation over at most a few thousand rows. If it ever gets slow, move it onto the queue.

## 10. Test contract (specs FR8)

The `migration_plan_items.test_contract` jsonb is built from Agent 1 facts and never from LLM output:

```json
{
  "public_interface": [
    { "entity_id": "uuid", "name": "calc_total", "kind": "function|method|hook",
      "class": "Cart | null", "params": [...], "file_path": "..." }
  ],
  "incoming_call_sites": [
    { "from_unit_id": "uuid", "from_entity_id": "uuid", "to_entity_id": "uuid", "call_count": 3 }
  ],
  "outgoing_dependencies": [ { "to_unit_id": "uuid", "call_count": 5, "hook_binding_count": 0 } ],
  "external_calls": [ "wp_remote_get", "update_post_meta" ],
  "hooks_bound": [ "init", "save_post" ],
  "side_effect_signals": [ { "entity_id": "uuid", "risk_level": "medium", "summary_excerpt": "..." } ],
  "unverified_member_ids": [ "uuid" ],
  "merged_cluster_ids": []
}
```
- `public_interface` is the members with at least one incoming edge from another unit, or a hook binding, with `params` taken from `raw_facts`.
- `external_calls` come from `raw_facts.calls` entries that didn't resolve to an edge.
- `unverified_member_ids` tells Agent 3 which members' summaries it shouldn't trust.

**Implementation notes** (`agent2/worker/test_contract.py`, pinned down while building it):
- **Inputs.** The contract is built over part 1's post-merge units (§7), so a `merged_scc` unit gets one contract and "another unit" means another post-merge unit. Entity ids are canonical (§3.2), and a node's summary is its representative's or else any duplicate's. Summaries are read from `knowledge_summaries` (entity level, `org_id`-scoped). The module never writes: `ordering.py` stores the contracts on the plan items.
- **`public_interface`.** Members that are the target of an edge from another unit, plus members at either end of a `hook_binding` edge: a bound callback (WordPress invokes it) or a bound hook (other code fires it). `kind` is `hook` for hook entities, `method` for a function with `raw_facts.class`, and otherwise the `entity_type`.
- **`incoming_call_sites`.** Only `calls` edges, one entry per (caller, callee) pair. Hook bindings from another unit show up in that unit's `outgoing_dependencies` instead.
- **`outgoing_dependencies`.** Gains `hook_binding_count` next to `call_count`, so a dependency that exists only through hook bindings doesn't look like `call_count: 0` with no explanation.
- **`external_calls`.** Reuses `report.py::external_call_names`, the same list the unit report sees (§6.1).
- **`hooks_bound`.** The names of hooks this unit's members bind to (the member is the `from` end). A hook entity that is only bound to doesn't count.
- **`side_effect_signals`.** Members whose entity summary is `medium` or `high` risk. `summary_excerpt` is the first 200 characters (`SUMMARY_EXCERPT_CHARS`).
- **`unverified_member_ids`.** Members whose summary is `unverified`. A member with no summary isn't listed, because there's nothing for Agent 3 to distrust.
- **Other.** `merged_cluster_ids` is always `[]` from the worker, since only override merges (§9.2) fill it. Every list is sorted, so the same facts give the same contract.

Agent 3 decides how to turn this into tests (recorded I/O, property-based, LLM-generated) and whether that needs a sandbox. None of that happens in Agent 2.

## 11. API surface (`/api/agent2/...`)

The conventions are the same as Agent 1 (`design.md` §4): Supabase JWT auth dependency, `org_id` resolved server-side and never accepted as a parameter, `404` for missing and other-org resources alike, `409` for a valid resource in the wrong state, responses rebuilt field by field (never passing a row through as-is).

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /projects/{project_id}/plans` | Create a planning job | Body optional `{source_job_id}` (defaults to `latest_job_id`). `202 {planning_job_id, status}`. `409` if the source isn't `done` or a planning job is already active. |
| `GET /projects/{project_id}/plans` | List plans (all versions) for the project | `{plans: [{id, version, status, source_job_id, is_stale, created_at}]}` |
| `GET /planning-jobs/{planning_job_id}` | Poll status | `{planning_job_id, project_id, status, progress_stage, error, plan_id \| null}` |
| `GET /plans/{plan_id}` | Plan overview | `{id, project_id, version, status, source_job_id, is_stale, open_facade_flags, approved_by, approved_at, parent_plan_id, parent_version, facade_cycles: [{level, scc_id, cluster_ids}], phases: [{phase_cluster_id, phase_order, label, summary_text, risk_level, grounding_status, approval, units: [{cluster_id, unit_order, label, size, risk_level, grounding_status, flags}]}]}` |
| `GET /plans/{plan_id}/units/{cluster_id}` | Unit detail | `{plan_id, cluster_id, phase_cluster_id, phase_order, unit_order, kind, label, size, risk_level, grounding_status, flags, report, constituent_reports, members: [{id, entity_type, name, file_path, grounding_status}], test_contract, edges: [{id, from_cluster_id, to_cluster_id, direction, call_count, hook_binding_count, weight, flag, scc_id}]}`. `404` for a unit that isn't an item of this plan. |
| `GET /plans/{plan_id}/graph?level=phase\|unit[&phase_id=]` | Graph for visualization | `{nodes: [{id, label, size, risk_level}], edges: [{from, to, weight, flag}]}`. `phase_id` limits a `unit`-level graph to one phase (drill-down). `422` on a missing or invalid `level`, or `phase_id` with `level=phase`. `404` for a `phase_id` that isn't a phase of this plan. |
| `POST /plans/{plan_id}/submit` | `draft → in_review` | `200 {plan_id, status: "in_review"}`. `409` from any other state or when stale. |
| `POST /plans/{plan_id}/overrides` | Record an override, get a new version | `{kind: merge\|facade_ack\|reorder, payload}` → `201 {plan_id, version, parent_plan_id, parent_version, override_id, status: "draft", open_facade_flags}`. Only on `draft`/`in_review` (`409` otherwise or when stale). `422` on an invalid or DAG-inconsistent override, and for `split` (out of the MVP, §9.2). `404` for a missing or other-org plan. |
| `POST /plans/{plan_id}/phases/{phase_cluster_id}/approval` | Approve or reject a phase | `{status: approved\|rejected, note}`. Only on `in_review`, `409` if `stale`. `404` for a phase not in this plan. `200 {plan_id, phase_cluster_id, approval: {status, note, reviewer_id, created_at}, status}`, where `status` is the plan's resulting status. |

**`POST /projects/{project_id}/plans` details** (pinned down while building it, `agent2/router.py`):
- The project and source-job lookups use the caller's RLS-scoped client. The `planning_jobs` insert uses the **service-role** client, because `planning_jobs` has no authenticated insert policy (§2, "writes are service-role only"). `org_id` is stamped from the server-resolved auth context, never from the request.
- A `source_job_id` that doesn't belong to this project (or org) gets `404`, the same as a missing one. A project with no `latest_job_id` and no body gets `409` ("finish an ingestion run first").
- The active-job check runs before the insert. If two requests race past it, the partial unique index `planning_jobs_one_active_per_project` raises a unique violation (`23505`), and that also maps to `409`.
- `params` holds exactly the five keys from §12: `seed`, `alpha`, `max_unit_size`, `hub_percentile`, `leiden_resolution`. `AGENT2_REPORT_CONCURRENCY` and the queue timeout are operational settings and aren't frozen.

**`GET /planning-jobs/{planning_job_id}` details:** `plan_id` is the **version 1** plan the worker emitted for this job (§8). It stays `null` until `status = done`. Override versions share the same `planning_job_id`, so the lookup is pinned to `version = 1` rather than picking up whichever version is newest. `graph_stats` and `params` are deliberately not returned. `project_id` is included so the status page can link back to its project after a refresh (added for `PlanningJobStatusPage`'s "Try again"; Agent 1's `GET /jobs/{job_id}` lacks it and its status page can't do this).

**`GET /projects/{project_id}/plans` details:** plans are ordered by `created_at` descending and include every version. The §9.1 stale check (`router.py::mark_stale_plans`, reused by every later plan read and write) runs first. It goes through the **service-role** client, because `migration_plans` has no authenticated update policy. It filters by `org_id` + `project_id` and moves `draft`/`in_review`/`approved` plans whose `source_job_id ≠ latest_job_id` to `stale`. `is_stale` is computed as `source_job_id ≠ latest_job_id` regardless of `status`, so a `rejected` plan on an older run still gets the "built on an older ingestion run" badge even though its status stays `rejected`.

**`GET /plans/{plan_id}` details** (`router.py::load_plan`/`build_plan_overview`):
- `load_plan` runs the §9.1 stale check before anything else and is the entry point for every later plan endpoint. `project_id` (for the page's link back) and `is_stale` (same meaning as in the list) were added to the shape while building it.
- Phases come from the plan's items, not from `clusters.parent_cluster_id`, because a `reorder`/`merge` override can move a unit (§2). Phases are sorted by `phase_order` and units by `unit_order`. Items, clusters and edges are paged (1000 rows), and the `cluster_reports` lookup is chunked `in_` filters (200 ids).
- **Unit fields.** A unit with its own report uses that report's `risk_level`/`grounding_status` and the cluster's `label`/`size`. A unit without one stands in its constituents' reports: the originals from `graph_stats.merged_into` for a `merged_scc` unit, or `test_contract.merged_cluster_ids` for an override merge. `risk_level` is their `rollup_risk`, `grounding_status` is `unverified` if any is and otherwise `verified` once all are present, and `label` is the constituents' labels sorted and joined with ` + `. A `merged_scc` unit uses its cluster's own label first. An override merge always joins, because its item keeps the lowest unit id, and that unit's label alone would hide the rest. `size` is the cluster's own for `merged_scc`, and the constituents' sum for an override merge.
- **`approved_by` / `approved_at`** are the plan row's columns, set by the approval auto-transition (below) and `null` until the plan is `approved`. `approved_by` is the reviewer's user id; the frontend shows "you" when it matches the signed-in user.
- **`parent_plan_id` / `parent_version`** name the version this one was made from by an override (§9.2), and are `null` for version 1. They drive `PlanPage`'s "Version N (from M)" note and its link back.
- **`facade_cycles`** are the still-open `needs_facade` SCCs (§7) at both levels, in `(level, scc_id)` order: what a `facade_ack` override can take (§9.2). Acknowledged cycles, and unit-level cycles an override merge folded into one plan item, are left out, the same rule as `open_facade_flags`. `cluster_ids` are the plan items (unit level) or phases (phase level) the cycle runs through. Added for the override controls on `PlanPage`.
- **`flags`**, in this order: `foundation` (the `kind = foundation` unit), `merged` (either kind of merge), `needs_facade` (the unit or one of its constituents is an end of a unit-level `needs_facade` edge). Phase-level `needs_facade` edges don't flag units, and they're counted in `open_facade_flags` anyway. A unit-level SCC stops flagging its units once a `facade_ack` in this version's override chain acknowledges it, or once an override merge folds all of its units into one plan item (§9.2).
- **Phases.** `label` is the phase cluster's, and `summary_text`/`grounding_status` are its report's (`null` without one). `risk_level` is the report's, or else the rollup of its units' (§8). `approval` is `{status, note, reviewer_id, created_at}` from `phase_approvals`, or `null`.

**`GET /plans/{plan_id}/units/{cluster_id}` details** (`router.py::build_unit_detail`):
- `load_plan` (with its stale check) runs first. `cluster_id` must be a `migration_plan_items` row of this plan, so a unit merged away into a `merged_scc` unit, a phase id, or another plan's unit all get `404` ("unit not found in this plan"). The unit fields (`label`, `size`, `risk_level`, `grounding_status`, `flags`) are computed exactly as in the overview, through the same `_unit_sources` stand-in.
- **Reports.** A unit with its own report gets `report: {summary_text, risk_level, risk_factors, grounding_status}` and `constituent_reports: []`. A merged unit (a `merged_scc` unit's originals, or an override merge's `merged_cluster_ids`) gets `report: null` and one `constituent_reports` entry per source, `{cluster_id, label, ...the same report fields}`, with nulls where a source has no report (§9.2: the UI shows these, never an LLM call).
- **Members** come from `cluster_members` of the post-merge clusters: the unit itself (a `merged_scc` unit holds its originals' re-pointed members, §7), or every `merged_cluster_ids` entry for an override merge. There's one row per canonical entity (§3.2), because duplicates are the same code. `grounding_status` is the representative's entity summary for the plan's `source_job_id`, else any duplicate's, else `null` (same rule as §10). Sorted by `(file_path, name, entity_type, id)`.
- **Edges** are the unit-level `cluster_edges` with either end in the unit, its sources or its member clusters. So a `merged_scc` unit also shows the `merged` edges kept between its originals. `direction` is `internal`, `outgoing` (this unit depends on the other end) or `incoming`, relative to that same set. Phase-level edges aren't included.
- `test_contract` is the plan item's jsonb as stored (§10).

**`GET /plans/{plan_id}/graph` details** (`router.py::build_plan_graph`):
- `load_plan` (with its stale check) runs first. Nodes are built from the plan overview, so they carry the same `label`, `size` and `risk_level` as the list view and come in plan order (`phase_order`, then `unit_order`). A phase node's `size` is the sum of its units' sizes, and its `risk_level` is the overview's (its report's, else the rollup of its units'). Every node is returned: the node cap is the frontend's job.
- **Phase level.** The edges are the stored phase-level `cluster_edges` (§7) between this plan's phases. They come from the worker's clustering. That stays accurate for override versions, because `merge` and `reorder` only work within one phase (§11 overrides details). If a future override moves units between phases, this needs recomputing.
- **Unit level.** The stored unit-level edges are remapped onto plan items. An edge ending on a `merged_scc` unit's original (from `graph_stats.merged_into`), or on a unit folded into an override merge (`merged_cluster_ids`), is redirected to the item that absorbed it. Edges that end up inside one node, such as a merged SCC's own `merged` edges, are dropped. Edges that collapse onto the same `(from, to)` pair are combined: `weight` is summed, and `flag` is the strongest of `needs_facade` > `merged` > `none`. With `phase_id`, only that phase's units are nodes, and edges leaving the phase are left out.
- `from` depends on `to`, as in `cluster_edges`. Edges are sorted by `(from, to)`.

**`POST /plans/{plan_id}/submit` details** (`router.py::submit_plan`):
- `load_plan` (with its stale check) runs first, and `404` covers a missing or other-org plan. A plan whose `source_job_id` isn't the project's latest gets `409` with a "stale" message before the status is looked at. That includes a `rejected` plan on an older run, which keeps its `rejected` status (§9.1). Any other status than `draft` gets `409` naming the status.
- The update goes through the **service-role** client (no authenticated update policy on `migration_plans`, §2). It filters on `org_id`, `id` **and** `status = draft`, so if a concurrent submit or stale check moves the plan first, the update matches no row and the request gets `409` ("no longer a draft") rather than overwriting the other change.

**`POST /plans/{plan_id}/phases/{phase_cluster_id}/approval` details** (`router.py::approve_phase`):
- Checks run in this order: `load_plan` (with its stale check, `404` if missing or other-org), then the phase must be a `phase_cluster_id` of one of this plan's items (`404` "phase not found in this plan" otherwise), then `409` if stale, then `409` unless `in_review`. Nothing is written when any of these fail. A body without a valid `status` gets `422`.
- **The reviewer is always the caller.** `reviewer_id` is `auth.user_id` from the JWT. The body has no reviewer field, and an extra `reviewer_id` in it is ignored. `org_id` comes from the auth context too.
- **Upsert, within the RLS we have.** `phase_approvals` has an authenticated **insert** policy (`org_id = current_org_id() and reviewer_id = auth.uid()`) but no update policy (§2). So a phase's first decision is inserted on the caller's RLS-scoped client, and the database itself enforces the org and reviewer. Changing an existing decision (while the plan is still `in_review`) updates that row's `status`, `note`, `reviewer_id` and `updated_at` through the service-role client, filtered by `id` + `org_id`. If two first decisions race, the loser's insert hits the `(plan_id, phase_cluster_id)` unique constraint (`23505`) and falls back to the update. The last decision wins.
- **Auto-transitions (§9.1)**, computed from every decision on the plan after the write: any `rejected` phase moves the plan to `rejected`. Every phase `approved` **and** `open_facade_flags = 0` moves it to `approved`, with `approved_by` = the caller and `approved_at` = now. Otherwise it stays `in_review`, even with every phase approved while a facade flag is open. The plan update goes through the service-role client and only matches an `in_review` plan, so a concurrent change (a stale check, or another reviewer's transition) wins. The response then reports the plan's actual status after re-reading it.
- `open_facade_flags` is the plan row's stored count. Acknowledging a facade is a `facade_ack` override, which makes a new version with a lower count (§9.2), so it's never decremented in place.

**`POST /plans/{plan_id}/overrides` details** (`router.py::create_override`, recomputation in `agent2/overrides.py`):
- Checks run in this order: `load_plan` (`404`), stale (`409`), status `draft`/`in_review` (`409` "overrides can only be made on a draft or in-review plan (status=X)"), then the override itself (`422` with a readable message). Nothing is written until the new version is known to be valid.
- **Rebuilt from version 1.** The version is reconstructed by replaying its override chain (walking `parent_plan_id` up to version 1, one `plan_overrides` row per step via `result_plan_id`), then the new override is applied. Inputs are version 1's plan items (post-merge units, their phases and test contracts), `clusters` (kind, label), `cluster_members` (for the §8 tie key), `cluster_reports` (risk) and `cluster_edges`. There's no Agent 1 read, no LLM and no Leiden. Replaying keeps plan item ids deterministic, so an id seen on version *n* names the same item on *n+1*. With no override at all, the rebuild matches the worker's version 1 exactly (order, contracts and flag count; checked by `tests/test_agent2_pipeline_fixture.py`).
- **Payloads.**
  - `merge` `{cluster_ids: [≥2 plan item ids]}`: the items must share a phase. The merged item keeps the lowest unit id, with every underlying post-merge unit in `test_contract.merged_cluster_ids`. Contracting units can close a cycle §7 never flagged (a → x → b with a and b merged), so a merge is refused (`422`) when any cycle in the new item graph contains an edge that isn't part of a `needs_facade` SCC.
  - `facade_ack` `{level: unit|phase, scc_id, note}`: the note is required. The SCC must exist as `needs_facade` edges, and it can't already be acknowledged in the chain.
  - `reorder` `{cluster_id, unit_order}`: pins the item to that 1-based position in its phase. Pins apply in chain order after §8's own sort. The result is refused (`422`, "X depends on Y, so Y must come first in the phase") if any dependency inside the phase then comes after its dependent. Edges inside one cycle don't count, since a cycle has no consistent order. A later merge drops earlier pins on the merged items, and the merged item takes its §8 position.
  - `split` answers `422` ("split overrides aren't part of the MVP"). It was dropped, not deferred (§9.2).
- **Recomputed version.** Phases keep version 1's `phase_order` (none of the supported kinds moves a unit between phases). `unit_order` is §8 step 2 over the item graph, with each item's tie key combined from its units' keys, then the pins. Each item's `test_contract` is its units' version 1 contracts with unit ids remapped to plan items. Call sites and dependencies between units of one item drop out, the other lists are sorted unions, and `public_interface` stays the union (over-inclusion is the safe side for Agent 3). An item whose references didn't move keeps its version 1 contract as is. `open_facade_flags` counts the stored `needs_facade` SCCs, minus acknowledged ones, minus unit-level ones folded into a single item.
- **Writes.** The new `migration_plans` row (`draft`, `parent_plan_id` = *n*, version = the planning job's highest + 1, retried on a `23505` race) and its items go through the service-role client. The `plan_overrides` row goes through the caller's RLS-scoped client, so the insert policy enforces `org_id` and `created_by = auth.uid()`. It's written last with `result_plan_id` set, so no update policy is needed. If it fails, the new version and its items are deleted, because a version without its override can't be replayed.
- Version *n* keeps its status and its approvals. The new version starts as a `draft` with no approvals.

## 12. Configuration

All values are frozen into `planning_jobs.params` at creation, so later env changes never alter an existing job's results.

| Env var | Default | Meaning |
|---|---|---|
| `AGENT2_SEED` | 42 | Leiden seed |
| `AGENT2_EDGE_ALPHA` | 0.5 | Similarity weight α (§3.5) |
| `AGENT2_MAX_UNIT_SIZE` | 25 | Unit cap, in canonical entities |
| `AGENT2_HUB_PERCENTILE` | 99 | Degree percentile for hubs |
| `AGENT2_LEIDEN_RESOLUTION` | 1.0 | Base resolution |
| `AGENT2_REPORT_CONCURRENCY` | 10 | Unit-report fan-out cap |
| `AGENT2_QUEUE_VISIBILITY_TIMEOUT_SECONDS` | 1800 | Same semantics as Agent 1's |

## 13. Dependencies on Agent 1 & known gaps (not fixed by Agent 2)

Agent 2 is read-only on Agent 1 tables. These are gaps it works around. Fixing them belongs in Agent 1's `tasks.md`:

- **Duplicate entities across extractors.** Handled by §3.2 canonicalization. The real fix is reconciliation in Agent 1's persist step.
- **No class↔method or inheritance edges.** Handled by §3.3 containment from `raw_facts`. Inheritance isn't used for ordering yet. A future Agent 1 `extends`/`implements` edge type would add dependency edges (subclass depends on parent).
- **`include` edges have no target entity.** Ignored. File-level dependencies are invisible to ordering.
- **Method calls resolved by bare name only when unique** (`php_extractor.py`). Ambiguous `$this->foo()` calls are missing edges, so the graph is sparser than reality.
- **Agent 1 job never reaching `done`** (e.g. the Azure endpoint issue in `tasks.md` Phase 4). Agent 2 cannot run until it does, by design (specs §2 gate).
- ~~**Unresolved calls aren't stored.**~~ Fixed 2026-09-24 in Agent 1: `php_extractor.py` now writes `raw_facts.calls` (distinct, sorted) for functions and methods, so `test_contract.external_calls` (§10) and the unit report's external dependency names (§6.1) fill in for jobs ingested after the fix. Older jobs still have no `calls` and give empty lists.
- **Entities persisted twice in one job.** Live job `f01c503b` (project `hospital_test`) has 340 entities that are exactly 170 duplicated pairs, with every `calls` row doubled. §3.2 canonicalization collapses it cleanly (170 nodes, `canonicalized = 170`), so the plan is unaffected. The likely cause is a redelivered or retried ingestion message re-running persist without clearing first.

## 14. Worker wiring

`agent2/worker/run.py::process_planning_job` runs the four stages in order. Each stage function takes `(client, planning_job, state)`, where `PlanningState` carries the in-memory hand-offs: `graph` (§3) → `clusters` (§4) → `risk_by_cluster` from the reports (§6) → `plan_id` (§8). Nothing is re-read from the DB between stages. Stage `order` runs `run_dependencies_stage` (§7), then `run_test_contract_stage` over the post-merge units (§10), then `run_ordering_stage` (§8). The Azure client and the embedder are looked up on `run.py` at call time, so tests can swap them.

**Re-run safety.** A redelivered message for a job that isn't `done` re-runs every stage. `migration_plan_items` references `clusters` without cascading (plans are immutable), so an earlier attempt's version 1 plan would block stage `cluster`'s delete. `stage_cluster` therefore deletes that unfinished plan first (`ordering.delete_unfinished_plan`). The plan was never handed out, because `plan_id` stays null until `done`.

## 15. Pipeline runs & tuning findings

**Fixture run (automated, `tests/test_agent2_pipeline_fixture.py`).** `tests/fixtures/agent2_cycle_repo/` has three files whose entry functions form a call cycle (`billing_total → tax_rate → shipping_cost → billing_total`), dense intra-file calls, and two hook callbacks. It runs through Agent 1's real extractors and persist step into a fake client, then through the real planner worker with only the LLM and embedder faked. With the §12 defaults (seed 42):
- Each file becomes its own unit, so the cycle is a 3-unit SCC.
- Leiden puts billing in one phase and tax + shipping in another. The SCC spans phases, so it's `needs_facade` at unit level (one `scc_id` over the three edges) and at phase level. `open_facade_flags = 2`, the §7 double count for one real cycle.
- The order puts callees first (`billing_format`'s unit before its caller `cycle_render_invoice`). Test contracts carry cross-unit callees as `public_interface`, hook callbacks with `hooks_bound`, and outgoing dependencies.
- A re-run of the same job replaces its plan cleanly. Two planning jobs over the same snapshot give the same layout.
- Determinism is **per snapshot**. A re-ingestion mints new entity ids, and ids are the last tie-break (§8) and Leiden's vertex order (§4), so a fresh ingestion of the same code can order two symmetric phases differently.

**Live data (read-only, in-memory dry run, 2026-09-23).** No WordPress repo has been ingested to `done` yet. The only live `done` jobs with entities are for `hospital_test`, a **Laravel** app (170 entities: 123 functions and 47 classes over 48 files). The pure stage functions were run over its latest job with no LLM calls and no writes:
- **Very sparse call graph.** There are 9 resolved `calls` rows for 170 entities. Laravel code is mostly `$this->…`, facades and container calls, which Agent 1 can't resolve (§13). Clustering is driven almost entirely by class containment (122 of 128 weighted edges), so units ≈ classes (37 community units of up to 12, plus 9 `isolated_file` units). No hubs, no foundation, no cycles.
- **Phases degenerate to one unit each (46 phases).** Leiden over an edgeless unit graph returns singletons, which would mean 46 separate approvals. This is the main tuning issue. **Decided (2026-09-24): the §4 directory fallback.** Re-running the dry run over job `9a043808` gives 12 phases instead of 46 (e.g. `app/Http/Controllers` 15 units, `app/Http/Controllers/Auth` 8, `app/Models` 11, `database/migrations` 2).
- **α (`AGENT2_EDGE_ALPHA`) had no effect.** The similarity term only reweights pairs that already have a structural edge (§3.5), so it can't connect a sparse graph. Results were identical at α = 0 and 0.5. If similarity should pull related-but-uncalled code together, it needs top-k similarity edges, which is a design change.
- **`max_unit_size` = 25 was never binding** (largest unit: 12). A cap of 10 split one unit and changed nothing else.
- **§9 defaults: no change yet.** A Laravel repo isn't representative of the WordPress target, and the real bottleneck is edge sparsity, not a parameter. Revisit after the first WordPress ingestion reaches `done` (`tasks_agent2.md` Phase 4).
