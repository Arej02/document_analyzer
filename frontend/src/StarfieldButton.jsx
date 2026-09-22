import { useEffect, useLayoutEffect, useRef } from 'react'
import { useAnimate, useReducedMotion } from 'motion/react'

// Adapted from the "Starfield Button" (Originkit) component the user supplied —
// stripped of TypeScript, Next.js "use client", and the icon/image system this
// app doesn't use, and re-themed to a purple that sits on top of the app's dark
// navy background (see .page-shell in styles.css). Kept the animated glow-ring
// + pixel-starfield mechanics as-is since that's the whole point of the ask.

const MAX_BAND_WIDTH = 30

const num = (v) => {
  const parsed = parseFloat(String(v ?? ''))
  return Number.isFinite(parsed) && parsed > 0 ? Math.min(parsed, MAX_BAND_WIDTH) : 0
}

const bandWidthsOf = (b) => {
  const fused = num(b?.borderWidth)
  return {
    top: b?.borderTopWidth !== undefined ? num(b.borderTopWidth) : fused,
    right: b?.borderRightWidth !== undefined ? num(b.borderRightWidth) : fused,
    bottom: b?.borderBottomWidth !== undefined ? num(b.borderBottomWidth) : fused,
    left: b?.borderLeftWidth !== undefined ? num(b.borderLeftWidth) : fused,
  }
}

const borderColorOf = (b) => b?.borderColor ?? 'transparent'

const WHITE = { r: 255, g: 255, b: 255, a: 1 }

function parseColor(input) {
  if (!input) return WHITE
  let c = String(input).trim()

  const token = c.match(/^var\([^,]+,\s*(.+)\)$/i)
  if (token) c = token[1].trim()

  if (c[0] === '#') {
    let h = c.slice(1)
    if (h.length === 3 || h.length === 4) h = h.split('').map((ch) => ch + ch).join('')
    if (h.length !== 6 && h.length !== 8) return WHITE
    const n = parseInt(h, 16)
    if (Number.isNaN(n)) return WHITE
    return h.length === 6
      ? { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 }
      : { r: (n >>> 24) & 255, g: (n >>> 16) & 255, b: (n >>> 8) & 255, a: (n & 255) / 255 }
  }

  const fn = c.match(/rgba?\(([^)]+)\)/i)
  if (fn) {
    const p = fn[1].split(/[,\s/]+/).filter(Boolean).map(Number)
    if (p.length >= 3 && p.slice(0, 3).every((v) => !Number.isNaN(v)))
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 && !Number.isNaN(p[3]) ? p[3] : 1 }
  }
  return WHITE
}

const rgba = (c, a) => `rgba(${Math.round(c.r)}, ${Math.round(c.g)}, ${Math.round(c.b)}, ${Math.max(0, Math.min(1, a))})`

const radiusFromPercent = (w, h, pct) => (Math.min(w, h) / 2) * (Math.max(0, Math.min(100, pct)) / 100)

const BAND_MASK = {
  maskImage: 'linear-gradient(#000 0 0), linear-gradient(#000 0 0)',
  maskClip: 'border-box, content-box',
  maskComposite: 'exclude',
  WebkitMaskImage: 'linear-gradient(#000 0 0), linear-gradient(#000 0 0)',
  WebkitMaskClip: 'border-box, content-box',
  WebkitMaskComposite: 'xor',
}

const useIsoLayoutEffect = typeof window !== 'undefined' ? useLayoutEffect : useEffect

const rnd = (i, salt) => {
  const x = Math.sin((i + 1) * 12.9898 + salt * 78.233) * 43758.5453
  return x - Math.floor(x)
}

