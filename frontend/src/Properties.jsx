// Proprieta' del livello selezionato (o del documento): ogni campo e' un
// argomento di un'operazione di Store, inviato quando si lascia il controllo.
import { useEffect, useState } from 'react'
import Icon from './Icons.jsx'
import { colorHex, defaultParams, fmtNum, stepFor, titleCase } from './util.js'

// slider che invia solo al rilascio: ogni invio e' un render sul server
export function Slider({ label, value, min, max, step, onCommit }) {
  const [v, setV] = useState(value)
  useEffect(() => setV(value), [value])
  const commit = () => { if (v !== value) onCommit(v) }
  return (
    <label className="slider">
      <span>{label}</span>
      <input type="range" min={min} max={max} step={step} value={v ?? 0}
        onChange={e => setV(+e.target.value)} onPointerUp={commit} onKeyUp={commit} onBlur={commit} />
      <input type="number" className="num" min={min} max={max} step={step} value={fmtNum(v ?? 0)}
        onChange={e => setV(+e.target.value)} onBlur={commit} onKeyDown={e => e.key === 'Enter' && commit()} />
    </label>
  )
}

function Num({ label, value, onCommit, step = 1 }) {
  const [v, setV] = useState(value)
  useEffect(() => setV(value), [value])
  return (
    <label className="numfield"><span>{label}</span>
      <input type="number" step={step} value={v === undefined || v === null ? '' : fmtNum(v)}
        onChange={e => setV(e.target.value === '' ? null : +e.target.value)}
        onBlur={() => v !== value && v !== null && onCommit(v)} onKeyDown={e => e.key === 'Enter' && e.target.blur()} />
    </label>
  )
}

function Color({ label, value, onCommit }) {
  return (
    <label className="numfield"><span>{label}</span>
      <input type="color" value={colorHex(value)} onChange={e => onCommit(e.target.value)} />
    </label>
  )
}

function EffectEditor({ layer, effect, index, catalog, run }) {
  const spec = catalog.find(e => e.name === effect.type)
  const [open, setOpen] = useState(false)
  const upd = (k, val) => run('update_effect', { layer: layer.id, index, params: { [k]: val } })
  return (
    <div className={'effect' + (effect.enabled === false ? ' off' : '')}>
      <div className="effect-head">
        <input type="checkbox" checked={effect.enabled !== false} title="On/off"
          onChange={e => run('update_effect', { layer: layer.id, index, enabled: e.target.checked })} />
        <button className="link" onClick={() => setOpen(!open)}>{titleCase(effect.type)} {open ? '▾' : '▸'}</button>
        <span className="grow" />
        <button className="icon-btn" title="Earlier" disabled={index === 0}
          onClick={() => run('move_effect', { layer: layer.id, index, to: index - 1 })}><Icon name="up" size={13} /></button>
        <button className="icon-btn" title="Remove" onClick={() => run('remove_effect', { layer: layer.id, index })}><Icon name="trash" size={13} /></button>
      </div>
      {open && spec && Object.entries(spec.params).map(([k, p]) => {
        const val = effect.params?.[k] ?? p.default
        if (p.type === 'number' || p.type === 'int')
          return <Slider key={k} label={k} value={val} min={p.min} max={p.max} step={stepFor(p)} onCommit={x => upd(k, x)} />
        if (p.type === 'color') return <Color key={k} label={k} value={val} onCommit={x => upd(k, x)} />
        if (p.type === 'bool') return (
          <label key={k} className="check"><input type="checkbox" checked={!!val} onChange={e => upd(k, e.target.checked)} /> {k}</label>)
        if (p.type === 'points') return (
          <label key={k} className="numfield wide"><span>{k}</span>
            <input defaultValue={val ? JSON.stringify(val) : ''} placeholder="[[0,0],[128,140],[255,255]]"
              onBlur={e => { try { upd(k, e.target.value ? JSON.parse(e.target.value) : null) } catch { /* json rotto */ } }} />
          </label>)
        return null
      })}
    </div>
  )
}

