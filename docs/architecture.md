# Architecture overview

A one-page map of how the whole system fits together, drawn as Mermaid diagrams. It's an overview only: the authoritative detail lives in `design.md` (Agent 1, including §5a for Graphify and source evidence), `design_agent2.md`, `design_agent3.md` and `design_report.md` (Assessment Report). If this page and a design doc disagree, the design doc wins; fix this page.

## 1. System overview

Three independent agents, each with its own pgmq queue and long-lived worker process, plus the read-only Assessment Report consumer with its own queue and worker. None of them call each other; they only read and write shared Supabase tables. FastAPI only enqueues jobs and reads results; nothing long-running happens inside a request.

```mermaid
flowchart LR
    UI["React frontend<br/>(frontend/)"]

    subgraph API["FastAPI (backend/document_analyzer/app.py)"]
        R1["jobs.py<br/>/api/agent1/jobs, /ask, /coverage"]
        RP["projects_router.py"]
        RC["config_router.py"]
        R2["agent2/router.py<br/>/api/agent2/..."]
        R3["agent3/router.py<br/>/api/agent3/..."]
        RR["report/router.py<br/>/api/reports/..."]
        RPUB["report/public_router.py<br/>/api/public/reports/{token}<br/>(no auth)"]
    end

    subgraph SB["Supabase"]
        PG[("Postgres + pgvector<br/>RLS by org_id")]
        Q1[["pgmq: agent1_ingestion_jobs"]]
        Q2[["pgmq: agent2_planning_jobs"]]
        Q3[["pgmq: agent3_execution_jobs"]]
        QR[["pgmq: report_generation_jobs"]]
        ST[("Storage: agent1-uploads,<br/>agent1-snapshots, agent3-output,<br/>reports")]
        AUTH["Auth + Vault"]
    end

    subgraph Workers["Worker processes"]
        W1["Agent 1: Understand<br/>document-analyzer-worker"]
        W2["Agent 2: Plan<br/>document-analyzer-planner"]
        W3["Agent 3: Execute<br/>document-analyzer-executor"]
        WR["Assessment Report<br/>document-analyzer-reporter"]
    end

    subgraph Colima["Container engine: Colima (default) or Docker (CONTAINER_ENGINE)"]
        GF["Graphify container<br/>(document-analyzer-graphify)"]
        SX["Agent 3 sandbox<br/>(galvanise-agent3-runner, gVisor in prod)"]
        PDF["Report PDF renderer<br/>(headless Chromium, no network)"]
    end

    AOAI["Azure OpenAI<br/>(summaries, cluster reports, Q&A)"]
    EMB["Local all-MiniLM-L6-v2<br/>(pinned embeddings)"]

    UI -->|"Supabase JWT"| API
    UI --> AUTH
    API --> PG
    R1 -->|enqueue| Q1
    R2 -->|enqueue| Q2
    R3 -->|enqueue| Q3
    RR -->|enqueue| QR
    RPUB -->|"token hash lookup,<br/>stored artifact only"| PG
    RPUB --> ST

    Q1 --> W1
    Q2 --> W2
    Q3 --> W3
    QR --> WR

    W1 -->|"entities, edges, summaries,<br/>source_chunks"| PG
    W1 --> ST
    W2 -->|"reads Agent 1 tables (read-only)<br/>writes clusters, plans"| PG
    W3 -->|"reads approved plans (read-only)<br/>writes unit_executions"| PG
    W3 --> ST
    WR -->|"reads Agents 1-3 + cost tables (read-only)<br/>writes reports, share links, internal estimates"| PG
    WR --> ST

    W1 -. "container_engine.py" .-> GF
    W3 -. "sandbox.py" .-> SX
    WR -. "container_engine.py" .-> PDF
    W1 --> AOAI
    W2 --> AOAI
    WR -->|"executive summary only"| AOAI
    R1 -->|"ask/answer.py"| AOAI
    W1 --> EMB
    R1 --> EMB
```

## 2. Agent 1 ingestion pipeline

`worker/run.py::process_job`, for one dequeued job. The `progress_stage` values are `extracting`, `summarizing` and `grounding`. A grounding failure flags the summary `unverified`; it never blocks the job.