const pointOnRoundRect = (t, w, h, r) => {
  const rr = Math.max(0, Math.min(r, w / 2, h / 2))
  const sx = Math.max(0, w - 2 * rr)
  const sy = Math.max(0, h - 2 * rr)
  const arc = (Math.PI / 2) * rr
  const total = 2 * sx + 2 * sy + 4 * arc
  if (total <= 0) return { x: w / 2, y: h / 2 }

  let d = (((t % 1) + 1) % 1) * total + sx / 2
  d %= total

  if (d < sx) return { x: rr + d, y: 0 }
  d -= sx
  if (d < arc) {
    const a = d / rr
    return { x: w - rr + rr * Math.sin(a), y: rr - rr * Math.cos(a) }
  }
  d -= arc
  if (d < sy) return { x: w, y: rr + d }
  d -= sy
  if (d < arc) {
    const a = d / rr
    return { x: w - rr + rr * Math.cos(a), y: h - rr + rr * Math.sin(a) }
  }
  d -= arc
  if (d < sx) return { x: w - rr - d, y: h }
  d -= sx
  if (d < arc) {
    const a = d / rr
    return { x: rr - rr * Math.sin(a), y: h - rr + rr * Math.cos(a) }
  }
  d -= arc
  if (d < sy) return { x: 0, y: h - rr - d }
  d -= sy
  const a = d / rr
  return { x: rr - rr * Math.cos(a), y: rr - rr * Math.sin(a) }
}

const pointOnRoundRectAngular = (t, w, h, r) => {
  const a = w / 2
  const b = h / 2
  if (a <= 0 || b <= 0) return { x: w / 2, y: h / 2 }
  const rr = Math.max(0, Math.min(r, a, b))
  const th = -Math.PI / 2 + (((t % 1) + 1) % 1) * Math.PI * 2
  const dx = Math.cos(th)
  const dy = Math.sin(th)

  const kx = Math.abs(dx) > 1e-9 ? a / Math.abs(dx) : Infinity
  const ky = Math.abs(dy) > 1e-9 ? b / Math.abs(dy) : Infinity
  let k = Math.min(kx, ky)

  const cx = a - rr
  const cy = b - rr
  if (rr > 0 && Math.abs(dx * k) > cx && Math.abs(dy * k) > cy) {
    const Cx = Math.sign(dx * k) * cx
    const Cy = Math.sign(dy * k) * cy
    const proj = dx * Cx + dy * Cy
    const disc = rr * rr - (Cx * Cx + Cy * Cy) + proj * proj
    k = proj + Math.sqrt(Math.max(0, disc))
  }
  return { x: a + dx * k, y: b + dy * k }
}

const SECONDS_AT_SPEED_1 = 10

// Mint/emerald theme reusing the app's own brand accent (the logo mark, status
// dot and eyebrow text in styles.css are all this same green) instead of an
// unrelated color, so the button reads as part of the existing identity.
const ACCENT = '#63eeaf'
const ACCENT_DEEP = '#0a1a18'

