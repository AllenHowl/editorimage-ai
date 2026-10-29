import { useCallback, useEffect, useRef, useState } from 'react'
import { api, connectEvents, op } from './api.js'
import Canvas, { clampZoom } from './Canvas.jsx'
import Chat from './Chat.jsx'
import { AIPrompt, AIServices, Browse, Describe, Expand, Export, NewDoc, Question } from './Dialogs.jsx'
import Icon from './Icons.jsx'
import Layers from './Layers.jsx'
import Properties from './Properties.jsx'
import { findLayer, titleCase } from './util.js'

const TOOLS = [
  ['move', 'move', '移动 / 选择图层 (V)', 'v'],
  ['marquee', 'marquee', '矩形选区 (M)', 'm'],
  ['ellipse', 'lasso', '椭圆选区 (L)', 'l'],
  ['crop', 'crop', '裁剪 (C)', 'c'],
  ['brush', 'brush', '画笔 (B)', 'b'],
  ['eraser', 'eraser', '橡皮擦 (E)', 'e'],
  ['clone', 'stamp', '仿制图章 (S) — 按住 Alt 单击设置取样点', 's'],
  ['spot', 'heal', '污点修复画笔 (J) — 涂抹瑕疵以修复', 'j'],
  ['text', 'text', '文字 (T)', 't'],
  ['shape', 'shape', '形状 (U)', 'u'],
  ['hand', 'hand', '平移 (H)', 'h'],
]

