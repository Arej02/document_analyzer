"""Cited Q&A over one ingestion job ("ask the codebase") — ported from Galvanise's
understanding/context.py and understanding/answer.py onto Agent 1's Supabase tables.

`context.retrieve` deterministically assembles evidence (semantic search over
`source_chunks` + symbol search over `extracted_entities`, fused, then bounded graph
expansion over `entity_edges`); `answer.generate_answer` makes one Azure OpenAI call
that must cite only the evidence and relationship IDs it was given. Read-only on
Agent 1's tables, and it runs on the caller's RLS-scoped client. Reviewers use it
during plan approval (design.md §4, design_agent2.md).
"""
