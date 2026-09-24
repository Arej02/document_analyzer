-- Agent 2 (Plan) job queue — design_agent2.md §1, §2 "Queue".
-- Its own queue, never shared with agent1_ingestion_jobs (CLAUDE.md: each agent
-- has its own queue and worker). pgmq extension already enabled by
-- 20260918030000_pgmq_job_queue.sql.

select pgmq.create('agent2_planning_jobs');
