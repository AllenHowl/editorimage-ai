// 显示所选图层或文档的属性，每个字段对应 Store 操作参数，
// 在用户离开控件时提交修改。
import { useEffect, useState } from 'react'
import Icon from './Icons.jsx'
import { colorHex, defaultParams, fmtNum, stepFor, titleCase } from './util.js'

// 仅在松开滑块时发送请求，因为每次提交都会触发服务器渲染。
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
        <input type="checkbox" checked={effect.enabled !== false} title="启用 / 禁用"
          onChange={e => run('update_effect', { layer: layer.id, index, enabled: e.target.checked })} />
        <button className="link" onClick={() => setOpen(!open)}>{titleCase(effect.type)} {open ? '▾' : '▸'}</button>
        <span className="grow" />
        <button className="icon-btn" title="提前执行" disabled={index === 0}
          onClick={() => run('move_effect', { layer: layer.id, index, to: index - 1 })}><Icon name="up" size={13} /></button>
        <button className="icon-btn" title="移除" onClick={() => run('remove_effect', { layer: layer.id, index })}><Icon name="trash" size={13} /></button>
      </div>
      {open && spec && Object.entries(spec.params).map(([k, p]) => {
        const val = effect.params?.[k] ?? p.default
        if (p.type === 'number' || p.type === 'int')
          return <Slider key={k} label={titleCase(k)} value={val} min={p.min} max={p.max} step={stepFor(p)} onCommit={x => upd(k, x)} />
        if (p.type === 'color') return <Color key={k} label={titleCase(k)} value={val} onCommit={x => upd(k, x)} />
        if (p.type === 'bool') return (
          <label key={k} className="check"><input type="checkbox" checked={!!val} onChange={e => upd(k, e.target.checked)} /> {k === 'mono' ? '单色噪点' : titleCase(k)}</label>)
        if (p.type === 'points') return (
          <label key={k} className="numfield wide"><span>{titleCase(k)}</span>
            <input defaultValue={val ? JSON.stringify(val) : ''} placeholder="[[0,0],[128,140],[255,255]]"
              onBlur={e => { try { upd(k, e.target.value ? JSON.parse(e.target.value) : null) } catch { /* 忽略无效的 JSON 输入。 */ } }} />
          </label>)
        return null
      })}
    </div>
  )
}

