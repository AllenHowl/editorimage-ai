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

export function Export({ project, home, onClose, onDone }) {
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
      <p className="hint">The extension picks the format: .png .jpg .webp .tif</p>
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
