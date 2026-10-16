// Finestre: nuovo documento, apri/importa (sfoglia il disco), esporta, genera.
import { useEffect, useState } from 'react'
import { api } from './api.js'

export function Modal({ title, onClose, children }) {
  return (
    <div className="modal-bg" onMouseDown={e => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label={title}>
        <div className="modal-head"><b>{title}</b><button className="icon-btn" onClick={onClose}>✕</button></div>
        {children}
      </div>
    </div>
  )
}

export function NewDoc({ presets, home, onCreate, onClose }) {
  const [name, setName] = useState('untitled')
  const [preset, setPreset] = useState('1080p')
  const [w, setW] = useState(1920)
  const [h, setH] = useState(1080)
  const [bg, setBg] = useState('#ffffff')
  const [transparent, setTransparent] = useState(false)
  const [path, setPath] = useState('')
  useEffect(() => { if (presets?.[preset]) { setW(presets[preset][0]); setH(presets[preset][1]) } }, [preset])
  const sep = home?.includes('\\') ? '\\' : '/'
  return (
    <Modal title="New document" onClose={onClose}>
      <label className="numfield wide"><span>Name</span><input value={name} onChange={e => setName(e.target.value)} /></label>
      <label className="numfield wide"><span>Preset</span>
        <select value={preset} onChange={e => setPreset(e.target.value)}>
          {Object.entries(presets || {}).map(([k, v]) => <option key={k} value={k}>{k} — {v[0]}×{v[1]}</option>)}
          <option value="custom">custom</option>
        </select></label>
      <div className="grid2">
        <label className="numfield"><span>Width</span><input type="number" value={w} onChange={e => { setW(+e.target.value); setPreset('custom') }} /></label>
        <label className="numfield"><span>Height</span><input type="number" value={h} onChange={e => { setH(+e.target.value); setPreset('custom') }} /></label>
        <label className="numfield"><span>Background</span><input type="color" value={bg} disabled={transparent} onChange={e => setBg(e.target.value)} /></label>
        <label className="check"><input type="checkbox" checked={transparent} onChange={e => setTransparent(e.target.checked)} /> transparent</label>
      </div>
      <label className="numfield wide"><span>Save as</span>
        <input value={path} placeholder={`${home}${sep}${name}.json (optional, enables autosave)`} onChange={e => setPath(e.target.value)} /></label>
      <div className="modal-foot">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={() => onCreate({ name, width: w, height: h, background: transparent ? 'transparent' : bg, path: path || null })}>Create</button>
      </div>
    </Modal>
  )
}

// mode: "open" (documento o foto) | "import" (immagini come livelli)
export function Browse({ mode, home, onPick, onClose }) {
  const [dir, setDir] = useState(null)
  const [err, setErr] = useState('')
  const [chosen, setChosen] = useState([])
  const go = async p => {
    try { setDir(await api('/api/browse' + (p ? `?path=${encodeURIComponent(p)}` : ''))); setErr('') } catch (e) { setErr(e.message) }
  }
  useEffect(() => { go(home) }, [])
  const click = f => {
    if (f.dir) return go(f.path)
    if (mode === 'open') return onPick([f.path])
    if (f.kind === 'image') setChosen(c => c.includes(f.path) ? c.filter(x => x !== f.path) : [...c, f.path])
  }
  return (
    <Modal title={mode === 'open' ? 'Open document or photo' : 'Import images as layers'} onClose={onClose}>
      <div className="row">
        <input className="grow" value={dir?.path || ''} onChange={e => setDir(d => ({ ...d, path: e.target.value }))}
          onKeyDown={e => e.key === 'Enter' && go(e.target.value)} />
        <button onClick={() => go(dir?.parent)}>Up</button>
        {dir?.roots?.map(r => <button key={r} onClick={() => go(r)}>{r}</button>)}
      </div>
      {err && <p className="warn">{err}</p>}
      <div className="files">
        {dir?.entries?.map(f => (
          <div key={f.path} className={'file' + (chosen.includes(f.path) ? ' sel' : '') + (f.dir ? ' dir' : '')}
            onClick={() => click(f)} onDoubleClick={() => !f.dir && onPick([f.path])}>
            <span>{f.dir ? '📁' : f.kind === 'doc' ? '📄' : '🖼'}</span> {f.name}
          </div>))}
        {dir && !dir.entries.length && <p className="hint">No images or documents here.</p>}
      </div>
      {mode === 'import' && <div className="modal-foot">
        <span className="hint">{chosen.length} selected</span>
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!chosen.length} onClick={() => onPick(chosen)}>Import</button>
      </div>}
    </Modal>
  )
}

