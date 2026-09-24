export default function PrimaryButton({ label = 'Analyze', type = 'button', disabled = false, loading = false, onClick, style, className = '' }) {
  return (
    <button type={type} disabled={disabled || loading} aria-busy={loading} onClick={onClick} style={style} className={`primary-button ${className}`}>
      {loading && <span className="button-spinner" aria-hidden="true" />}
      <span>{label}</span>
    </button>
  )
}
