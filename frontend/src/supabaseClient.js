import { createClient } from '@supabase/supabase-js'

// The Supabase URL + anon key come from the backend's GET /api/config
// (design.md §4), so the only Supabase settings live in the backend's .env.
// VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY, when both are set, override it
// (e.g. a static deploy with no backend on the same origin).
//
// Anon key only — safe for the browser. RLS + the backend's JWT validation
// (design.md §1a) are what actually enforce access, not this key.
//
// Never throws: a failure resolves to { supabase: null, error } so
// AuthProvider can render a setup message instead of a blank page.
let loading = null

async function fetchConfig() {
  const envUrl = import.meta.env.VITE_SUPABASE_URL
  const envKey = import.meta.env.VITE_SUPABASE_ANON_KEY
  if (envUrl && envKey) return { url: envUrl, anonKey: envKey }

  let response
  try {
    response = await fetch('/api/config')
  } catch {
    throw new Error("Can't reach the backend. Start it with `uv run uvicorn document_analyzer.app:app --reload`.")
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(body.detail || `GET /api/config failed (${response.status}). Is the backend running?`)
  }
  return { url: body.supabase_url, anonKey: body.supabase_anon_key }
}

export function loadSupabase() {
  if (!loading) {
    loading = fetchConfig()
      .then(({ url, anonKey }) => ({ supabase: createClient(url, anonKey), error: null }))
      .catch((error) => {
        loading = null // let a later mount retry
        return { supabase: null, error: error.message }
      })
  }
  return loading
}