export function Export({ project, home, formats, onClose, onDone }) {
  const sep = home?.includes('\\') ? '\\' : '/'
  const [path, setPath] = useState(`${home}${sep}${project?.name || 'image'}.png`)
  const [quality, setQuality] = useState(92)
  const [scale, setScale] = useState(1)
  const [transparent, setTransparent] = useState(project?.background === 'transparent')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const go = async () => {
    setBusy(true); setErr('')
    try { onDone(await api('/api/export', { body: { path, quality, scale, transparent } })) } catch (e) { setErr(e.message) }
    setBusy(false)
  }
  return (
    <Modal title="Export image" onClose={onClose}>
      <label className="numfield wide"><span>File</span><input value={path} onChange={e => setPath(e.target.value)} /></label>
      <p className="hint">The extension picks the format: {(formats || ['.png', '.jpg', '.webp', '.tif']).join(' ')}</p>
      <div className="grid2">
        <label className="numfield"><span>Quality</span><input type="number" min={1} max={100} value={quality} onChange={e => setQuality(+e.target.value)} /></label>
        <label className="numfield"><span>Scale</span><input type="number" min={0.05} max={4} step={0.05} value={scale} onChange={e => setScale(+e.target.value)} /></label>
        <label className="check"><input type="checkbox" checked={transparent} onChange={e => setTransparent(e.target.checked)} /> keep transparency</label>
        <span className="hint">{Math.round(project.width * scale)}×{Math.round(project.height * scale)} px</span>
      </div>
      {err && <p className="warn">{err}</p>}
      <div className="modal-foot">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={busy} onClick={go}>{busy ? 'Exporting…' : 'Export'}</button>
      </div>
    </Modal>
  )
}

export function PromptDialog({ title, placeholder, hint, onRun, onClose }) {
  const [text, setText] = useState('')
  return (
    <Modal title={title} onClose={onClose}>
      <textarea autoFocus rows={3} value={text} placeholder={placeholder} onChange={e => setText(e.target.value)} />
      {hint && <p className="hint">{hint}</p>}
      <div className="modal-foot">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!text.trim()} onClick={() => onRun(text.trim())}>Run</button>
      </div>
    </Modal>
  )
}

// motori per un compito: auto, quelli locali che lo sanno fare, i servizi pronti
const LOCALI = { generate: ['comfyui'], edit: ['comfyui'], inpaint: ['comfyui'], describe: [],
  upscale: ['comfyui', 'lanczos'], cutout: ['local'] }
const NOMI_LOCALI = { comfyui: 'ComfyUI (local)', lanczos: 'Lanczos (local)', local: 'rembg (local)' }

export function backendChoices(services, task) {
  const pronti = (services?.providers || []).filter(p => p.pronto && p.fa.includes(task))
  const def = services?.providers?.find(p => p.id === services?.default)
  const auto = def && def.fa.includes(task) ? `Auto (${def.nome})`
    : task === 'describe' ? 'Auto (first service with a key)' : 'Auto (local)'
  return [['auto', auto], ...(LOCALI[task] || []).map(l => [l, NOMI_LOCALI[l] || l]),
    ...pronti.map(p => [p.id, `${p.nome} · ${p.modelli[task] || ''}`])]
}

