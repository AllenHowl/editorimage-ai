// La tela: l'immagine renderizzata dal server (identica all'export) e sopra uno
// strato SVG in coordinate del documento per selezioni, riquadri e pennellate.
// Nessun pixel si calcola qui: ogni gesto finisce in un'operazione di Store.
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { addPoint, boxArr, fitScale, hitLayer, rectFrom, resizeFromCorner } from './util.js'

export default function Canvas({ project, revision, tool, toolOpts, selection, setSelection, selectedId,
  setSelectedId, run, zoom, setZoom, setCursor, onTextAt }) {
  const viewRef = useRef(null)
  const svgRef = useRef(null)
  const [view, setView] = useState({ w: 800, h: 600 })
  const [shown, setShown] = useState(null)
  const [wanted, setWanted] = useState(null)
  const latest = useRef(null)
  const [drag, setDragState] = useState(null)
  // il gesto vive anche in un ref: pointerup puo' arrivare prima del render che vede l'ultimo move
  const dragRef = useRef(null)
  const setDrag = v => {
    dragRef.current = typeof v === 'function' ? v(dragRef.current) : v
    setDragState(dragRef.current)
  }
  const W = project?.width || 1, H = project?.height || 1

  useLayoutEffect(() => {
    const el = viewRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setView({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const fit = fitScale(W, H, view.w, view.h)
  const scale = zoom === 'fit' ? fit : zoom
  const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1
  const reqW = Math.min(W, Math.ceil(W * scale * dpr / 64) * 64)

  // si precarica la nuova anteprima e si sostituisce solo quando e' pronta: niente lampi
  useEffect(() => {
    if (!project) return
    const src = `/api/render?w=${reqW}&rev=${revision}`
    setWanted(src)
    latest.current = src
    const img = new Image()
    // un'anteprima vecchia che arriva dopo la nuova non deve sostituirla
    const done = () => { if (latest.current === src) setShown(src) }
    img.onload = done
    img.onerror = done
    img.src = src
  }, [revision, reqW, !!project])

  if (!project) return <div className="canvas-view" ref={viewRef} />

  const layers = project.layers || []
  const sel = layers.find(l => l.id === selectedId)

  const toDoc = e => {
    const r = svgRef.current.getBoundingClientRect()
    return { x: (e.clientX - r.left) / scale, y: (e.clientY - r.top) / scale }
  }

  const down = e => {
    if (e.button !== 0) return
    const p = toDoc(e)
    e.currentTarget.setPointerCapture(e.pointerId)
    if (tool === 'move') {
      const corner = e.target.dataset?.corner
      if (corner && sel?.box) { setDrag({ kind: 'resize', corner, start: p, box: sel.box, cur: p }); return }
      const hit = sel?.box && hitLayer([sel], p) ? sel : hitLayer(layers, p)
      if (hit) {
        setSelectedId(hit.id)
        if (!hit.locked) setDrag({ kind: 'move', start: p, box: hit.box, cur: p, id: hit.id })
      } else setSelectedId(null)
    } else if (tool === 'brush' || tool === 'eraser') {
      setDrag({ kind: 'stroke', points: [[p.x, p.y]] })
    } else if (tool === 'text') {
      onTextAt(p)
    } else if (['marquee', 'ellipse', 'crop', 'shape'].includes(tool)) {
      setDrag({ kind: 'rect', start: p, cur: p })
    } else if (tool === 'hand') {
      setDrag({ kind: 'pan', sx: e.clientX, sy: e.clientY, l: viewRef.current.scrollLeft, t: viewRef.current.scrollTop })
    }
  }

  const move = e => {
    const p = toDoc(e)
    setCursor({ x: Math.round(p.x), y: Math.round(p.y) })
    const drag = dragRef.current
    if (!drag) return
    if (drag.kind === 'stroke') setDrag(d => ({ ...d, points: addPoint(d.points, p, Math.max(1, 2 / scale)) }))
    else if (drag.kind === 'pan') {
      viewRef.current.scrollLeft = drag.l - (e.clientX - drag.sx)
      viewRef.current.scrollTop = drag.t - (e.clientY - drag.sy)
    } else setDrag(d => ({ ...d, cur: p }))
  }

  const up = async e => {
    if (dragRef.current && e && e.clientX !== undefined) move(e)
    const d = dragRef.current
    setDrag(null)
    if (!d) return
    if (d.kind === 'move') {
      const dx = d.cur.x - d.start.x, dy = d.cur.y - d.start.y
      if (Math.abs(dx) + Math.abs(dy) < 0.5) return
      await run('set_transform', { layer: d.id, x: d.box[0] + dx, y: d.box[1] + dy })
    } else if (d.kind === 'resize') {
      const nb = resizeFromCorner(d.box, d.corner, { x: d.cur.x - d.start.x, y: d.cur.y - d.start.y })
      await run('set_transform', { layer: sel.id, x: nb[0], y: nb[1], width: nb[2] })
    } else if (d.kind === 'stroke') {
      const target = sel && sel.type === 'paint' && !sel.locked ? sel.id : null
      const o = toolOpts
      await run('paint_stroke', { points: d.points, layer: target, color: o.color, size: o.size,
        hardness: o.hardness, opacity: o.opacity, erase: tool === 'eraser' })
    } else if (d.kind === 'rect') {
      const r = rectFrom(d.start, d.cur)
      if (r.w < 2 || r.h < 2) { if (tool !== 'shape') setSelection(null); return }
      if (tool === 'shape') {
        const res = await run('add_shape', { kind: toolOpts.shape, x: r.x, y: r.y, w: r.w, h: r.h,
          fill: toolOpts.fill, stroke: toolOpts.strokeOn ? toolOpts.stroke : null,
          stroke_width: toolOpts.strokeOn ? toolOpts.strokeWidth : 0, radius: toolOpts.radius })
        if (res?.result?.id) setSelectedId(res.result.id)
      } else setSelection({ kind: tool === 'ellipse' ? 'ellipse' : 'rect', box: boxArr(r), crop: tool === 'crop' })
    }
  }

  // riquadro del livello selezionato, spostato durante il trascinamento
  let selBox = sel?.box
  if (drag?.kind === 'move' && selBox) selBox = [drag.box[0] + drag.cur.x - drag.start.x, drag.box[1] + drag.cur.y - drag.start.y, drag.box[2], drag.box[3]]
  if (drag?.kind === 'resize' && selBox) selBox = resizeFromCorner(drag.box, drag.corner, { x: drag.cur.x - drag.start.x, y: drag.cur.y - drag.start.y })
  const hs = 7 / scale
  const live = drag?.kind === 'rect' ? rectFrom(drag.start, drag.cur) : null
  const cursor = { move: 'default', hand: drag ? 'grabbing' : 'grab', text: 'text' }[tool] || 'crosshair'

  return (
    <div className="canvas-view" ref={viewRef}
      onWheel={e => {
        if (!e.ctrlKey) return
        e.preventDefault()
        const z = clampZoom(scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15))
        setZoom(z)
      }}>
      <div className="stage-wrap" style={{ minWidth: W * scale + 64, minHeight: H * scale + 64 }}>
        <div className={'stage' + (project.background === 'transparent' ? ' checker' : '')}
          style={{ width: W * scale, height: H * scale }}>
          {shown && <img src={shown} alt="" draggable={false} />}
          {wanted && wanted !== shown && <div className="rendering">rendering…</div>}
          <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} width={W * scale} height={H * scale} style={{ cursor }}
            onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerLeave={() => setCursor(null)}>
            {selection && (selection.kind === 'ellipse'
              ? <ellipse className="marching" cx={selection.box[0] + selection.box[2] / 2} cy={selection.box[1] + selection.box[3] / 2}
                rx={selection.box[2] / 2} ry={selection.box[3] / 2} strokeWidth={1.5 / scale} />
              : <rect className={'marching' + (selection.crop ? ' crop' : '')} x={selection.box[0]} y={selection.box[1]}
                width={selection.box[2]} height={selection.box[3]} strokeWidth={1.5 / scale} />)}
            {live && (tool === 'ellipse' || (tool === 'shape' && toolOpts.shape === 'ellipse')
              ? <ellipse className="live" cx={live.x + live.w / 2} cy={live.y + live.h / 2} rx={live.w / 2} ry={live.h / 2} strokeWidth={1.5 / scale} />
              : <rect className="live" x={live.x} y={live.y} width={live.w} height={live.h} strokeWidth={1.5 / scale} />)}
            {drag?.kind === 'stroke' && drag.points.length > 0 && (
              <polyline points={drag.points.map(p => p.join(',')).join(' ')} fill="none"
                stroke={tool === 'eraser' ? 'rgba(255,255,255,.6)' : toolOpts.color} strokeWidth={toolOpts.size}
                strokeLinecap="round" strokeLinejoin="round" opacity={toolOpts.opacity} />)}
            {tool === 'move' && selBox && (
              <g className="selbox">
                <rect x={selBox[0]} y={selBox[1]} width={selBox[2]} height={selBox[3]} strokeWidth={1.2 / scale} />
                {!sel.locked && sel.type !== 'text' && [['nw', 0, 0], ['ne', 1, 0], ['sw', 0, 1], ['se', 1, 1]].map(([c, fx, fy]) => (
                  <rect key={c} data-corner={c} className="handle" x={selBox[0] + selBox[2] * fx - hs / 2}
                    y={selBox[1] + selBox[3] * fy - hs / 2} width={hs} height={hs} strokeWidth={1 / scale} />))}
              </g>)}
          </svg>
        </div>
      </div>
    </div>
  )
}

export const clampZoom = z => Math.min(16, Math.max(0.02, z))
