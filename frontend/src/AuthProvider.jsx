import { createContext, useContext, useEffect, useState } from 'react'
import { loadSupabase } from './supabaseClient'

// design.md §1a: the Supabase client SDK holds the session; every request to
// the FastAPI backend attaches its access_token as a Bearer header. This
// context is the one place that session is read from, so components never
// touch supabase.auth directly (SignIn takes the client from here too).
const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  // undefined = still loading; the client itself is built from GET /api/config.
  const [supabase, setSupabase] = useState(undefined)
  const [configError, setConfigError] = useState(null)
  const [session, setSession] = useState(undefined)

  useEffect(() => {
    let cancelled = false
    let subscription = null

    loadSupabase().then(({ supabase: client, error }) => {
      if (cancelled) return
      setSupabase(client)
      setConfigError(error)
      if (!client) return
      client.auth.getSession().then(({ data }) => {
        if (!cancelled) setSession(data.session)
      })
      subscription = client.auth.onAuthStateChange((_event, newSession) => setSession(newSession)).data.subscription
    })

    return () => {
      cancelled = true
      subscription?.unsubscribe()
    }
  }, [])

  const value = {
    supabase,
    session,
    loading: supabase === undefined || (supabase !== null && session === undefined),
    configured: supabase !== null,
    configError,
    signOut: () => supabase?.auth.signOut(),
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}

// Thin wrapper around fetch that attaches the current session's access_token
// as a Bearer header — every /api/agent1/* call should go through this
// instead of bare fetch() (design.md §1a; backend rejects unauthenticated
// requests, see backend/document_analyzer/auth.py).
export function useApiFetch() {
  const { session } = useAuth()
  return (path, options = {}) => {
    const headers = new Headers(options.headers || {})
    if (session?.access_token) headers.set('Authorization', `Bearer ${session.access_token}`)
    return fetch(path, { ...options, headers })
  }
}