function Backend({ services, task, value, onChange }) {
  return (
    <label className="numfield wide"><span>Model</span>
      <select value={value} onChange={e => onChange(e.target.value)}>
        {backendChoices(services, task).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select></label>
  )
}

const senzaServizi = (services, task) => !(services?.providers || []).some(p => p.pronto && p.fa.includes(task))

// prompt + motore (+ forza per image-to-image): generare, riempire, modificare
export function AIPrompt({ title, placeholder, hint, task, services, strength, onRun, onClose, onSettings }) {
  const [text, setText] = useState('')
  const [backend, setBackend] = useState('auto')
  const [forza, setForza] = useState(0.6)
  const via = () => text.trim() && onRun(text.trim(), backend, forza)
  return (
    <Modal title={title} onClose={onClose}>
      <textarea autoFocus rows={3} value={text} placeholder={placeholder} onChange={e => setText(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) via() }} />
      <Backend services={services} task={task} value={backend} onChange={setBackend} />
      {strength && <label className="slider"><span>Strength</span>
        <input type="range" min={0.1} max={1} step={0.05} value={forza} onChange={e => setForza(+e.target.value)} />
        <span className="num">{forza.toFixed(2)}</span></label>}
      {hint && <p className="hint">{hint}</p>}
      {senzaServizi(services, task) && <p className="hint">No online model set up yet. <button className="link" onClick={onSettings}>Add an API key…</button></p>}
      <div className="modal-foot">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!text.trim()} onClick={via}>Run</button>
      </div>
    </Modal>
  )
}

export function Expand({ project, services, onRun, onClose }) {
  const [m, setM] = useState({ left: 0, top: 0, right: 0, bottom: 0 })
  const [prompt, setPrompt] = useState('')
  const [backend, setBackend] = useState('auto')
  const lato = Math.max(project.width, project.height)
  const W = project.width + m.left + m.right, H = project.height + m.top + m.bottom
  return (
    <Modal title="Expand canvas (generative)" onClose={onClose}>
      <div className="row wrap">
        {[10, 25, 50].map(p => { const v = Math.round(lato * p / 100); return (
          <button key={p} onClick={() => setM({ left: v, top: v, right: v, bottom: v })}>+{p}% all sides</button>) })}
        <button onClick={() => { const v = Math.round(project.width / 4); setM({ left: v, top: 0, right: v, bottom: 0 }) }}>Wider</button>
        <button onClick={() => { const v = Math.round(project.height / 4); setM({ left: 0, top: v, right: 0, bottom: v }) }}>Taller</button>
      </div>
      <div className="grid2">
        {['left', 'right', 'top', 'bottom'].map(k => (
          <label key={k} className="numfield"><span>{k}</span>
            <input type="number" min={0} value={m[k]} onChange={e => setM({ ...m, [k]: Math.max(0, +e.target.value) })} /></label>))}
      </div>
      <p className="hint">New size {W}×{H}px. The new borders go on a new top layer masked to them: the original layers stay untouched.</p>
      <textarea rows={2} value={prompt} placeholder="What is outside the frame? (optional)" onChange={e => setPrompt(e.target.value)} />
      <Backend services={services} task="inpaint" value={backend} onChange={setBackend} />
      <p className="hint">Auto (local) without a prompt fills the borders with the classic remover: fine for sky, walls, plain backgrounds.
        For real new content use a prompt with ComfyUI or an online model.</p>
      <div className="modal-foot">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={m.left + m.top + m.right + m.bottom === 0}
          onClick={() => onRun({ ...m, prompt: prompt.trim() || null, backend })}>Expand</button>
      </div>
    </Modal>
  )
}

export function Describe({ target, services, onClose, onSettings }) {
  const [question, setQuestion] = useState('')
  const [backend, setBackend] = useState('auto')
  const [out, setOut] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const go = async () => {
    setBusy(true); setErr('')
    try {
      const r = await api('/api/op/describe_image', { body: { ...target, question: question.trim() || null, backend } })
      setOut(r.result)
    } catch (e) { setErr(e.message) }
    setBusy(false)
  }
  return (
    <Modal title={target?.layer ? 'Describe layer' : 'Describe image'} onClose={onClose}>
      <textarea rows={2} value={question} placeholder="Ask something about the image (optional)" onChange={e => setQuestion(e.target.value)} />
      <Backend services={services} task="describe" value={backend} onChange={setBackend} />
      {senzaServizi(services, 'describe') &&
        <p className="hint">Describing needs a vision model. <button className="link" onClick={onSettings}>Add an API key…</button></p>}
      {err && <p className="warn">{err}</p>}
      {out && <div className="described"><p>{out.text}</p>
        <div className="row"><small className="hint">{out.backend}</small><span className="grow" />
          <button onClick={() => navigator.clipboard?.writeText(out.text)}>Copy</button></div></div>}
      <div className="modal-foot">
        <button onClick={onClose}>Close</button>
        <button className="primary" disabled={busy} onClick={go}>{busy ? 'Looking…' : out ? 'Ask again' : 'Describe'}</button>
      </div>
    </Modal>
  )
}

