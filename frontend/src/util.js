// 界面纯函数，通过 test-util.mjs 测试，无需浏览器。
import { LABELS } from './labels.js'

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))

// 根据拖动的两点生成标准化矩形，使用文档像素坐标。
export function rectFrom(a, b) {
  const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y)
  return { x, y, w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y) }
}

export const boxArr = r => [Math.round(r.x), Math.round(r.y), Math.round(r.w), Math.round(r.h)]

// 返回边界包含该点的最上方图层，layers 按从上到下排列。
export function hitLayer(layers, p) {
  for (const ly of layers || []) {
    if (ly.visible === false || !ly.box) continue
    const [x, y, w, h] = ly.box
    if (p.x >= x && p.x <= x + w && p.y >= y && p.y <= y + h) return ly
  }
  return null
}

// 计算使文档完整放入视口并保留边距的缩放比例。
export function fitScale(docW, docH, viewW, viewH, pad = 32) {
  if (!docW || !docH || viewW <= 0 || viewH <= 0) return 1
  return Math.max(0.02, Math.min((viewW - pad * 2) / docW, (viewH - pad * 2) / docH, 4))
}

// 收集笔画点，丢弃与上一点过近的点。
export function addPoint(points, p, minDist = 2) {
  const last = points[points.length - 1]
  if (last && Math.hypot(p.x - last[0], p.y - last[1]) < minDist) return points
  return [...points, [Math.round(p.x * 10) / 10, Math.round(p.y * 10) / 10]]
}

// 从效果目录获取默认参数。
export function defaultParams(effect) {
  const out = {}
  for (const [k, p] of Object.entries(effect?.params || {})) {
    if (p.type === 'points' || p.type === 'vector') continue
    out[k] = p.default
  }
  return out
}

// 根据参数范围确定滑块步长。
export function stepFor(p) {
  if (p.type === 'int') return 1
  const span = (p.max ?? 1) - (p.min ?? 0)
  if (span <= 2) return 0.01
  if (span <= 20) return 0.1
  return 1
}

export const fmtNum = v => (typeof v === 'number' ? (Math.abs(v) >= 100 ? v.toFixed(0) : (+v.toFixed(3)).toString()) : String(v))

// 将 #ff000080 转为 #ff0000，供颜色输入控件使用。
export const colorHex = c => (typeof c === 'string' && /^#[0-9a-f]{6}/i.test(c) ? c.slice(0, 7) : '#000000')

export const titleCase = s => LABELS[s] || s || ''

// 拖动边角后计算新边界框，保持宽高比。
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

// 将从上到下的图层树展平，按面板顺序先放组再放子图层，
// 同时记录深度。open=true 时跳过已折叠组的子图层，
// open=false 时保留所有子图层。
export function flatLayers(layers, open = false, depth = 0, parent = null, hidden = false) {
  const out = []
  for (const ly of layers || []) {
    const off = hidden || ly.visible === false
    out.push({ ...ly, depth, parent, hiddenByParent: hidden })
    if (ly.type === 'group' && (!open || !ly.collapsed))
      out.push(...flatLayers(ly.children, open, depth + 1, ly.id, off))
  }
  return out
}

export const findLayer = (layers, id) => flatLayers(layers).find(l => l.id === id) || null

// 命中检测：组无独立边界框，隐藏组的子图层不参与检测。
export function hitLayerDeep(layers, p) {
  return hitLayer(flatLayers(layers).filter(l => !l.hiddenByParent), p)
}

// 查找包含指定图层的根列表或组内列表。
export function siblingsOf(layers, id) {
  if ((layers || []).some(l => l.id === id)) return layers
  for (const l of layers || []) {
    if (l.type === 'group') {
      const s = siblingsOf(l.children, id)
      if (s) return s
    }
  }
  return null
}
