-- Job queue — design.md §3 (decided: pgmq, Postgres-native, no second infra dependency).

create extension if not exists pgmq;

-- One queue for Agent 1's ingestion jobs. Agent 2/3 (not yet built) will get
-- their own queues when they exist — no agent shares another's queue, per the
-- "no direct agent-to-agent calls" rule in CLAUDE.md.
select pgmq.create('agent1_ingestion_jobs');