```mermaid
flowchart TD
    A["Dequeue from agent1_ingestion_jobs<br/>mark running"] --> B["fetch.py<br/>clone GitHub / unpack upload<br/>record source_commit_sha"]
    B --> C["boilerplate.py<br/>strip vendor / generated files -> filtered_files"]
    C --> D["snapshot.py<br/>post-filter tarball -> agent1-snapshots<br/>(read later by Agent 3)"]
    D --> E{"Extractors<br/>(per file)"}
    E -->|".php"| P["PhpExtractor (nikic)<br/>+ WordPressExtractor<br/>+ LaravelExtractor"]
    E -->|"every other code language"| G["GraphifyExtractor<br/>(isolated container, section 3)"]
    P --> F["evidence.py<br/>chunk -> secret-redact -> embed<br/>+ per-entity source_excerpt"]
    G --> F
    F --> H["persist.py<br/>extracted_entities, entity_edges,<br/>source_chunks, source_chunk_entities,<br/>extraction_coverage"]
    H --> I["diff.py<br/>changed / unchanged / removed counts"]
    I --> J["summarize.py + ground.py (fan-out)<br/>facts + source_excerpt -> Azure OpenAI<br/>summary_cache hit = no LLM call"]
    J --> K["fold module -> subsystem -> repo<br/>each level grounded"]
    K --> L["mark done"]

    classDef extracting fill:#e8f1ff,stroke:#4a78c2
    classDef summarizing fill:#fff4e0,stroke:#c28a2a
    classDef grounding fill:#e9f7ec,stroke:#3c8f53
    class B,C,D,E,P,G,F,H,I extracting
    class J summarizing
    class K grounding
```

Colours: blue is the `extracting` stage, orange is `summarizing`, green is `grounding`.

Live progress is an event stream: each agent's worker appends its own rows to the shared `workflow_events` table, keyed by `(workflow_kind, workflow_id)`, and the frontend's run view polls them per job. No agent reads or writes another's events (`design.md` §2).

## 3. Graphify extraction (isolated, parse-only)

Graphify's tree-sitter parsers read hostile input, so they run in a disposable container rather than in the worker. Nothing from the repository is ever executed. Mode is set with `AGENT1_GRAPHIFY`:

- `auto` (default): skip and record `unavailable` if the container engine or the image is missing.
- `required`: fail the job instead.
- `off`: never run.

Once the container has actually run, any failure (timeout, OOM, invalid output) fails the job.

```mermaid
sequenceDiagram
    autonumber
    participant W as Agent 1 worker<br/>(graphify/extractor.py)
    participant R as graphify/runner.py
    participant CE as shared/container_engine.py
    participant C as Graphify container<br/>(graphify_worker/)
    participant V as graphify/validate.py

    W->>W: select_files(): non-.php files whose<br/>extension has a working parser
    W->>R: run_extraction(selected files)
    R->>R: copy files (no symlinks) into<br/>CONTAINER_WORKDIR/graphify/<job>
    R->>CE: docker run (configured engine)
    CE->>CE: Colima: pinned socket, name check<br/>docker: ambient engine must answer
    CE->>C: --network none, read-only root,<br/>cap-drop ALL, non-root, limits, timeout
    C-->>R: nodes.jsonl, edges.jsonl,<br/>coverage.json, manifest.json
    R->>CE: remove container, delete job dir (always)
    R->>V: validate_output()
    V-->>W: sizes, sha256, schema, unique IDs,<br/>edge endpoints checked
    W->>W: map_output(): function/method/class -> entities<br/>calls / inherits / include edges + provenance<br/>unmapped relations dropped and counted
```

## 4. Cited Q&A: "Ask the codebase"

`POST /api/agent1/jobs/{job_id}/ask`. Retrieval is deterministic and runs on the caller's RLS-scoped client. The LLM only sees repository text inside random per-request fences, and may cite only the `E#` / `R#` IDs it was given.

```mermaid
sequenceDiagram
    autonumber
    actor U as User (AskCodebase panel)
    participant J as jobs.py
    participant X as ask/context.py
    participant DB as Supabase (RLS)
    participant A as ask/answer.py
    participant O as Azure OpenAI

    U->>J: POST /ask {question}
    J->>X: retrieve(job_id, question)
    X->>DB: match_source_chunks(embedding of question)
    X->>DB: symbol / path search over entity names
    X->>X: reciprocal-rank fusion
    X->>DB: 2-hop expansion over entity_edges<br/>(hub, edge and node caps)
    X->>X: merge overlapping excerpts,<br/>fit a 12k-token budget (1/4 for relationships)
    X-->>J: Context (E# evidence, R# relationships, limitations)
    alt no stored evidence for this job
        J-->>U: insufficient_evidence (re-ingest)
    else
        J->>A: generate_answer(context)
        A->>O: fenced prompt, fixed JSON shape
        O-->>A: answer + claims with citations
        A->>A: validate_citations()
        alt invalid or missing citation, malformed output
            A-->>U: 502 AI_INVALID_CITATION / AI_BAD_RESPONSE
        else
            A-->>U: answer + citations + limitations
        end
    end
```