export default function Properties({ project, layer, catalog, run, selection, onAI }) {
  if (!project) return null
  if (!layer) return (
    <div className="props">
      <h4>Document</h4>
      <div className="grid2">
        <Num label="W" value={project.width} onCommit={w => run('resize_canvas', { width: w, height: project.height })} />
        <Num label="H" value={project.height} onCommit={h => run('resize_canvas', { width: project.width, height: h })} />
      </div>
      <div className="row">
        <Color label="Background" value={project.background} onCommit={c => run('set_document', { background: c })} />
        <label className="check"><input type="checkbox" checked={project.background === 'transparent'}
          onChange={e => run('set_document', { background: e.target.checked ? 'transparent' : '#ffffff' })} /> transparent</label>
      </div>
      <button onClick={() => {
        const w = prompt('New width in px (proportions kept):', project.width)
        if (w) run('resize_image', { width: +w })
      }}>Resize image…</button>
      <p className="hint">Select a layer to edit it. Drag a box with the selection tool to mask, crop or remove things.</p>
    </div>
  )

  const L = layer.id
  const box = layer.box
  const addFx = name => run(layer.type === 'adjustment' ? 'add_effect' : 'add_effect',
    { type: name, layer: L, params: defaultParams(catalog.find(e => e.name === name)) })
  const filters = catalog.filter(e => layer.type === 'adjustment' ? e.kind === 'filter' : e.name !== 'color_match')

  return (
    <div className="props">
      <h4>{titleCase(layer.type)} · {layer.name}</h4>
      <Slider label="Opacity" value={layer.opacity ?? 1} min={0} max={1} step={0.01}
        onCommit={v => run('set_layer', { layer: L, opacity: v })} />
      <label className="numfield wide"><span>Blend</span>
        <select value={layer.blend || 'normal'} onChange={e => run('set_layer', { layer: L, blend: e.target.value })}>
          {['normal', 'multiply', 'screen', 'overlay', 'soft_light', 'hard_light', 'darken', 'lighten', 'difference',
            'exclusion', 'color_dodge', 'color_burn', 'add', 'subtract'].map(b => <option key={b} value={b}>{titleCase(b)}</option>)}
        </select>
      </label>

      {box && <>
        <h5>Transform</h5>
        <div className="grid2">
          <Num label="X" value={box[0]} onCommit={x => run('set_transform', { layer: L, x })} />
          <Num label="Y" value={box[1]} onCommit={y => run('set_transform', { layer: L, y })} />
          <Num label="W" value={box[2]} onCommit={w => run('set_transform', { layer: L, width: w })} />
          <Num label="Rot°" value={layer.rotation || 0} onCommit={r => run('set_transform', { layer: L, rotation: r })} />
        </div>
        <div className="row">
          <button onClick={() => run('align_layer', { layer: L, h: 'center', v: 'middle' })}>Center</button>
          <button onClick={() => run('set_transform', { layer: L, flip_h: !(layer.flip && layer.flip[0]) })}>Flip H</button>
          <button onClick={() => run('set_transform', { layer: L, flip_v: !(layer.flip && layer.flip[1]) })}>Flip V</button>
        </div>
      </>}

      {layer.type === 'text' && <>
        <h5>Text</h5>
        <textarea defaultValue={layer.text} key={layer.text} rows={2}
          onBlur={e => e.target.value !== layer.text && run('set_text', { layer: L, text: e.target.value })} />
        <div className="grid2">
          <Num label="Size" value={layer.style.size} onCommit={size => run('set_text', { layer: L, size })} />
          <Color label="Color" value={layer.style.color} onCommit={color => run('set_text', { layer: L, color })} />
          <label className="numfield"><span>Font</span>
            <select value={layer.style.font} onChange={e => run('set_text', { layer: L, font: e.target.value })}>
              {['sans', 'serif', 'mono', 'display', 'handwriting'].concat(
                ['sans', 'serif', 'mono', 'display', 'handwriting'].includes(layer.style.font) ? [] : [layer.style.font])
                .map(f => <option key={f}>{f}</option>)}
            </select></label>
          <label className="check"><input type="checkbox" checked={!!layer.style.bold}
            onChange={e => run('set_text', { layer: L, bold: e.target.checked })} /> bold</label>
          <Num label="Outline" value={layer.style.stroke_width} onCommit={v => run('set_text', { layer: L, stroke_width: v })} />
          <Color label="Outline" value={layer.style.stroke_color} onCommit={v => run('set_text', { layer: L, stroke_color: v })} />
        </div>
      </>}

      {layer.type === 'shape' && <>
        <h5>Shape</h5>
        <div className="grid2">
          <Color label="Fill" value={layer.shape.fill} onCommit={fill => run('set_shape', { layer: L, fill })} />
          <Color label="Stroke" value={layer.shape.stroke} onCommit={stroke => run('set_shape', { layer: L, stroke })} />
          <Num label="Stroke px" value={layer.shape.stroke_width} onCommit={v => run('set_shape', { layer: L, stroke_width: v })} />
          <Num label="Radius" value={layer.shape.radius} onCommit={v => run('set_shape', { layer: L, radius: v })} />
        </div>
      </>}

      {layer.type === 'fill' && <>
        <h5>Fill</h5>
        <label className="numfield wide"><span>Kind</span>
          <select value={layer.fill.kind} onChange={e => run('set_fill', { layer: L, kind: e.target.value })}>
            <option>solid</option><option>linear</option><option>radial</option></select></label>
        <div className="grid2">
          <Color label="Color" value={layer.fill.color} onCommit={color => run('set_fill', { layer: L, color })} />
          <Color label="Color 2" value={layer.fill.color2} onCommit={color2 => run('set_fill', { layer: L, color2 })} />
          <Num label="Angle" value={layer.fill.angle} onCommit={angle => run('set_fill', { layer: L, angle })} />
        </div>
      </>}

      <h5>Mask</h5>
      {layer.mask ? <>
        <div className="row">
          <span className="tag">{layer.mask.kind}{layer.mask.invert ? ' · inverted' : ''}</span>
          <button onClick={() => run('update_mask', { layer: L, invert: !layer.mask.invert })}>Invert</button>
          <button onClick={() => run('remove_mask', { layer: L })}>Remove</button>
        </div>
        <Slider label="Feather" value={layer.mask.feather || 0} min={0} max={200} step={1}
          onCommit={v => run('update_mask', { layer: L, feather: v })} />
      </> : <div className="row">
        <button disabled={!selection} title="Use the current selection as mask"
          onClick={() => run('set_mask', { layer: L, kind: selection.kind, box: selection.box, feather: 0 })}>From selection</button>
        {layer.type === 'image' && <button onClick={() => onAI('remove_background', { layer: L })}><Icon name="wand" size={13} /> Remove BG</button>}
      </div>}

      {layer.type === 'image' && <>
        <h5>AI</h5>
        <div className="row wrap">
          <button onClick={() => onAI('auto_enhance', { layer: L })}><Icon name="sparkle" size={13} /> Enhance</button>
          <button onClick={() => onAI('upscale', { layer: L, factor: 2 })}>Upscale ×2</button>
          <button onClick={() => run('rasterize_layer', { layer: L })} title="Bake effects and mask into pixels">Rasterize</button>
        </div>
      </>}
      {['text', 'shape', 'paint'].includes(layer.type) &&
        <button onClick={() => run('rasterize_layer', { layer: L })}>Rasterize</button>}

      <h5>Effects</h5>
      {(layer.effects || []).map((e, i) => (
        <EffectEditor key={i + e.type} layer={layer} effect={e} index={i} catalog={catalog} run={run} />))}
      <select value="" onChange={e => e.target.value && addFx(e.target.value)}>
        <option value="">+ Add effect…</option>
        {['color', 'detail', 'stylize', 'style'].map(g => (
          <optgroup key={g} label={titleCase(g)}>
            {filters.filter(e => e.group === g).map(e => <option key={e.name} value={e.name}>{titleCase(e.name)}</option>)}
          </optgroup>))}
      </select>
    </div>
  )
}
