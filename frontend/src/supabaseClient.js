import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

// Missing config must not throw at module load — that happens before React
// renders anything, so it would crash straight to a blank white page instead
// of a visible error. AuthProvider checks this and renders a setup message.
export const isSupabaseConfigured = Boolean(url && anonKey)

// Anon key only — safe for the browser. RLS + the backend's JWT validation
// (design.md §1a) are what actually enforce access, not this key.
export const supabase = isSupabaseConfigured ? createClient(url, anonKey) : null