## 5. Agent 2 (Plan) to Agent 3 (Execute)

The user starts planning. Only an approved, non-stale plan reaches Agent 3. Agent 3's stages are stubs until `tasks_agent3.md` Phases 2 to 4 land.

```mermaid
flowchart LR
    subgraph A1["Agent 1 tables (read-only to 2 and 3)"]
        KB[("extracted_entities<br/>entity_edges<br/>knowledge_summaries")]
        SNAP[("agent1-snapshots tarball")]
    end

    subgraph P["Agent 2 planner worker"]
        P1["graph.py<br/>canonical graph"] --> P2["cluster.py<br/>seeded Leiden"]
        P2 --> P3["report.py<br/>cluster reports (Azure OpenAI)"]
        P3 --> P4["dependencies.py + ordering.py<br/>SCCs, topological phases<br/>test_contract.py"]
    end

    subgraph REV["Human review (frontend, React Flow)"]
        R1["per-phase approval<br/>phase_approvals"]
        R2["overrides -> new plan version<br/>(plans are immutable)"]
    end

    subgraph E["Agent 3 executor worker"]
        E1["prepare"] --> E2["units<br/>generate code + tests"]
        E2 --> E3["assemble"] --> E4["final_test"]
    end

    SBX["Sandbox (Colima or Docker, gVisor in prod)<br/>no network, read-only root, limits"]
    OUT[("unit_executions<br/>agent3-output bucket")]

    USER(["User: POST /api/agent2/projects/{id}/plans"]) --> P1
    KB --> P1
    P4 --> PLAN[("migration_plans<br/>migration_plan_items")]
    PLAN --> R1
    R1 --> R2 --> PLAN
    PLAN -->|"status = approved, not stale"| E1
    SNAP --> E1
    E2 -. "runs generated Python only" .-> SBX
    E4 -.-> SBX
    E4 --> OUT
```

Agents 2 and 3 don't read `source_chunks`, edge `provenance`, or `inherits` edges yet. They do receive entities and edges for non-PHP repositories now that Graphify runs.

## 6. Data model (key tables)

Every table carries `org_id` with RLS. Worker writes use the service role, stamp `org_id` from the job row, and filter updates by `id` and `org_id`. The diagram omits `org_id`, `org_members`, `embedding_config`, `plan_overrides`, the report job's own columns, and the LLM cost tables (§8).

