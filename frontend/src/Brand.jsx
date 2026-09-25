export default function Brand({ className = 'brand' }) {
  return (
    <a className={className} href="/" aria-label="Galvanise home">
      <span className="iridescent-logo" aria-hidden="true">
        <i className="logo-circle logo-circle--large" style={{ '--logo-light': '#d4d5e9', '--logo-base': '#a0a1cf', '--logo-shade': '#8081a6' }} />
        <i className="logo-circle logo-circle--small" style={{ '--logo-light': '#d4d4d4', '--logo-base': '#9f9f9f', '--logo-shade': '#7f7f7f' }} />
        <i className="logo-circle logo-circle--bottom" style={{ '--logo-light': '#aaaeb7', '--logo-base': '#434a5f', '--logo-shade': '#363b4c' }} />
      </span>
      <span>Galvanise</span>
    </a>
  )
}
