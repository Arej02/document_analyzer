/** Radio group styled as a segmented control. Native radios keep arrow-key and focus behaviour. */
export function Segmented({ name, legend, value, options, onChange }) {
  return (
    <div className="ui-seg" role="radiogroup" aria-label={legend}>
      {options.map((o) => (
        <label key={o.value}>
          <input type="radio" name={name} value={o.value} checked={value === o.value} onChange={() => onChange(o.value)} />
          {o.icon}{o.label}
        </label>
      ))}
    </div>
  )
}
