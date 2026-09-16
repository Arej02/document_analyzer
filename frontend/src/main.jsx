import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

const Mark = () => (
  <svg className="brand-mark" viewBox="0 0 30 30" aria-hidden="true">
    <path d="M15 2.5 27.5 15 15 27.5 2.5 15 15 2.5Z" />
    <path d="m15 10 5 5-5 5-5-5 5-5Z" />
  </svg>
)

const GithubIcon = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 2.35a9.65 9.65 0 0 0-3.05 18.81c.49.09.66-.21.66-.47v-1.7c-2.69.58-3.26-1.14-3.26-1.14-.44-1.12-1.08-1.42-1.08-1.42-.88-.61.07-.6.07-.6.97.07 1.49 1 1.49 1 .87 1.48 2.27 1.05 2.82.8.09-.63.34-1.05.62-1.29-2.15-.25-4.42-1.08-4.42-4.78 0-1.05.38-1.91.99-2.58-.1-.25-.43-1.22.1-2.55 0 0 .8-.26 2.65.98a9.2 9.2 0 0 1 4.83 0c1.84-1.24 2.64-.98 2.64-.98.53 1.33.2 2.3.1 2.55.62.67.99 1.53.99 2.58 0 3.71-2.27 4.52-4.43 4.77.35.3.66.88.66 1.77v2.63c0 .26.18.56.67.47A9.65 9.65 0 0 0 12 2.35Z" />
  </svg>
)

function App() {
  const [url, setUrl] = useState('')
  const [tab, setTab] = useState('url')
  const [showToken, setShowToken] = useState(false)
  const [submitted, setSubmitted] = useState(false)

  const submit = (event) => {
    event.preventDefault()
    if (url.trim()) setSubmitted(true)
  }

  return (
    <main className="page-shell">
      <header className="site-header">
        <a className="brand" href="#top" aria-label="galvanise home"><Mark /><span>galvanise</span></a>
        <p className="status"><i /> AI repository intelligence</p>
      </header>

      <section className="hero" id="top">
        <p className="eyebrow">CODEBASE UNDERSTANDING, ON DEMAND</p>
        <h1>See the shape of<br /><em>any codebase.</em></h1>
        <p className="intro">Turn a GitHub repository into an architectural brief, dependency map,<br className="desktop-break" /> and navigable AST overview.</p>

        <form className="analyzer" onSubmit={submit}>
          <div className="tabs" role="tablist" aria-label="Repository source">
            <button className={tab === 'url' ? 'active' : ''} type="button" role="tab" aria-selected={tab === 'url'} onClick={() => setTab('url')}>GitHub URL</button>
            <button className={tab === 'archive' ? 'active' : ''} type="button" role="tab" aria-selected={tab === 'archive'} onClick={() => setTab('archive')}>Upload archive</button>
          </div>
          {tab === 'url' ? <>
            <label htmlFor="repo-url">GitHub repository URL</label>
            <div className="input-row">
              <div className="url-input"><GithubIcon /><input id="repo-url" value={url} onChange={(e) => { setUrl(e.target.value); setSubmitted(false) }} placeholder="https://github.com/owner/repository" /></div>
              <button className="analyze-button" type="submit">Analyze repository</button>
            </div>
            <button className="token-toggle" type="button" onClick={() => setShowToken(!showToken)}><span>{showToken ? '⌄' : '›'}</span> Private repository? Add a GitHub token</button>
            {showToken && <input className="token-input" aria-label="GitHub personal access token" placeholder="GitHub personal access token" type="password" />}
            {submitted && <p className="success">Repository queued for analysis.</p>}
          </> : <div className="upload-state"><strong>Drop a .zip or .tar.gz archive here</strong><span>or select a file from your computer</span><button type="button">Choose archive</button></div>}
        </form>
      </section>

      <footer><i /> Built for the parts of a codebase that aren’t obvious at first glance.</footer>
    </main>
  )
}

createRoot(document.getElementById('root')).render(<App />)
