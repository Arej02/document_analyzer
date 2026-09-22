import { createContext, useContext, useEffect, useState } from 'react'
import { supabase, isSupabaseConfigured } from './supabaseClient'

// design.md §1a: the Supabase client SDK holds the session; every request to
// the FastAPI backend attaches its access_token as a Bearer header. This
// context is the one place that session is read from, so components never
// touch supabase.auth directly.
const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [session, setSession] = useState(undefined) // undefined = still loading

  useEffect(() => {
    if (!isSupabaseConfigured) return
    supabase.auth.getSession().then(({ data }) => setSession(data.session))

    const { data: subscription } = supabase.auth.onAuthStateChange((_event, newSession) => {
      setSession(newSession)
    })
    return () => subscription.subscription.unsubscribe()
  }, [])

  const value = {
    session,
    loading: isSupabaseConfigured && session === undefined,
    configured: isSupabaseConfigured,
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
