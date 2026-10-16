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
  ['move', 'move', 'Move / select layer (V)', 'v'],
  ['marquee', 'marquee', 'Rectangle selection (M)', 'm'],
  ['ellipse', 'lasso', 'Ellipse selection (L)', 'l'],
  ['crop', 'crop', 'Crop (C)', 'c'],
  ['brush', 'brush', 'Brush (B)', 'b'],
  ['eraser', 'eraser', 'Eraser (E)', 'e'],
  ['clone', 'stamp', 'Clone stamp (S) — Alt+click sets the source', 's'],
  ['spot', 'heal', 'Spot healing brush (J) — paint over a blemish', 'j'],
  ['text', 'text', 'Text (T)', 't'],
  ['shape', 'shape', 'Shape (U)', 'u'],
  ['hand', 'hand', 'Pan (H)', 'h'],
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
    if (s.seq !== undefined && s.seq < seqRef.current) return  // risposta in ritardo: si scarta
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
  // pennello sulla maschera: serve un livello selezionato che non sia di pittura
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

  // operazioni lente (IA): una barra di stato mentre lavorano
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
      path = window.prompt('Save document as (.json):', `${st.home}${sep}${project.name}.json`)
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
    setBusy('Importing…')
    try { apply(await api('/api/upload', { body: fd })) } catch (er) { setErr(er.message) }
    setBusy('')
  }

  const onTextAt = p => {
    const text = window.prompt('Text:')
    if (!text) return
    run('add_text', { text, x: p.x, y: p.y, size: opts.textSize, color: opts.textColor, font: opts.font, bold: opts.bold })
      .then(r => { if (r?.result?.id) { setSelectedId(r.result.id); setTool('move') } })
  }

  // scorciatoie da tastiera, fuori dai campi di testo
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
        <button onClick={() => setDialog('new')}>New</button>
        <button onClick={() => setDialog('open')}>Open</button>
        <button disabled={!project} onClick={save} title="Ctrl+S">Save</button>
        <span className="sep" />
        <button className="icon-btn" disabled={!project?.undo} title="Undo (Ctrl+Z)" onClick={() => run('undo', {})}><Icon name="undo" /></button>
        <button className="icon-btn" disabled={!project?.redo} title="Redo (Ctrl+Y)" onClick={() => run('redo', {})}><Icon name="redo" /></button>
        <span className="sep" />
        <button disabled={!project} onClick={() => setDialog('import')}><Icon name="image" size={15} /> Import</button>
        <button disabled={!project} onClick={() => setDialog('generate')}><Icon name="sparkle" size={15} /> Generate</button>
        <button disabled={!project} onClick={() => runAI('auto_enhance', {}, 'Enhancing…')}><Icon name="wand" size={15} /> Auto enhance</button>
        <button title="Online AI models: API keys and which model does what" onClick={() => setDialog('services')}><Icon name="gear" size={15} /> AI models</button>
        <span className="grow" />
        {project && <span className="docname">{project.name}{st.path ? '' : ' (unsaved)'}</span>}
        <button className="primary" disabled={!project} onClick={() => setDialog('export')}>Export</button>
      </header>

      <div className="optbar">
        {tool === 'brush' || tool === 'eraser' ? <>
          {tool === 'brush' && <label>Color <input type="color" value={opts.color} onChange={e => set('color', e.target.value)} /></label>}
          <label>Size <input type="range" min={1} max={400} value={opts.size} onChange={e => set('size', +e.target.value)} /> {opts.size}px</label>
          <label>Hardness <input type="range" min={0} max={1} step={0.05} value={opts.hardness} onChange={e => set('hardness', +e.target.value)} /></label>
          <label>Opacity <input type="range" min={0.05} max={1} step={0.05} value={opts.opacity} onChange={e => set('opacity', +e.target.value)} /></label>
          {selLayer && selLayer.type !== 'paint' && <label title="Paint on the selected layer's mask instead of pixels">
            Paint on <select value={opts.onMask ? 'mask' : 'layer'} onChange={e => set('onMask', e.target.value === 'mask')}>
              <option value="layer">new paint</option><option value="mask">mask of “{selLayer.name}”</option></select></label>}
          {maskMode && tool === 'brush' && <select value={opts.maskOp} onChange={e => set('maskOp', e.target.value)}>
            <option value="hide">hide (black)</option><option value="reveal">reveal (white)</option></select>}
          <span className="hint">{maskMode ? (tool === 'eraser' ? 'Eraser reveals the mask.' : 'Painting the mask: nothing is deleted.')
            : 'Paints on the selected paint layer, or a new one.'}</span>
        </> : tool === 'clone' || tool === 'spot' ? <>
          <label>Size <input type="range" min={2} max={400} value={opts.size} onChange={e => set('size', +e.target.value)} /> {opts.size}px</label>
          {tool === 'clone' && <>
            <label>Hardness <input type="range" min={0} max={1} step={0.05} value={opts.hardness} onChange={e => set('hardness', +e.target.value)} /></label>
            <label title="Healing brush: copy the texture, keep the light and color of where you paint">
              <input type="checkbox" checked={opts.heal} onChange={e => set('heal', e.target.checked)} /> Heal</label>
            <span className="hint">{clone ? `Source ${Math.round(clone.src.x)}, ${Math.round(clone.src.y)}${clone.offset ? ' · aligned' : ''}` : 'Alt+click to set the source.'}</span>
            {clone && <button onClick={() => setClone(null)}>Reset source</button>}
          </>}
          {tool === 'spot' && <span className="hint">Paint over a blemish or a small object: it is removed on a new layer.</span>}
        </> : tool === 'shape' ? <>
          <select value={opts.shape} onChange={e => set('shape', e.target.value)}><option>rect</option><option>ellipse</option></select>
          <label>Fill <input type="color" value={opts.fill} onChange={e => set('fill', e.target.value)} /></label>
          <label><input type="checkbox" checked={opts.strokeOn} onChange={e => set('strokeOn', e.target.checked)} /> Stroke</label>
          <input type="color" value={opts.stroke} onChange={e => set('stroke', e.target.value)} />
          <label>Radius <input type="number" value={opts.radius} onChange={e => set('radius', +e.target.value)} /></label>
        </> : tool === 'text' ? <>
          <label>Size <input type="number" value={opts.textSize} onChange={e => set('textSize', +e.target.value)} /></label>
          <label>Color <input type="color" value={opts.textColor} onChange={e => set('textColor', e.target.value)} /></label>
          <select value={opts.font} onChange={e => set('font', e.target.value)}>{['sans', 'serif', 'mono', 'display', 'handwriting'].map(f => <option key={f}>{f}</option>)}</select>
          <label><input type="checkbox" checked={opts.bold} onChange={e => set('bold', e.target.checked)} /> Bold</label>
          <span className="hint">Click on the image to place text.</span>
        </> : selection ? <>
          <span className="tag">{selection.kind} {selBox.join(' · ')}</span>
          {selection.crop && <button className="primary" onClick={() => { run('crop_canvas', { x: selBox[0], y: selBox[1], width: selBox[2], height: selBox[3] }); setSelection(null) }}>Crop to selection</button>}
          <button disabled={!selLayer} onClick={() => run('set_mask', { layer: selectedId, kind: selection.kind, box: selBox, feather: 8 })}>Mask layer</button>
          <button onClick={() => runAI('inpaint', { box: selBox, ellipse: selection.kind === 'ellipse' }, 'Removing…')}><Icon name="wand" size={14} /> Remove object</button>
          <button onClick={() => setDialog('fill')}><Icon name="sparkle" size={14} /> Generative fill</button>
          <button onClick={() => openDialog('edit', { box: selBox })} title="Image-to-image on this area"><Icon name="sparkle" size={14} /> Edit area with AI</button>
          <button onClick={() => openDialog('describe', { box: selBox })}>Describe</button>
          <button onClick={() => setSelection(null)}>Deselect</button>
        </> : <span className="hint">{tool === 'move' ? 'Click a layer to select it, drag to move, corners to resize.' :
          ['marquee', 'ellipse', 'crop'].includes(tool) ? 'Drag on the image to select an area.' : ''}</span>}
        {busy && <span className="busy">{busy}</span>}
      </div>

      <div className="main">
        <nav className="toolbar" aria-label="Tools">
          {TOOLS.map(([id, icon, title]) => (
            <button key={id} className={'tool-btn' + (tool === id ? ' on' : '')} title={title} onClick={() => setTool(id)}>
              <Icon name={icon} />
            </button>))}
        </nav>

        <section className="center">
          {!st ? <div className="welcome"><p className="hint">Loading…</p></div> : project
            ? <Canvas project={project} revision={st.revision} tool={tool} toolOpts={opts} selection={selection}
              setSelection={setSelection} selectedId={selectedId} setSelectedId={setSelectedId} run={run}
              zoom={zoom} setZoom={setZoom} setCursor={setCursor} onTextAt={onTextAt} onAI={runAI}
              maskMode={maskMode} clone={clone} setClone={setClone} />
            : <div className="welcome">
              <img src="./logo.svg" alt="" width={72} />
              <h1>pedit</h1>
              <p>A layered image editor that an AI can use for you — and you can always take the mouse back.</p>
              <div className="row">
                <button className="primary" onClick={() => setDialog('new')}>New document</button>
                <button onClick={() => setDialog('open')}>Open a photo or document</button>
              </div>
              <p className="hint">…or drop an image here.</p>
            </div>}
          {err && <div className="toast" onClick={() => setErr('')}>{err}</div>}
        </section>

        <aside className="side">
          <div className="tabs">
            {[['layers', 'Layers'], ['looks', 'Looks'], ['chat', 'Assistant']].map(([id, label]) => (
              <button key={id} className={tab === id ? 'on' : ''} onClick={() => setTab(id)}>{label}</button>))}
          </div>
          {tab === 'layers' && project && <>
            <Layers project={project} revision={st.revision} selectedId={selectedId} setSelectedId={setSelectedId} run={run} />
            <Properties project={project} layer={selLayer} catalog={st.effects || []} run={run} selection={selection} onAI={runAI}
              blendModes={st.blend_modes} openDialog={openDialog} maskMode={maskMode && ['brush', 'eraser'].includes(tool)} />
          </>}
          {tab === 'looks' && project && <div className="looks">
            <p className="hint">{selLayer ? `Applies to “${selLayer.name}”.` : 'Applies to the whole image as an adjustment layer.'}</p>
            <div className="look-grid">
              {(st.looks || []).map(l => (
                <button key={l.name} className="look" onClick={() => run('apply_look', { name: l.name, layer: selLayer && selLayer.type !== 'adjustment' ? selLayer.id : null })}>
                  <b>{titleCase(l.name)}</b><small>{l.desc}</small></button>))}
            </div>
            <h5>Adjustment layers</h5>
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
          <span>{zoom === 'fit' ? 'fit' : `${Math.round(zoom * 100)}%`}</span>
          <button className="link" onClick={() => setZoom('fit')}>fit</button>
          <button className="link" onClick={() => setZoom(1)}>100%</button>
          {cursor && <span>x {cursor.x} · y {cursor.y}</span>}
          <span className="grow" />
          {st.mcp && <span className="hint" title="Any MCP client can edit this document live">MCP: {st.mcp}</span>}
        </> : <span className="hint">pedit {st?.version}</span>}
      </footer>

      {dialog === 'new' && <NewDoc presets={st?.presets} home={st?.home} onCreate={create} onClose={() => setDialog(null)} />}
      {dialog === 'open' && <Browse mode="open" home={st?.home} onPick={openPath} onClose={() => setDialog(null)} />}
      {dialog === 'import' && <Browse mode="import" home={st?.home} onPick={importPaths} onClose={() => setDialog(null)} />}
      {dialog === 'export' && <Export project={project} home={st?.home} formats={st?.formats} onClose={() => setDialog(null)}
        onDone={r => { setDialog(null); setErr(`Exported ${r.width}×${r.height} → ${r.path}`) }} />}
      {dialog === 'generate' && <AIPrompt title="Generate image" task="generate" services={services}
        placeholder="a misty pine forest at dawn, cinematic light" hint="Lands on a new layer." onClose={() => setDialog(null)}
        onSettings={() => setDialog('services')}
        onRun={(p, backend) => { setDialog(null); runAI('generate_image', { prompt: p, backend }, 'Generating…') }} />}
      {dialog === 'fill' && selection && <AIPrompt title="Generative fill" task="inpaint" services={services} placeholder="a wooden bench"
        hint="Fills the selection; the result is a new layer with a soft mask." onClose={() => setDialog(null)}
        onSettings={() => setDialog('services')}
        onRun={(p, backend) => { setDialog(null); runAI('inpaint', { box: selBox, prompt: p, ellipse: selection.kind === 'ellipse', backend }, 'Filling…') }} />}
      {dialog === 'edit' && <AIPrompt title={dialogData?.layer ? 'Edit layer with AI' : 'Edit with AI'} task="edit" services={services} strength
        placeholder="make it a snowy winter evening" onClose={() => setDialog(null)} onSettings={() => setDialog('services')}
        hint="Image-to-image. Low strength keeps the picture, high reinvents it. The result is a new layer above; the original stays."
        onRun={(p, backend, strength) => { setDialog(null); runAI('edit_image', { prompt: p, backend, strength, ...dialogData }, 'Editing…') }} />}
      {dialog === 'expand' && project && <Expand project={project} services={services} onClose={() => setDialog(null)}
        onRun={b => { setDialog(null); runAI('expand_canvas', b, 'Expanding…') }} />}
      {dialog === 'describe' && <Describe target={dialogData || {}} services={services} onClose={() => setDialog(null)}
        onSettings={() => setDialog('services')} />}
      {dialog === 'services' && <AIServices services={services} onSaved={setServices} onClose={() => setDialog(null)} />}
      {question && <Question q={question} onAnswer={ok => { api('/api/answer', { body: { id: question.id, ok } }); setQuestion(null) }} />}
    </div>
  )
}
