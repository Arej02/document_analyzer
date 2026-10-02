// Pure helpers for the GitHub connection panel — design_agent3.md §15.7, §15.12.
// Kept free of React so `node --test` can exercise them. These checks only avoid obviously doomed
// requests; the backend (PUT /api/agent3/projects/{id}/github-connection) is what actually validates.

// Mirrors the backend and database rule: owner/name in GitHub's character set, no "." or ".." segment.
const REPO_RE = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/
// Classic (ghp_), fine-grained (github_pat_), OAuth (gho_) and legacy hex tokens: letters, digits, _ . -
const TOKEN_RE = /^[A-Za-z0-9_.-]{20,255}$/

export const isRepoShaped = (value) => {
  const repo = String(value ?? '').trim()
  return REPO_RE.test(repo) && !repo.split('/').some((part) => part === '.' || part === '..')
}

export const isTokenShaped = (value) => TOKEN_RE.test(String(value ?? '').trim())

// Why the Connect button is disabled, or null when it can be pressed. A reason is shown, not just a
// greyed-out button (same convention as GenerateCodeAction / DownloadProjectButton).
export const connectBlocker = ({ repo, token }) => {
  if (!String(repo ?? '').trim()) return 'Enter the repository as owner/name.'
  if (!isRepoShaped(repo)) return 'The repository must look like owner/name.'
  if (!String(token ?? '').trim()) return 'Paste a GitHub access token.'
  if (!isTokenShaped(token)) return "That doesn't look like a GitHub access token."
  return null
}

// What the panel shows, from GET /github-connection's body and the request state.
export const panelMode = ({ loading, loadError, connection, editing }) => {
  if (loading) return 'loading'
  if (loadError) return 'error'
  if (!connection?.connected) return 'connect'
  return editing ? 'rotate' : 'connected'
}

export const SETTINGS_PATH = 'Settings → General → Pull Requests → “Automatically delete head branches”'