```mermaid
erDiagram
    projects ||--o{ ingestion_jobs : has
    ingestion_jobs ||--o{ extracted_entities : produces
    ingestion_jobs ||--o{ entity_edges : produces
    extracted_entities ||--o{ entity_edges : "from / to"
    extracted_entities ||--o{ knowledge_summaries : summarized_by
    knowledge_summaries ||--o{ summary_cache : cached_as
    ingestion_jobs ||--o{ source_chunks : evidence
    source_chunks ||--o{ source_chunk_entities : links
    extracted_entities ||--o{ source_chunk_entities : links
    ingestion_jobs ||--o{ secret_findings : flags
    ingestion_jobs ||--o{ filtered_files : skips

    projects ||--o{ planning_jobs : has
    ingestion_jobs ||--o{ planning_jobs : source_job
    planning_jobs ||--o{ clusters : creates
    clusters ||--o{ cluster_members : contains
    extracted_entities ||--o{ cluster_members : member
    clusters ||--o| cluster_reports : described_by
    planning_jobs ||--o{ cluster_edges : creates
    planning_jobs ||--o{ migration_plans : yields
    migration_plans ||--o{ migration_plan_items : orders
    migration_plans ||--o{ phase_approvals : approved_by
    migration_plans ||--o{ migration_plans : parent_plan

    migration_plans ||--o{ execution_jobs : executed_by
    execution_jobs ||--o{ unit_executions : runs
    clusters ||--o{ unit_executions : unit

    projects ||--o{ report_generation_jobs : has
    report_generation_jobs ||--o| assessment_reports : yields
    assessment_reports ||--o{ report_share_links : shared_by
    assessment_reports ||--o| report_internal_estimates : "internal only"

    ingestion_jobs {
        uuid id PK
        uuid project_id
        text status
        text progress_stage
        text source_commit_sha
        text source_snapshot_path
        jsonb extraction_coverage
        jsonb discovery_stats
    }
    extracted_entities {
        uuid id PK
        uuid job_id FK
        enum entity_type
        text file_path
        jsonb raw_facts
        text content_hash
    }
    entity_edges {
        uuid job_id FK
        uuid from_entity_id FK
        uuid to_entity_id FK
        enum edge_type "calls, hook_binding, global, include, route_binding, inherits"
        jsonb provenance
    }
    source_chunks {
        uuid id PK
        uuid job_id FK
        text file_path
        int line_start
        int line_end
        vector embedding
    }
    migration_plans {
        uuid id PK
        uuid source_job_id FK
        uuid parent_plan_id FK
        enum status "draft, in_review, approved, rejected, stale"
    }
    assessment_reports {
        uuid id PK
        uuid source_job_id "pinned Agent 1 job"
        uuid plan_id "full tier only"
        enum tier "xray, full"
        jsonb report_json "customer-safe"
        text html_path
        text pdf_path
    }
    report_share_links {
        uuid id PK
        uuid report_id FK
        text token_hash "sha256 only, never the token"
        timestamptz expires_at
        timestamptz revoked_at
    }
    report_internal_estimates {
        uuid report_id PK
        numeric ai_cost_p50 "no tenant policy"
        numeric ai_cost_p90
    }
```

## 7. Trust boundaries

- **No ingested code runs.** Agents 1 and 2 only parse the repository statically. Graphify's container parses a copy of the files; it doesn't execute anything. Agent 3 runs only the Python it generated, and only inside its sandbox. The worker never imports generated code in-process.
- **Repository text is untrusted in every prompt.** It is fenced or marked untrusted for summaries, grounding, cluster reports and Q&A. Q&A answers must cite supplied evidence, and a bad citation is rejected rather than shown.
- **Secrets are redacted.** Source chunks and excerpts go through `secret_scan.py::redact_text` before they're stored or sent anywhere. Findings go to `secret_findings`.
- **Code stays local for embeddings.** Embeddings use the pinned local MiniLM model. The only code sent to Azure OpenAI is an entity's own excerpt (in its summary call) and the retrieved Q&A context.
- **Containers run on one configured engine.** `CONTAINER_ENGINE=colima` (default) or `docker` (default on Windows); there is no fallback between them. Every container call goes through `shared/container_engine.py`. Run `uv run document-analyzer-doctor` to verify the setup.
- **The public share route is the only unauthenticated data path.** `GET /api/public/reports/{token}` (and `/pdf`) look up `sha256(token)` and return only the stored HTML or PDF. They accept no other parameter, read no other table, and give the same 404 for an unknown, expired, revoked or deleted link. Responses carry `no-store`, `noindex`, `no-referrer` and a strict CSP. See §9.
- **The PDF container renders text we didn't write.** Entity names, paths and summaries are attacker-controlled. The renderer runs with no network, a read-only root, no capabilities, resource limits, a timeout and JavaScript off, with one read-only HTML file in and one PDF out. It runs through `shared/container_engine.py`.
- **A report never contains source or internal cost.** Collectors select explicit columns only, internal estimates live in a table with no tenant policy, and a fail-closed leak gate scans the JSON, the HTML and the PDF text.

## 8. LLM cost accounting (added 2026-10-01)

Every Azure OpenAI call from every agent goes through one wrapper and is recorded in internal-only tables. Full design: `design.md` §7 (agent specifics in `design_agent2.md` §17 and `design_agent3.md` §15).

```mermaid
flowchart LR
    A1[Agent 1 worker] --> W
    A2[Agent 2 worker] --> W
    A3[Agent 3 worker] --> W
    QA[Q&A endpoint, API process] --> W
    W["MeteredLLMClient<br/>(shared/llm.py)<br/>latency, usage, cost"] --> AOAI[Azure OpenAI]
    W --> R["LLMCostRecorder<br/>thread-safe buffer"]
    R -->|"best effort, never raises"| T[("llm_invocations<br/>llm_job_costs<br/>RLS on, no tenant policies")]
    T --> E["Cost endpoints<br/>operator only, 404 otherwise"]
    E --> UI[Run views: cost chip + by-stage table]
```

