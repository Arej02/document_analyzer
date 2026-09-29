import { useCallback, useEffect, useRef, useState } from 'react'

/** Copies text and returns a status message that clears itself. Falls back to execCommand outside secure contexts. */
export function useCopy() {
  const [message, setMessage] = useState('')
  const timer = useRef()
  useEffect(() => () => clearTimeout(timer.current), [])
  const copy = useCallback(async (text, label = 'Copied') => {
    let ok = true
    try { await navigator.clipboard.writeText(text) } catch {
      const ta = Object.assign(document.createElement('textarea'), { value: text })
      ta.style.position = 'fixed'; ta.style.opacity = '0'
      document.body.appendChild(ta); ta.select()
      try { ok = document.execCommand('copy') } catch { ok = false }
      ta.remove()
    }
    setMessage(ok ? label : 'Copy failed. Select the text and copy manually.')
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setMessage(''), 2200)
  }, [])
  return [message, copy]
}
