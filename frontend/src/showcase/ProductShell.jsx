import { SiteHeader } from './site-header'
import IridescentLogo from './IridescentLogo'
import { LinkedInLink } from './linkedin-link'
import { useAuth } from '../AuthProvider'

export function ProductShell({ children }) {
  const { session, signOut } = useAuth()
  return (
    <div id="workspace" className="product-shell" tabIndex={-1}>
      <SiteHeader />
      {session && <div className="product-account"><button type="button" onClick={signOut}>Sign Out</button></div>}
      {children}
      <footer className="product-footer">
        <div className="product-footer-brand">
          <IridescentLogo size={38} />
          <span className="brand-wordmark">Galvanise</span>
        </div>
        <div className="product-copyright">
          <div className="product-contact"><LinkedInLink /><a href="mailto:hello@galvanise.dev">hello@galvanise.dev</a></div>
          <span>© {new Date().getFullYear()} Galvanise. All rights reserved.</span>
        </div>
      </footer>
    </div>
  )
}
