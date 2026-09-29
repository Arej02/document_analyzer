// Pass as `Toc`'s `footer`. Renders nothing when neither a commit nor a time is known.
import { provenance } from './provenance.js'

export default function Provenance({ commitSha, generatedAt }) {
  const p = provenance({ commitSha, generatedAt })
  if (!p) return null
  return <>Generated{p.commit ? <> from <code>{p.commit}</code></> : null}{p.ago ? <> {p.ago}</> : null}</>
}