export default function Properties({ project, layer, catalog, run, selection, onAI, blendModes, openDialog, maskMode }) {
  if (!project) return null
  if (!layer) return (
    <div className="props">
      <h4>文档</h4>
      <div className="grid2">
        <Num label="宽" value={project.width} onCommit={w => run('resize_canvas', { width: w, height: project.height })} />
        <Num label="高" value={project.height} onCommit={h => run('resize_canvas', { width: project.width, height: h })} />
      </div>
      <div className="row">
        <Color label="背景" value={project.background} onCommit={c => run('set_document', { background: c })} />
        <label className="check"><input type="checkbox" checked={project.background === 'transparent'}
          onChange={e => run('set_document', { background: e.target.checked ? 'transparent' : '#ffffff' })} /> 透明</label>
      </div>
      <div className="row wrap">
        <button onClick={() => {
          const w = prompt('新宽度（像素，保持比例）：', project.width)
          if (w) run('resize_image', { width: +w })
        }}>调整图片大小…</button>
        <button onClick={() => openDialog('expand')}><Icon name="expand" size={13} /> 扩展画布…</button>
        <button onClick={() => openDialog('describe', {})}><Icon name="eye2" size={13} /> 描述图片</button>
      </div>
      <p className="hint">选择图层进行编辑。用选区工具框选区域，可添加蒙版、裁剪或移除物体。</p>
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
      <Slider label="不透明度" value={layer.opacity ?? 1} min={0} max={1} step={0.01}
        onCommit={v => run('set_layer', { layer: L, opacity: v })} />
      <label className="numfield wide"><span>混合模式</span>
        <select value={layer.blend || 'normal'} onChange={e => run('set_layer', { layer: L, blend: e.target.value })}>
          {(layer.type === 'group' ? ['pass_through'] : []).concat(blendModes || ['normal'])
            .map(b => <option key={b} value={b}>{b === 'add' ? '线性减淡（添加）' : titleCase(b)}</option>)}
        </select>
      </label>
      {layer.type !== 'group' && <label className="check" title="仅在下方图层有像素的区域显示此图层">
        <input type="checkbox" checked={!!layer.clip} onChange={e => run('set_layer', { layer: L, clip: e.target.checked })} /> 剪贴到下方图层</label>}
      {layer.type === 'group' && <p className="hint">
        {layer.blend === 'pass_through' ? '穿透：组内的调整会影响下方所有图层。'
          : '隔离：组内的调整仅影响该组。'}</p>}

      {box && <>
        <h5>变换</h5>
        <div className="grid2">
          <Num label="X" value={box[0]} onCommit={x => run('set_transform', { layer: L, x })} />
          <Num label="Y" value={box[1]} onCommit={y => run('set_transform', { layer: L, y })} />
          <Num label="宽" value={box[2]} onCommit={w => run('set_transform', { layer: L, width: w })} />
          <Num label="旋转°" value={layer.rotation || 0} onCommit={r => run('set_transform', { layer: L, rotation: r })} />
        </div>
        <div className="row">
          <button onClick={() => run('align_layer', { layer: L, h: 'center', v: 'middle' })}>居中</button>
          <button onClick={() => run('set_transform', { layer: L, flip_h: !(layer.flip && layer.flip[0]) })}>水平翻转</button>
          <button onClick={() => run('set_transform', { layer: L, flip_v: !(layer.flip && layer.flip[1]) })}>垂直翻转</button>
        </div>
      </>}

      {layer.type === 'text' && <>
        <h5>文字</h5>
        <textarea defaultValue={layer.text} key={layer.text} rows={2}
          onBlur={e => e.target.value !== layer.text && run('set_text', { layer: L, text: e.target.value })} />
        <div className="grid2">
          <Num label="大小" value={layer.style.size} onCommit={size => run('set_text', { layer: L, size })} />
          <Color label="颜色" value={layer.style.color} onCommit={color => run('set_text', { layer: L, color })} />
          <label className="numfield"><span>字体</span>
            <select value={layer.style.font} onChange={e => run('set_text', { layer: L, font: e.target.value })}>
              {['sans', 'serif', 'mono', 'display', 'handwriting'].concat(
                ['sans', 'serif', 'mono', 'display', 'handwriting'].includes(layer.style.font) ? [] : [layer.style.font])
                .map(f => <option key={f} value={f}>{titleCase(f)}</option>)}
            </select></label>
          <label className="check"><input type="checkbox" checked={!!layer.style.bold}
            onChange={e => run('set_text', { layer: L, bold: e.target.checked })} /> 粗体</label>
          <Num label="轮廓" value={layer.style.stroke_width} onCommit={v => run('set_text', { layer: L, stroke_width: v })} />
          <Color label="轮廓" value={layer.style.stroke_color} onCommit={v => run('set_text', { layer: L, stroke_color: v })} />
        </div>
      </>}

      {layer.type === 'shape' && <>
        <h5>形状</h5>
        <div className="grid2">
          <Color label="填充" value={layer.shape.fill} onCommit={fill => run('set_shape', { layer: L, fill })} />
          <Color label="描边" value={layer.shape.stroke} onCommit={stroke => run('set_shape', { layer: L, stroke })} />
          <Num label="描边宽度" value={layer.shape.stroke_width} onCommit={v => run('set_shape', { layer: L, stroke_width: v })} />
          <Num label="圆角" value={layer.shape.radius} onCommit={v => run('set_shape', { layer: L, radius: v })} />
        </div>
      </>}

      {layer.type === 'fill' && <>
        <h5>填充</h5>
        <label className="numfield wide"><span>类型</span>
          <select value={layer.fill.kind} onChange={e => run('set_fill', { layer: L, kind: e.target.value })}>
            <option value="solid">纯色</option><option value="linear">线性渐变</option><option value="radial">径向渐变</option></select></label>
        <div className="grid2">
          <Color label="颜色" value={layer.fill.color} onCommit={color => run('set_fill', { layer: L, color })} />
          <Color label="颜色 2" value={layer.fill.color2} onCommit={color2 => run('set_fill', { layer: L, color2 })} />
          <Num label="角度" value={layer.fill.angle} onCommit={angle => run('set_fill', { layer: L, angle })} />
        </div>
      </>}

      <h5>蒙版</h5>
      {layer.mask ? <>
        <div className="row wrap">
          <span className="tag">{titleCase(layer.mask.kind)}{layer.mask.invert ? ' · 已反相' : ''}{layer.mask.strokes ? ` · ${layer.mask.strokes} 笔` : ''}</span>
          <button onClick={() => run('update_mask', { layer: L, invert: !layer.mask.invert })}>反相</button>
          {layer.mask.strokes > 0 && <button title="移除蒙版上的最后一笔"
            onClick={() => run('remove_mask_stroke', { layer: L })}>撤销笔画</button>}
          <button onClick={() => run('remove_mask', { layer: L })}>移除</button>
        </div>
        <p className="hint">{maskMode ? '画笔和橡皮擦作用于此蒙版（黑色隐藏，白色显示）。'
          : '选择画笔，将“绘制到”切换为蒙版，即可细化边缘。'}</p>
        <Slider label="羽化" value={layer.mask.feather || 0} min={0} max={200} step={1}
          onCommit={v => run('update_mask', { layer: L, feather: v })} />
      </> : <div className="row">
        <button disabled={!selection} title="将当前选区用作蒙版"
          onClick={() => run('set_mask', { layer: L, kind: selection.kind, box: selection.box, feather: 0 })}>从选区创建</button>
        <button title="创建可用画笔绘制的蒙版（初始全部显示）"
          onClick={() => run('set_mask', { layer: L, kind: 'paint' })}>绘制蒙版</button>
        <button title="创建初始全部隐藏的蒙版，绘制以显示内容"
          onClick={() => run('set_mask', { layer: L, kind: 'paint', hide_all: true })}>全部隐藏</button>
        {layer.type === 'image' && <button onClick={() => onAI('remove_background', { layer: L })}><Icon name="wand" size={13} /> 移除背景</button>}
      </div>}

      {layer.type === 'image' && <>
        <h5>AI</h5>
        <div className="row wrap">
          <button onClick={() => onAI('auto_enhance', { layer: L })}><Icon name="sparkle" size={13} /> 增强</button>
          <button onClick={() => onAI('upscale', { layer: L, factor: 2 })}>放大 2 倍</button>
          <button title="图生图：按提示词重新生成此图层" onClick={() => openDialog('edit', { layer: L })}>
            <Icon name="sparkle" size={13} /> 用 AI 编辑…</button>
          <button title="使用视觉模型描述此图层" onClick={() => openDialog('describe', { layer: L })}>
            <Icon name="eye2" size={13} /> 描述图片</button>
          <button onClick={() => run('rasterize_layer', { layer: L })} title="将效果和蒙版应用到像素">栅格化</button>
        </div>
      </>}
      {['text', 'shape', 'paint'].includes(layer.type) &&
        <button onClick={() => run('rasterize_layer', { layer: L })}>栅格化</button>}

      <h5>效果</h5>
      {(layer.effects || []).map((e, i) => (
        <EffectEditor key={i + e.type} layer={layer} effect={e} index={i} catalog={catalog} run={run} />))}
      <select value="" onChange={e => e.target.value && addFx(e.target.value)}>
        <option value="">+ 添加效果…</option>
        {['color', 'detail', 'stylize', 'style'].map(g => (
          <optgroup key={g} label={titleCase(g)}>
            {filters.filter(e => e.group === g).map(e => <option key={e.name} value={e.name}>{titleCase(e.name)}</option>)}
          </optgroup>))}
      </select>
    </div>
  )
}
