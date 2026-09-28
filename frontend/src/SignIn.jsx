import { useState } from 'react'
import { useAuth } from './AuthProvider'
import PrimaryButton from './PrimaryButton'


const MailIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M4 6h16v12H4z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    <path d="m4.5 6.5 7.5 6 7.5-6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

const LockIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <rect x="5" y="11" width="14" height="9" rx="2" stroke="currentColor" strokeWidth="1.6" />
    <path d="M8 11V7.5a4 4 0 0 1 8 0V11" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
  </svg>
)

const EyeIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    <circle cx="12" cy="12" r="2.6" stroke="currentColor" strokeWidth="1.6" />
  </svg>
)

const EyeOffIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M3 3l18 18" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    <path d="M10.6 5.7A9.7 9.7 0 0 1 12 5.6c6.4 0 10 6.5 10 6.5a15.4 15.4 0 0 1-3.4 4.2M6.6 6.7C4 8.4 2 12 2 12s3.6 6.5 10 6.5a9.6 9.6 0 0 0 4-.86" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    <path d="M9.9 10a2.6 2.6 0 0 0 3.6 3.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

// Email/password auth against Supabase Auth — the simplest provider to test
// against locally without configuring OAuth or email-link delivery. Other
// providers (magic link, OAuth) can be added here later without touching
// AuthProvider or any of the API call sites, since they all just read
// session off context.
export function SignIn() {
  const { supabase } = useAuth()
  const [mode, setMode] = useState('sign-in') // 'sign-in' | 'sign-up'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState(null)
  const [info, setInfo] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  const submit = async (event) => {
    event.preventDefault()
    setError(null)
    setInfo(null)
    setSubmitting(true)
    try {
      if (mode === 'sign-in') {
        const { error: signInError } = await supabase.auth.signInWithPassword({ email, password })
        if (signInError) throw signInError
      } else {
        const { data, error: signUpError } = await supabase.auth.signUp({ email, password })
        if (signUpError) throw signUpError
        // With a session, AuthProvider's onAuthStateChange already switches to the app.
        if (!data.session) {
          setInfo('Account created. Check your inbox to confirm it, or turn off "Confirm email" in Supabase → Authentication → Sign In / Providers.')
        }
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  const switchMode = () => {
    setMode(mode === 'sign-in' ? 'sign-up' : 'sign-in')
    setError(null)
    setInfo(null)
  }

  return (
    <main className="auth-page">
      <div className="auth-glow" aria-hidden="true" />
      <div className="auth-wrap">

        <div className="auth-card">
          <h1 className="auth-heading">{mode === 'sign-in' ? 'Welcome back' : 'Create your account'}</h1>
          <p className="auth-subtext">
            {mode === 'sign-in'
              ? 'Sign in to continue'
              : 'Sign up with email'}
          </p>

          <form onSubmit={submit} noValidate>
            <div className="auth-field">
              <label htmlFor="auth-email">Email</label>
              <div className="auth-input-shell">
                <span className="auth-input-icon"><MailIcon /></span>
                <input
                  id="auth-email"
                  type="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                />
              </div>
            </div>

            <div className="auth-field">
              <label htmlFor="auth-password">Password</label>
              <div className="auth-input-shell">
                <span className="auth-input-icon"><LockIcon /></span>
                <input
                  id="auth-password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete={mode === 'sign-in' ? 'current-password' : 'new-password'}
                  required
                  minLength={6}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                />
                <button
                  type="button"
                  className="auth-toggle-visibility"
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  aria-pressed={showPassword}
                >
                  {showPassword ? <EyeOffIcon /> : <EyeIcon />}
                </button>
              </div>
            </div>

            <div className="auth-submit">
              <PrimaryButton label="Continue" type="submit" loading={submitting} />
            </div>
          </form>

          <p className="auth-switch">
            {mode === 'sign-in' ? "Don't have an account?" : 'Already have an account?'}
            <button type="button" onClick={switchMode}>
              {mode === 'sign-in' ? 'Sign up' : 'Sign in'}
            </button>
          </p>

          {error && <p className="auth-error" role="alert">{error}</p>}
          {info && <p className="auth-info" role="status">{info}</p>}
        </div>
      </div>
    </main>
  )
}