export default function App() {
  const [st, setSt] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')
  const [tool, setTool] = useState('move')
  const [opts, setOpts] = useState({ color: '#ff3366', size: 24, hardness: 0.8, opacity: 1, shape: 'rect', fill: '#5b8cff',
    stroke: '#ffffff', strokeOn: false, strokeWidth: 4, radius: 0, textSize: 96, textColor: '#ffffff', font: 'sans', bold: true,
    onMask: false, maskOp: 'hide', heal: false })
  const [clone, setClone] = useState(null)
  const [services, setServices] = useState(null)
  const [dialogData, setDialogData] = useState(null)
  const [selection, setSelection] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [zoom, setZoom] = useState('fit')
  const [cursor, setCursor] = useState(null)
  const [tab, setTab] = useState('layers')
  const [dialog, setDialog] = useState(null)
  const [question, setQuestion] = useState(null)
  const [llm, setLlm] = useState(null)
  const seqRef = useRef(0)

  const apply = useCallback(s => {
    if (!s) return
    if (s.seq !== undefined && s.seq < seqRef.current) return  // 丢弃延迟到达的旧响应。
    if (s.seq !== undefined) seqRef.current = s.seq
    setSt(prev => ({ ...prev, ...s }))
  }, [])

  useEffect(() => {
    api('/api/state').then(s => { apply(s); setLlm(s.chat) }).catch(e => setErr(e.message))
    api('/api/ai/services').then(setServices).catch(() => {})
    return connectEvents(ev => {
      if (ev.type === 'project') api('/api/project').then(apply).catch(() => {})
      if (ev.type === 'question') setQuestion(ev)
    })
  }, [apply])

  const project = st?.project
  const layers = project?.layers || []
  const selLayer = findLayer(layers, selectedId)
  const openDialog = (name, data = null) => { setDialogData(data); setDialog(name) }
  // 在蒙版上绘制时，需要选中一个非绘画图层。
  const maskMode = !!(opts.onMask && selLayer && selLayer.type !== 'paint')
  useEffect(() => { if (selectedId && !selLayer) setSelectedId(null) }, [project])

  const run = useCallback(async (name, body) => {
    setErr('')
    try {
      const r = await op(name, body)
      apply(r)
      return r
    } catch (e) { setErr(e.message); return null }
  }, [apply])

  // 执行较慢的 AI 操作时显示状态提示。
  const runAI = useCallback(async (name, body, label) => {
    setBusy(label || titleCase(name) + '…')
    const r = await run(name, body)
    setBusy('')
    if (r?.result?.id && name !== 'auto_enhance') setSelectedId(r.result.id)
    if (name === 'expand_canvas' && r?.result?.layer) setSelectedId(null)
    return r
  }, [run])

  const create = async body => {
    try { apply(await api('/api/project/create', { body })); setDialog(null); setSelection(null); setSelectedId(null) } catch (e) { setErr(e.message) }
  }
  const openPath = async paths => {
    try { apply(await api('/api/project/open', { body: { path: paths[0] } })); setDialog(null); setSelection(null); setSelectedId(null) } catch (e) { setErr(e.message) }
  }
  const importPaths = async paths => {
    try { apply(await api('/api/import', { body: { paths, add: true } })); setDialog(null) } catch (e) { setErr(e.message) }
  }
  const save = async () => {
    if (!project) return
    let path = st.path
    if (!path) {
      const sep = st.home?.includes('\\') ? '\\' : '/'
      path = window.prompt('文档另存为 (.json)：', `${st.home}${sep}${project.name}.json`)
      if (!path) return
    }
    try { apply(await api('/api/project/save', { body: { path } })) } catch (e) { setErr(e.message) }
  }

  const onDrop = async e => {
    e.preventDefault()
    const files = [...(e.dataTransfer?.files || [])].filter(f => f.type.startsWith('image/'))
    if (!files.length) return
    if (!project) {
      await create({ name: files[0].name.replace(/\.[^.]+$/, ''), width: 1920, height: 1080 })
    }
    const fd = new FormData()
    files.forEach(f => fd.append('files', f))
    fd.append('add', '1')
    setBusy('正在导入…')
    try { apply(await api('/api/upload', { body: fd })) } catch (er) { setErr(er.message) }
    setBusy('')
  }

  const onTextAt = p => {
    const text = window.prompt('文字：')
    if (!text) return
    run('add_text', { text, x: p.x, y: p.y, size: opts.textSize, color: opts.textColor, font: opts.font, bold: opts.bold })
      .then(r => { if (r?.result?.id) { setSelectedId(r.result.id); setTool('move') } })
  }

  // 焦点不在输入框时处理键盘快捷键。
  useEffect(() => {
    const onKey = e => {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return
      const k = e.key.toLowerCase()
      if ((e.ctrlKey || e.metaKey) && k === 'z') { e.preventDefault(); run(e.shiftKey ? 'redo' : 'undo', {}) }
      else if ((e.ctrlKey || e.metaKey) && k === 'y') { e.preventDefault(); run('redo', {}) }
      else if ((e.ctrlKey || e.metaKey) && k === 's') { e.preventDefault(); save() }
      else if ((e.ctrlKey || e.metaKey) && k === 'd') { e.preventDefault(); setSelection(null) }
      else if ((e.ctrlKey || e.metaKey) && k === 'j' && selectedId) { e.preventDefault(); run('duplicate_layer', { layer: selectedId }) }
      else if (e.key === 'Escape') setSelection(null)
      else if ((e.key === 'Delete' || e.key === 'Backspace') && selectedId) run('remove_layer', { layer: selectedId })
      else if (e.key === '+' || e.key === '=') setZoom(z => clampZoom((z === 'fit' ? 1 : z) * 1.25))
      else if (e.key === '-') setZoom(z => clampZoom((z === 'fit' ? 1 : z) / 1.25))
      else if (e.key === '0') setZoom('fit')
      else if (e.key === '1') setZoom(1)
      else if (!e.ctrlKey && !e.metaKey && !e.altKey) {
        const t = TOOLS.find(x => x[3] === k)
        if (t) setTool(t[0])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const set = (k, v) => setOpts(o => ({ ...o, [k]: v }))
  const selBox = selection?.box

  return (
    <div className="app" onDragOver={e => e.preventDefault()} onDrop={onDrop}>
      <header className="topbar">
        <img src="./logo.svg" alt="" className="logo" /><b className="brand">pedit</b>
        <button onClick={() => setDialog('new')}>新建</button>
        <button onClick={() => setDialog('open')}>打开</button>
        <button disabled={!project} onClick={save} title="Ctrl+S">保存</button>
        <span className="sep" />
        <button className="icon-btn" disabled={!project?.undo} title="撤销 (Ctrl+Z)" onClick={() => run('undo', {})}><Icon name="undo" /></button>
        <button className="icon-btn" disabled={!project?.redo} title="重做 (Ctrl+Y)" onClick={() => run('redo', {})}><Icon name="redo" /></button>
        <span className="sep" />
        <button disabled={!project} onClick={() => setDialog('import')}><Icon name="image" size={15} /> 导入</button>
        <button disabled={!project} onClick={() => setDialog('generate')}><Icon name="sparkle" size={15} /> 生成</button>
        <button disabled={!project} onClick={() => runAI('auto_enhance', {}, '正在增强…')}><Icon name="wand" size={15} /> 自动增强</button>
        <button title="在线 AI 模型：API 密钥及各项任务的模型设置" onClick={() => setDialog('services')}><Icon name="gear" size={15} /> AI 模型</button>
        <span className="grow" />
        {project && <span className="docname">{project.name}{st.path ? '' : '（未保存）'}</span>}
        <button className="primary" disabled={!project} onClick={() => setDialog('export')}>导出</button>
      </header>

      <div className="optbar">
        {tool === 'brush' || tool === 'eraser' ? <>
          {tool === 'brush' && <label>颜色 <input type="color" value={opts.color} onChange={e => set('color', e.target.value)} /></label>}
          <label>大小 <input type="range" min={1} max={400} value={opts.size} onChange={e => set('size', +e.target.value)} /> {opts.size}px</label>
          <label>硬度 <input type="range" min={0} max={1} step={0.05} value={opts.hardness} onChange={e => set('hardness', +e.target.value)} /></label>
          <label>不透明度 <input type="range" min={0.05} max={1} step={0.05} value={opts.opacity} onChange={e => set('opacity', +e.target.value)} /></label>
          {selLayer && selLayer.type !== 'paint' && <label title="在所选图层的蒙版上绘制">
            绘制到 <select value={opts.onMask ? 'mask' : 'layer'} onChange={e => set('onMask', e.target.value === 'mask')}>
              <option value="layer">绘画图层</option><option value="mask">“{selLayer.name}”的蒙版</option></select></label>}
          {maskMode && tool === 'brush' && <select value={opts.maskOp} onChange={e => set('maskOp', e.target.value)}>
            <option value="hide">隐藏（黑色）</option><option value="reveal">显示（白色）</option></select>}
          <span className="hint">{maskMode ? (tool === 'eraser' ? '橡皮擦会显示蒙版遮挡的区域。' : '正在绘制蒙版：不会删除原始内容。')
            : '在所选绘画图层上绘制；未选中时新建图层。'}</span>
        </> : tool === 'clone' || tool === 'spot' ? <>
          <label>大小 <input type="range" min={2} max={400} value={opts.size} onChange={e => set('size', +e.target.value)} /> {opts.size}px</label>
          {tool === 'clone' && <>
            <label>硬度 <input type="range" min={0} max={1} step={0.05} value={opts.hardness} onChange={e => set('hardness', +e.target.value)} /></label>
            <label title="修复画笔：复制纹理，保留目标区域的光线和颜色">
              <input type="checkbox" checked={opts.heal} onChange={e => set('heal', e.target.checked)} /> 修复</label>
            <span className="hint">{clone ? `取样点 ${Math.round(clone.src.x)}, ${Math.round(clone.src.y)}${clone.offset ? ' · 已对齐' : ''}` : '按住 Alt 单击设置取样点。'}</span>
            {clone && <button onClick={() => setClone(null)}>重置取样点</button>}
          </>}
          {tool === 'spot' && <span className="hint">涂抹瑕疵或小物体，在新图层中生成修复结果。</span>}
        </> : tool === 'shape' ? <>
          <select value={opts.shape} onChange={e => set('shape', e.target.value)}><option value="rect">矩形</option><option value="ellipse">椭圆</option></select>
          <label>填充 <input type="color" value={opts.fill} onChange={e => set('fill', e.target.value)} /></label>
          <label><input type="checkbox" checked={opts.strokeOn} onChange={e => set('strokeOn', e.target.checked)} /> 描边</label>
          <input type="color" value={opts.stroke} onChange={e => set('stroke', e.target.value)} />
          <label>圆角 <input type="number" value={opts.radius} onChange={e => set('radius', +e.target.value)} /></label>
        </> : tool === 'text' ? <>
          <label>大小 <input type="number" value={opts.textSize} onChange={e => set('textSize', +e.target.value)} /></label>
          <label>颜色 <input type="color" value={opts.textColor} onChange={e => set('textColor', e.target.value)} /></label>
          <select value={opts.font} onChange={e => set('font', e.target.value)}>{['sans', 'serif', 'mono', 'display', 'handwriting'].map(f => <option key={f} value={f}>{titleCase(f)}</option>)}</select>
          <label><input type="checkbox" checked={opts.bold} onChange={e => set('bold', e.target.checked)} /> 粗体</label>
          <span className="hint">单击画布放置文字。</span>
        </> : selection ? <>
          <span className="tag">{titleCase(selection.kind)} {selBox.join(' · ')}</span>
          {selection.crop && <button className="primary" onClick={() => { run('crop_canvas', { x: selBox[0], y: selBox[1], width: selBox[2], height: selBox[3] }); setSelection(null) }}>裁剪到选区</button>}
          <button disabled={!selLayer} onClick={() => run('set_mask', { layer: selectedId, kind: selection.kind, box: selBox, feather: 8 })}>为图层添加蒙版</button>
          <button onClick={() => runAI('inpaint', { box: selBox, ellipse: selection.kind === 'ellipse' }, '正在移除…')}><Icon name="wand" size={14} /> 移除物体</button>
          <button onClick={() => setDialog('fill')}><Icon name="sparkle" size={14} /> 生成式填充</button>
          <button onClick={() => openDialog('edit', { box: selBox })} title="对该区域进行图生图编辑"><Icon name="sparkle" size={14} /> 用 AI 编辑选区</button>
          <button onClick={() => openDialog('describe', { box: selBox })}>描述图片</button>
          <button onClick={() => setSelection(null)}>取消选择</button>
        </> : <span className="hint">{tool === 'move' ? '单击选择图层，拖动以移动，拖动边角调整大小。' :
          ['marquee', 'ellipse', 'crop'].includes(tool) ? '在画布上拖动以选择区域。' : ''}</span>}
        {busy && <span className="busy">{busy}</span>}
      </div>

      <div className="main">
        <nav className="toolbar" aria-label="工具">
          {TOOLS.map(([id, icon, title]) => (
            <button key={id} className={'tool-btn' + (tool === id ? ' on' : '')} title={title} onClick={() => setTool(id)}>
              <Icon name={icon} />
            </button>))}
        </nav>

        <section className="center">
          {!st ? <div className="welcome"><p className="hint">正在加载…</p></div> : project
            ? <Canvas project={project} revision={st.revision} tool={tool} toolOpts={opts} selection={selection}
              setSelection={setSelection} selectedId={selectedId} setSelectedId={setSelectedId} run={run}
              zoom={zoom} setZoom={setZoom} setCursor={setCursor} onTextAt={onTextAt} onAI={runAI}
              maskMode={maskMode} clone={clone} setClone={setClone} />
            : <div className="welcome">
              <img src="./logo.svg" alt="" width={72} />
              <h1>pedit</h1>
              <p>支持 AI 指令的图层式图片编辑器，随时都能用鼠标接手编辑。</p>
              <div className="row">
                <button className="primary" onClick={() => setDialog('new')}>新建文档</button>
                <button onClick={() => setDialog('open')}>打开照片或文档</button>
              </div>
              <p className="hint">也可以将图片拖到这里。</p>
            </div>}
          {err && <div className="toast" onClick={() => setErr('')}>{err}</div>}
        </section>

        <aside className="side">
          <div className="tabs">
            {[['layers', '图层'], ['looks', '风格'], ['chat', '助手']].map(([id, label]) => (
              <button key={id} className={tab === id ? 'on' : ''} onClick={() => setTab(id)}>{label}</button>))}
          </div>
          {tab === 'layers' && project && <>
            <Layers project={project} revision={st.revision} selectedId={selectedId} setSelectedId={setSelectedId} run={run} />
            <Properties project={project} layer={selLayer} catalog={st.effects || []} run={run} selection={selection} onAI={runAI}
              blendModes={st.blend_modes} openDialog={openDialog} maskMode={maskMode && ['brush', 'eraser'].includes(tool)} />
          </>}
          {tab === 'looks' && project && <div className="looks">
            <p className="hint">{selLayer ? `应用于“${selLayer.name}”。` : '作为调整图层应用于整张图片。'}</p>
            <div className="look-grid">
              {(st.looks || []).map(l => (
                <button key={l.name} className="look" onClick={() => run('apply_look', { name: l.name, layer: selLayer && selLayer.type !== 'adjustment' ? selLayer.id : null })}>
                  <b>{titleCase(l.name)}</b><small>{l.desc}</small></button>))}
            </div>
            <h5>调整图层</h5>
            <div className="row wrap">
              {(st.effects || []).filter(e => e.kind === 'filter' && e.group === 'color' && e.name !== 'color_match').map(e => (
                <button key={e.name} onClick={() => run('add_adjustment', { type: e.name }).then(r => r?.result?.id && setSelectedId(r.result.id))}>{titleCase(e.name)}</button>))}
            </div>
          </div>}
          {tab === 'chat' && <Chat llm={llm} setLlm={setLlm} selection={selection} selectedLayer={selLayer} />}
        </aside>
      </div>

      <footer className="status">
        {project ? <>
          <span>{project.width}×{project.height}px</span>
          <span>{zoom === 'fit' ? '适合窗口' : `${Math.round(zoom * 100)}%`}</span>
          <button className="link" onClick={() => setZoom('fit')}>适合窗口</button>
          <button className="link" onClick={() => setZoom(1)}>100%</button>
          {cursor && <span>x {cursor.x} · y {cursor.y}</span>}
          <span className="grow" />
          {st.mcp && <span className="hint" title="MCP 客户端可实时编辑当前文档">MCP: {st.mcp}</span>}
        </> : <><span className="hint">pedit {st?.version}</span><span className="grow" /></>}
        <a className="credit" href="https://metiu.xyz/" target="_blank" rel="noreferrer">由 metiu.xyz 制作</a>
      </footer>

      {dialog === 'new' && <NewDoc presets={st?.presets} home={st?.home} onCreate={create} onClose={() => setDialog(null)} />}
      {dialog === 'open' && <Browse mode="open" home={st?.home} onPick={openPath} onClose={() => setDialog(null)} />}
      {dialog === 'import' && <Browse mode="import" home={st?.home} onPick={importPaths} onClose={() => setDialog(null)} />}
      {dialog === 'export' && <Export project={project} home={st?.home} formats={st?.formats} onClose={() => setDialog(null)}
        onDone={r => { setDialog(null); setErr(`已导出 ${r.width}×${r.height} → ${r.path}`) }} />}
      {dialog === 'generate' && <AIPrompt title="生成图片" task="generate" services={services}
        placeholder="晨雾中的松林，电影般的光线" hint="生成结果将添加为新图层。" onClose={() => setDialog(null)}
        onSettings={() => setDialog('services')}
        onRun={(p, backend) => { setDialog(null); runAI('generate_image', { prompt: p, backend }, '正在生成…') }} />}
      {dialog === 'fill' && selection && <AIPrompt title="生成式填充" task="inpaint" services={services} placeholder="一张木质长椅"
        hint="填充选区，结果保存为带柔边蒙版的新图层。" onClose={() => setDialog(null)}
        onSettings={() => setDialog('services')}
        onRun={(p, backend) => { setDialog(null); runAI('inpaint', { box: selBox, prompt: p, ellipse: selection.kind === 'ellipse', backend }, '正在填充…') }} />}
      {dialog === 'edit' && <AIPrompt title={dialogData?.layer ? '用 AI 编辑图层' : '用 AI 编辑'} task="edit" services={services} strength
        placeholder="变成下雪的冬日傍晚" onClose={() => setDialog(null)} onSettings={() => setDialog('services')}
        hint="图生图：强度越低越接近原图，越高改动越大。结果将添加到上方的新图层，保留原图。"
        onRun={(p, backend, strength) => { setDialog(null); runAI('edit_image', { prompt: p, backend, strength, ...dialogData }, '正在编辑…') }} />}
      {dialog === 'expand' && project && <Expand project={project} services={services} onClose={() => setDialog(null)}
        onRun={b => { setDialog(null); runAI('expand_canvas', b, '正在扩展…') }} />}
      {dialog === 'describe' && <Describe target={dialogData || {}} services={services} onClose={() => setDialog(null)}
        onSettings={() => setDialog('services')} />}
      {dialog === 'services' && <AIServices services={services} onSaved={setServices} onClose={() => setDialog(null)} />}
      {question && <Question q={question} onAnswer={ok => { api('/api/answer', { body: { id: question.id, ok } }); setQuestion(null) }} />}
    </div>
  )
}
