// Funzioni pure dell'interfaccia: provate da test-util.mjs senza browser.

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))

// rettangolo normalizzato da due punti di trascinamento (pixel del documento)
export function rectFrom(a, b) {
  const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y)
  return { x, y, w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y) }
}

export const boxArr = r => [Math.round(r.x), Math.round(r.y), Math.round(r.w), Math.round(r.h)]

// livello piu' in alto il cui riquadro contiene il punto (layers: dall'alto)
export function hitLayer(layers, p) {
  for (const ly of layers || []) {
    if (ly.visible === false || !ly.box) continue
    const [x, y, w, h] = ly.box
    if (p.x >= x && p.x <= x + w && p.y >= y && p.y <= y + h) return ly
  }
  return null
}

// scala per far stare il documento nella vista, con un margine
export function fitScale(docW, docH, viewW, viewH, pad = 32) {
  if (!docW || !docH || viewW <= 0 || viewH <= 0) return 1
  return Math.max(0.02, Math.min((viewW - pad * 2) / docW, (viewH - pad * 2) / docH, 4))
}

// punti di una pennellata: si scartano quelli troppo vicini al precedente
export function addPoint(points, p, minDist = 2) {
  const last = points[points.length - 1]
  if (last && Math.hypot(p.x - last[0], p.y - last[1]) < minDist) return points
  return [...points, [Math.round(p.x * 10) / 10, Math.round(p.y * 10) / 10]]
}

// parametri di default di un effetto dal catalogo
export function defaultParams(effect) {
  const out = {}
  for (const [k, p] of Object.entries(effect?.params || {})) {
    if (p.type === 'points' || p.type === 'vector') continue
    out[k] = p.default
  }
  return out
}

// passo dello slider dall'intervallo del parametro
export function stepFor(p) {
  if (p.type === 'int') return 1
  const span = (p.max ?? 1) - (p.min ?? 0)
  if (span <= 2) return 0.01
  if (span <= 20) return 0.1
  return 1
}

export const fmtNum = v => (typeof v === 'number' ? (Math.abs(v) >= 100 ? v.toFixed(0) : (+v.toFixed(3)).toString()) : String(v))

// "#ff000080" -> "#ff0000" per <input type=color>
export const colorHex = c => (typeof c === 'string' && /^#[0-9a-f]{6}/i.test(c) ? c.slice(0, 7) : '#000000')

export const titleCase = s => (s || '').replace(/_/g, ' ').replace(/^./, c => c.toUpperCase())

// nuovo box dopo il trascinamento di un angolo, proporzioni mantenute
export function resizeFromCorner(box, corner, d) {
  const [x, y, w, h] = box
  const ratio = w / h || 1
  let nw = w
  if (corner === 'se') nw = w + d.x
  else if (corner === 'sw') nw = w - d.x
  else if (corner === 'ne') nw = w + d.x
  else if (corner === 'nw') nw = w - d.x
  nw = Math.max(4, nw)
  const nh = nw / ratio
  const nx = corner === 'sw' || corner === 'nw' ? x + (w - nw) : x
  const ny = corner === 'ne' || corner === 'nw' ? y + (h - nh) : y
  return [nx, ny, nw, nh]
}