export default function StarfieldButton({
  label = 'Analyze',
  font = {
    fontSize: 14,
    fontWeight: 700,
    letterSpacing: '0em',
    lineHeight: '1.2em',
  },
  showText = true,
  padding = '0 20px',
  rounded = 100,
  fill = `linear-gradient(160deg, #15332c, ${ACCENT_DEEP})`,
  textColor = '#f2fbf7',
  gap = 10,
  border = {
    borderColor: 'rgba(99,238,175,0.35)',
    borderWidth: 1,
  },
  glow = { size: 11, color: ACCENT, opacity: 85 },
  stroke = {
    size: 60,
    color: ACCENT,
    count: 1,
    speed: 45,
    movement: 'continuous',
    direction: 'ccw',
    thickness: 2,
  },
  pixel = { size: 4, color: ACCENT, density: 45, brightness: 100 },
  transition = { ease: [0.44, 0, 0.56, 1], type: 'tween', delay: 0, duration: 0.45 },
  type = 'button',
  disabled = false,
  loading = false,
  onClick,
  style,
  className,
}) {
  const isInert = disabled || loading
  const { color: glowColor = ACCENT, size: glowSize = 11, opacity: glowOpacity = 85 } = glow
  const {
    count: lightCountProp = 1,
    color: lightColor = ACCENT,
    size: lightSize = 60,
    thickness: lightThickness = 2,
    speed: speedPct = 45,
    direction = 'ccw',
    movement = 'continuous',
  } = stroke
  const speed = 2 * (Math.max(0, Math.min(100, Math.round(speedPct))) / 50)
  const { color: pixelColor = ACCENT, size: pixelSize = 4, density: pixelDensity = 45, brightness: pixelBrightness = 100 } = pixel

  const [scope, animate] = useAnimate()
  const buttonRef = useRef(null)
  const trackRef = useRef(null)
  const bandRef = useRef(null)
  const faceRef = useRef(null)
  const innerGlowRef = useRef(null)
  const canvasRef = useRef(null)
  const lightsRef = useRef([])

  const band = bandWidthsOf(border)
  const bandPadding = `${band.top}px ${band.right}px ${band.bottom}px ${band.left}px`
  const bandMax = Math.max(band.top, band.right, band.bottom, band.left)

  const glowAlpha = Math.max(0, Math.min(100, glowOpacity)) / 100
  const glowAlphaRef = useRef(glowAlpha)
  glowAlphaRef.current = glowAlpha

  const lightCount = Math.max(1, Math.min(12, Math.round(lightCountProp)))
  const lightPx = Math.max(4, Math.round(lightSize))
  const lightThick = Math.max(1, Math.min(MAX_BAND_WIDTH, Math.round(lightThickness)))
  const ringInset = bandMax / 2 - lightThick / 2

  const glowPx = Math.max(1, Math.round(glowSize))
  const glowRimPx = Math.max(1, Math.round(glowPx * 0.18))
  const glowBlurPx = Math.max(1, Math.round(glowPx * 0.5))

  const geom = useRef({ w: 0, h: 0, radius: 0 })
  useIsoLayoutEffect(() => {
    const el = buttonRef.current
    if (!el) return
    const applyRadius = () => {
      const w = el.offsetWidth
      const h = el.offsetHeight
      if (!w || !h) return
      const radius = radiusFromPercent(w, h, rounded)
      geom.current = { w, h, radius }
      el.style.borderRadius = `${radius}px`
      if (trackRef.current) trackRef.current.style.borderRadius = `${radius}px`
      if (bandRef.current) bandRef.current.style.borderRadius = `${Math.max(0, radius - ringInset)}px`
      if (faceRef.current) {
        const inset = (v) => Math.max(0, radius - v)
        const x = [inset(band.left), inset(band.right), inset(band.right), inset(band.left)]
        const y = [inset(band.top), inset(band.top), inset(band.bottom), inset(band.bottom)]
        const innerRadius = `${x.map((v) => `${v}px`).join(' ')} / ${y.map((v) => `${v}px`).join(' ')}`
        faceRef.current.style.borderRadius = innerRadius
        if (innerGlowRef.current) innerGlowRef.current.style.borderRadius = innerRadius
      }
    }
    applyRadius()
    const ro = new ResizeObserver(applyRadius)
    ro.observe(el)
    return () => ro.disconnect()
  }, [rounded, ringInset, band.top, band.right, band.bottom, band.left])

  const reveal = useRef(0)
  const revealCtrl = useRef(null)
  const tickCtrl = useRef(null)
  const reducedMotion = useReducedMotion()

  const cfg = {
    pixelColor,
    pixelSize,
    pixelDensity,
    pixelBrightness,
    lightCount,
    bandMax,
    ringInset,
    movement,
    direction,
    turnsPerSec: Math.max(0, speed) / SECONDS_AT_SPEED_1,
  }
  const cfgRef = useRef(cfg)
  cfgRef.current = cfg

  const size = useRef({ w: 1, h: 1, dpr: 1 })
  const city = useRef({ cols: 0, rows: 0, dens: -1, cells: [] })

  const buildCity = (w, h, cell) => {
    const c = Math.max(4, Math.round(cell))
    const cols = Math.max(1, Math.floor(w / c))
    const rows = Math.max(1, Math.floor(h / c))
    const dens = Math.max(0, Math.min(1, cfgRef.current.pixelDensity / 100))
    if (cols === city.current.cols && rows === city.current.rows && dens === city.current.dens && city.current.cells.length) return
    const offX = (w - cols * c) / 2
    const offY = (h - rows * c) / 2
    const cells = []
    for (let r = 0; r < rows; r++) {
      for (let col = 0; col < cols; col++) {
        const i = r * cols + col
        const lit = rnd(i, 1) < dens
        cells.push({
          cx: offX + col * c + c / 2,
          cy: offY + r * c + c / 2,
          base: lit ? 0.5 + rnd(i, 2) * 0.5 : 0.05 + rnd(i, 3) * 0.18,
          speed: 0.6 + rnd(i, 4) * 2.4,
          phase: rnd(i, 5) * Math.PI * 2,
        })
      }
    }
    city.current = { cols, rows, dens, cells }
  }

  const draw = (ctx) => {
    const { w, h, dpr } = size.current
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, w, h)

    const rv = reveal.current
    if (rv < 0.001) return

    const { pixelSize: cell, pixelColor: col } = cfgRef.current
    const lightMul = Math.max(0, cfgRef.current.pixelBrightness) / 100
    buildCity(w, h, cell)
    const { cells } = city.current
    const c = Math.max(4, Math.round(cell))
    const dot = Math.max(1, Math.round(c * 0.62))
    const off = dot / 2
    const t = performance.now() / 1000

    const cx = w / 2
    const cy = h / 2
    const maxD = Math.hypot(cx, cy) || 1

    ctx.globalCompositeOperation = 'lighter'
    ctx.fillStyle = col
    for (let k = 0; k < cells.length; k++) {
      const p = cells[k]
      const tw = 0.55 + 0.45 * Math.sin(t * p.speed + p.phase)
      const d = Math.hypot(p.cx - cx, p.cy - cy) / maxD
      const centerBias = 0.55 + 0.45 * (1 - d)
      let a = p.base * tw * centerBias * rv * lightMul
      if (a <= 0.002) continue
      if (a > 1) a = 1
      ctx.globalAlpha = a
      ctx.fillRect(p.cx - off, p.cy - off, dot, dot)
    }
    ctx.globalAlpha = 1
    ctx.globalCompositeOperation = 'source-over'
  }

  const placeLights = (elapsedSec) => {
    const { w, h, radius } = geom.current
    if (!w || !h) return
    const c = cfgRef.current
    const half = c.bandMax / 2

    const pw = Math.max(0, w - c.bandMax)
    const ph = Math.max(0, h - c.bandMax)
    const pr = Math.max(0, radius - half)
    const dir = c.direction === 'cw' ? 1 : -1
    const base = elapsedSec * c.turnsPerSec * dir

    for (let i = 0; i < c.lightCount; i++) {
      const el = lightsRef.current[i]
      if (!el) continue
      let t = base + i / c.lightCount
      t = ((t % 1) + 1) % 1

      const p = c.movement === 'step' ? pointOnRoundRectAngular(t, pw, ph, pr) : pointOnRoundRect(t, pw, ph, pr)
      el.style.transform = `translate3d(${p.x + half - c.ringInset}px, ${p.y + half - c.ringInset}px, 0)`
    }
  }

  useEffect(() => {
    const canvas = canvasRef.current
    const face = faceRef.current
    if (!canvas || !face) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const applySize = (w, h) => {
      if (w <= 0 || h <= 0) return
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      size.current = { w, h, dpr }
      canvas.width = Math.max(1, Math.floor(w * dpr))
      canvas.height = Math.max(1, Math.floor(h * dpr))
    }

    const ro = new ResizeObserver(() => {
      applySize(face.clientWidth, face.clientHeight)
      placeLights(performance.now() / 1000)
    })
    ro.observe(face)
    applySize(face.clientWidth, face.clientHeight)
    placeLights(performance.now() / 1000)

    if (reducedMotion) {
      reveal.current = 0
      draw(ctx)
    } else {
      tickCtrl.current = animate(0, 1, {
        duration: 1,
        ease: 'linear',
        repeat: Infinity,
        onUpdate: () => {
          draw(ctx)
          placeLights(performance.now() / 1000)
        },
      })
    }

    return () => {
      ro.disconnect()
      tickCtrl.current?.stop()
      tickCtrl.current = null
    }
  }, [reducedMotion])

  useIsoLayoutEffect(() => {
    if (innerGlowRef.current) innerGlowRef.current.style.opacity = String(reveal.current * glowAlpha)
  }, [glowAlpha])

  useIsoLayoutEffect(() => {
    placeLights(performance.now() / 1000)
  }, [lightCount, lightPx, lightThick, movement, direction, rounded, bandMax])

  useEffect(() => () => revealCtrl.current?.stop(), [])

  const revealOpts = () => (reducedMotion ? { duration: 0 } : transition ?? { duration: 0.35 })

  const animateReveal = (to) => {
    revealCtrl.current?.stop()
    revealCtrl.current = animate(reveal.current, to, {
      ...revealOpts(),
      onUpdate: (v) => {
        reveal.current = v
        if (innerGlowRef.current) innerGlowRef.current.style.opacity = String(v * glowAlphaRef.current)
      },
    })
  }

  const scaleTo = (s) => {
    if (buttonRef.current) animate(buttonRef.current, { scale: s }, revealOpts())
  }

  const onEnter = () => { if (!isInert) animateReveal(1) }
  const onLeave = () => { animateReveal(0); scaleTo(1) }

  const lightRGB = parseColor(lightColor)

  return (
    <div
      ref={scope}
      className={className}
      style={{
        minWidth: 80,
        minHeight: 36,
        position: 'relative',
        display: 'inline-grid',
        placeItems: 'stretch',
        overflow: 'visible',
        opacity: disabled ? 0.5 : 1,
        ...style,
      }}
    >
      <button
        type={type}
        disabled={disabled || loading}
        aria-busy={loading || undefined}
        onClick={onClick}
        ref={buttonRef}
        onPointerEnter={onEnter}
        onPointerLeave={onLeave}
        onPointerDown={() => !isInert && scaleTo(0.97)}
        onPointerUp={() => !isInert && scaleTo(1)}
        onFocus={onEnter}
        onBlur={onLeave}
        style={{
          boxSizing: 'border-box',
          position: 'relative',
          padding: bandPadding,
          border: 'none',
          background: 'transparent',
          cursor: isInert ? 'not-allowed' : 'pointer',
          userSelect: 'none',
          textDecoration: 'none',
          overflow: 'visible',
        }}
      >
        <div
          ref={trackRef}
          aria-hidden
          style={{
            position: 'absolute',
            inset: 0,
            boxSizing: 'border-box',
            padding: bandPadding,
            background: borderColorOf(border),
            zIndex: 0,
            pointerEvents: 'none',
            ...BAND_MASK,
          }}
        />

        <div
          ref={bandRef}
          aria-hidden
          style={{
            position: 'absolute',
            top: ringInset,
            right: ringInset,
            bottom: ringInset,
            left: ringInset,
            boxSizing: 'border-box',
            padding: lightThick,
            zIndex: 0,
            pointerEvents: 'none',
            ...BAND_MASK,
          }}
        >
          {Array.from({ length: lightCount }, (_, i) => (
            <div
              key={i}
              ref={(el) => { lightsRef.current[i] = el }}
              style={{
                position: 'absolute',
                top: 0,
                left: 0,
                width: lightPx,
                height: lightPx,
                marginTop: -lightPx / 2,
                marginLeft: -lightPx / 2,
                pointerEvents: 'none',
                background: `radial-gradient(circle, ${lightColor} 0%, ${lightColor} 30%, ${rgba(lightRGB, 0)} 72%)`,
              }}
            />
          ))}
        </div>

        <span
          ref={faceRef}
          style={{
            position: 'relative',
            zIndex: 1,
            boxSizing: 'border-box',
            width: '100%',
            height: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap,
            padding,
            whiteSpace: 'nowrap',
            background: fill,
            overflow: 'hidden',
          }}
        >
          <canvas
            ref={canvasRef}
            aria-hidden
            style={{ position: 'absolute', inset: 0, zIndex: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
          />
          <span
            ref={innerGlowRef}
            aria-hidden
            style={{
              position: 'absolute',
              inset: 0,
              zIndex: 1,
              border: `${glowRimPx}px solid ${glowColor}`,
              filter: `blur(${glowBlurPx}px)`,
              opacity: 0,
              pointerEvents: 'none',
              maskImage: 'linear-gradient(#000 0 0)',
              maskClip: 'border-box',
              WebkitMaskImage: 'linear-gradient(#000 0 0)',
              WebkitMaskClip: 'border-box',
            }}
          />
          {showText && (
            <span
              style={{
                position: 'relative',
                zIndex: 2,
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 8,
                color: textColor,
                ...font,
              }}
            >
              {loading && <span className="starfield-spinner" aria-hidden />}
              {loading ? 'Please wait' : label}
            </span>
          )}
        </span>
      </button>
    </div>
  )
}