- **Trust boundary:** the cost tables are service-role only. Org members can't read them through PostgREST, and cost never appears on job rows, `workflow_events` or any org-facing response. Operators are an explicit allowlist (`COST_OPERATOR_USER_IDS`).
- **Never blocks work:** a failed insert or flush is logged and dropped. It never fails or delays a job or request.
- **Estimate, not invoice:** `cost_usd` uses list prices from `shared/llm_pricing.toml` and is stamped with a `pricing_version`. Reconcile with the Azure invoice for real margin.
- **Single choke point:** a guard test fails the build if anything other than `shared/llm.py` imports `openai` or builds an `AzureOpenAI` client.

## 9. Assessment Report (added 2026-10-01)

A shareable, polished report generated from one repo's Agent 1 run and, optionally, its Agent 2 plan, including a time and price estimate. It is a **read-only consumer** of Agents 1 to 3 and the cost tables, not a fourth agent: a user triggers it, and nothing calls it. Full design: `design_report.md`, spec: `specs_report.md`.

### Generation flow

```mermaid
flowchart LR
    USER(["User: POST /api/projects/{id}/reports {tier}"]) --> API["report/router.py<br/>gate: Agent 1 done,<br/>plan pinned to same job (full)"]
    API --> J[("report_generation_jobs")]
    API --> Q[["pgmq: report_generation_jobs"]]
    Q --> W

    subgraph W["Reporter worker (document-analyzer-reporter)"]
        C1["collect<br/>explicit-column collectors"] --> C2["estimate<br/>seeded Monte Carlo, p50/p90"]
        C2 --> C3["summarize<br/>executive summary only<br/>grounded + numeric check"]
        C3 --> C4["render_html<br/>Jinja2 + content gate"]
        C4 --> C5["render_pdf<br/>rendered gate"]
        C5 --> C6["publish"]
    end

    A[("Agent 1 / 2 / 3 tables<br/>read-only")] --> C1
    CAL[("llm_cost_by_execution_unit,<br/>unit_executions<br/>pooled aggregates only")] --> C2
    C3 -.-> AOAI["Azure OpenAI<br/>via shared/llm.py"]
    C5 -.-> PDF["PDF container<br/>no network, JS off"]
    C6 --> R[("assessment_reports<br/>report_json: customer-safe")]
    C6 --> B[("reports bucket<br/>{org}/{report}/report.html, .pdf")]
    C2 --> I[("report_internal_estimates<br/>no tenant policy")]
    I --> OP["GET .../internal-estimate<br/>operator only, 404 otherwise"]
```

- **Immutable snapshot.** A report is pinned to one Agent 1 job (and one plan id and version for a Full Assessment). Regenerating makes a new row.
- **Out of date is computed on read** (`projects.latest_job_id` moved on, or the pinned plan went `stale`/`rejected`). The report never writes to Agent 1 or 2 tables and never calls `mark_stale_plans`.
- **One renderer.** The stored HTML is what the app shows (in a sandboxed iframe), what a share link serves, and what the PDF is printed from.
- **PDF failure is not report failure.** The report stays `done` with `pdf_status = failed` and a retry.
- **Customer vs operator.** The customer artifact never has internal cost. The AI cost range and price basis sit in `report_internal_estimates`, readable only by operators.

### Share flow

```mermaid
sequenceDiagram
    participant U as Org member
    participant API as report/router.py (auth)
    participant DB as Postgres
    participant V as Anyone with the link
    participant PUB as public_router.py (no auth)

    U->>API: POST /api/reports/{id}/share-links {expires_in_days}
    API->>API: token = 32 random bytes
    API->>DB: store sha256(token), expires_at
    API-->>U: full URL, shown once
    V->>PUB: GET /api/public/reports/{token}
    PUB->>DB: look up sha256(token), constant-time compare
    alt valid, not expired, not revoked, report exists
        PUB->>DB: access_count + 1, last_accessed_at
        PUB-->>V: stored HTML only (CSP, no-store, noindex)
    else anything else
        PUB-->>V: the same 404
    end
    U->>API: POST /api/share-links/{id}/revoke
    API->>DB: revoked_at, revoked_by (effective on the next request)
```
