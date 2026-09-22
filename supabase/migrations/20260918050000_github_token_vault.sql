-- GitHub token lifecycle — design.md §2 `github_token_ref`.
-- Private-repo access tokens are stored in Supabase Vault (pgsodium-encrypted),
-- scoped to a single ingestion job, and deleted on job completion — never
-- persisted long-term or retrievable afterward.

create extension if not exists supabase_vault;