const COMPITI = { generate: 'Text to image', edit: 'Image to image', inpaint: 'Fill (inpaint)', describe: 'Image to text',
  upscale: 'Upscale', cutout: 'Background removal' }

// modelli in rete: chiave, modello per compito, servizio predefinito di Auto
export function AIServices({ services, onSaved, onClose }) {
  const [pid, setPid] = useState(services?.default || 'openai')
  const p = services?.providers?.find(x => x.id === pid)
  const [key, setKey] = useState('')
  const [models, setModels] = useState({})
  const [url, setUrl] = useState('')
  const [err, setErr] = useState('')
  const [ok, setOk] = useState('')
  useEffect(() => { setKey(''); setModels(p?.modelli || {}); setUrl(p?.indirizzo || ''); setErr(''); setOk('') }, [pid])
  const save = async (extra = {}) => {
    setErr(''); setOk('')
    try {
      const r = await api('/api/ai/services', { body: { provider: pid, key: key || null, models,
        url: p?.indirizzo_modificabile ? url : null, ...extra } })
      onSaved(r); setKey(''); setOk('Saved.')
    } catch (e) { setErr(e.message) }
  }
  return (
    <Modal title="AI models" onClose={onClose}>
      <p className="hint">Local AI (rembg, OpenCV, ComfyUI) works without keys. Add a key to use online models too: the provider
        bills your account. Keys stay on this computer (~/.pedit/ai_api.json), never in documents.</p>
      <label className="numfield wide"><span>Service</span>
        <select value={pid} onChange={e => setPid(e.target.value)}>
          {services?.providers?.map(x => <option key={x.id} value={x.id}>
            {x.nome}{x.pronto ? ' ✓' : ''}{services.default === x.id ? ' (default)' : ''}</option>)}
        </select></label>
      {p?.nota && <p className="hint">{p.nota}</p>}
      {p?.vuole_chiave && <label className="numfield wide"><span>API key</span>
        <input type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)}
          placeholder={p.chiave ? `${p.chiave}${p.chiave_da_altrove ? ' (from assistant or env)' : ''}` : 'paste the key'} /></label>}
      {p?.indirizzo_modificabile && <label className="numfield wide"><span>Address</span>
        <input value={url} placeholder="https://…/v1" onChange={e => setUrl(e.target.value)} /></label>}
      {p?.fa.map(c => (
        <label key={c} className="numfield wide"><span>{COMPITI[c] || c}</span>
          <input list={`m-${pid}`} value={models[c] ?? ''} onChange={e => setModels({ ...models, [c]: e.target.value })} /></label>))}
      <datalist id={`m-${pid}`}>{p?.suggeriti?.map(m => <option key={m} value={m} />)}</datalist>
      {p && !p.pronto && p.motivo && <p className="warn">{p.motivo}</p>}
      {err && <p className="warn">{err}</p>}
      {ok && <p className="hint">{ok}</p>}
      <div className="modal-foot">
        {p?.chiave && !p.chiave_da_altrove && <button onClick={() => save({ key: '' })}>Forget key</button>}
        <label className="check" title="Auto uses this service to generate, fill and edit">
          <input type="checkbox" checked={services?.default === pid} onChange={e => save({ default: e.target.checked })} /> default for Auto</label>
        <span className="grow" />
        <button onClick={onClose}>Close</button>
        <button className="primary" onClick={() => save()}>Save</button>
      </div>
    </Modal>
  )
}

export function Question({ q, onAnswer }) {
  return (
    <Modal title="The assistant asks" onClose={() => onAnswer(false)}>
      <p>{q.text}</p>
      <div className="modal-foot">
        <button onClick={() => onAnswer(false)}>No</button>
        <button className="primary" onClick={() => onAnswer(true)}>Yes</button>
      </div>
    </Modal>
  )
}
