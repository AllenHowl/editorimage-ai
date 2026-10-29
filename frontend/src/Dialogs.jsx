// 对话框：新建、浏览磁盘以打开或导入、导出和生成。
import { useEffect, useState } from 'react'
import { titleCase } from './util.js'
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
  const [name, setName] = useState('未命名')
  const [preset, setPreset] = useState('1080p')
  const [w, setW] = useState(1920)
  const [h, setH] = useState(1080)
  const [bg, setBg] = useState('#ffffff')
  const [transparent, setTransparent] = useState(false)
  const [path, setPath] = useState('')
  useEffect(() => { if (presets?.[preset]) { setW(presets[preset][0]); setH(presets[preset][1]) } }, [preset])
  const sep = home?.includes('\\') ? '\\' : '/'
  return (
    <Modal title="新建文档" onClose={onClose}>
      <label className="numfield wide"><span>名称</span><input value={name} onChange={e => setName(e.target.value)} /></label>
      <label className="numfield wide"><span>预设</span>
        <select value={preset} onChange={e => setPreset(e.target.value)}>
          {Object.entries(presets || {}).map(([k, v]) => <option key={k} value={k}>{titleCase(k)} — {v[0]}×{v[1]}</option>)}
          <option value="custom">自定义</option>
        </select></label>
      <div className="grid2">
        <label className="numfield"><span>宽度</span><input type="number" value={w} onChange={e => { setW(+e.target.value); setPreset('custom') }} /></label>
        <label className="numfield"><span>高度</span><input type="number" value={h} onChange={e => { setH(+e.target.value); setPreset('custom') }} /></label>
        <label className="numfield"><span>背景</span><input type="color" value={bg} disabled={transparent} onChange={e => setBg(e.target.value)} /></label>
        <label className="check"><input type="checkbox" checked={transparent} onChange={e => setTransparent(e.target.checked)} /> 透明</label>
      </div>
      <label className="numfield wide"><span>另存为</span>
        <input value={path} placeholder={`${home}${sep}${name}.json （可选，填写后启用自动保存）`} onChange={e => setPath(e.target.value)} /></label>
      <div className="modal-foot">
        <button onClick={onClose}>取消</button>
        <button className="primary" onClick={() => onCreate({ name, width: w, height: h, background: transparent ? 'transparent' : bg, path: path || null })}>创建</button>
      </div>
    </Modal>
  )
}

// mode：open 打开文档或照片，import 将图片导入为图层。
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
    <Modal title={mode === 'open' ? '打开文档或照片' : '导入图片为图层'} onClose={onClose}>
      <div className="row">
        <input className="grow" value={dir?.path || ''} onChange={e => setDir(d => ({ ...d, path: e.target.value }))}
          onKeyDown={e => e.key === 'Enter' && go(e.target.value)} />
        <button onClick={() => go(dir?.parent)}>上一级</button>
        {dir?.roots?.map(r => <button key={r} onClick={() => go(r)}>{r}</button>)}
      </div>
      {err && <p className="warn">{err}</p>}
      <div className="files">
        {dir?.entries?.map(f => (
          <div key={f.path} className={'file' + (chosen.includes(f.path) ? ' sel' : '') + (f.dir ? ' dir' : '')}
            onClick={() => click(f)} onDoubleClick={() => !f.dir && onPick([f.path])}>
            <span>{f.dir ? '📁' : f.kind === 'doc' ? '📄' : '🖼'}</span> {f.name}
          </div>))}
        {dir && !dir.entries.length && <p className="hint">此处没有图片或文档。</p>}
      </div>
      {mode === 'import' && <div className="modal-foot">
        <span className="hint">已选择 {chosen.length} 项</span>
        <button onClick={onClose}>取消</button>
        <button className="primary" disabled={!chosen.length} onClick={() => onPick(chosen)}>导入</button>
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
    <Modal title="导出图片" onClose={onClose}>
      <label className="numfield wide"><span>文件</span><input value={path} onChange={e => setPath(e.target.value)} /></label>
      <p className="hint">根据扩展名选择格式：{(formats || ['.png', '.jpg', '.webp', '.tif']).join(' ')}</p>
      <div className="grid2">
        <label className="numfield"><span>质量</span><input type="number" min={1} max={100} value={quality} onChange={e => setQuality(+e.target.value)} /></label>
        <label className="numfield"><span>缩放</span><input type="number" min={0.05} max={4} step={0.05} value={scale} onChange={e => setScale(+e.target.value)} /></label>
        <label className="check"><input type="checkbox" checked={transparent} onChange={e => setTransparent(e.target.checked)} /> 保留透明度</label>
        <span className="hint">{Math.round(project.width * scale)}×{Math.round(project.height * scale)} px</span>
      </div>
      {err && <p className="warn">{err}</p>}
      <div className="modal-foot">
        <button onClick={onClose}>取消</button>
        <button className="primary" disabled={busy} onClick={go}>{busy ? '正在导出…' : '导出'}</button>
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
        <button onClick={onClose}>取消</button>
        <button className="primary" disabled={!text.trim()} onClick={() => onRun(text.trim())}>执行</button>
      </div>
    </Modal>
  )
}

// 为任务列出自动模式、支持该任务的本地引擎及已就绪服务。
const LOCALI = { generate: ['comfyui'], edit: ['comfyui'], inpaint: ['comfyui'], describe: [],
  upscale: ['comfyui', 'lanczos'], cutout: ['local'] }
const NOMI_LOCALI = { comfyui: 'ComfyUI（本地）', lanczos: 'Lanczos（本地）', local: 'rembg（本地）' }

export function backendChoices(services, task) {
  const pronti = (services?.providers || []).filter(p => p.pronto && p.fa.includes(task))
  const def = services?.providers?.find(p => p.id === services?.default)
  const auto = def && def.fa.includes(task) ? `自动（${def.nome}）`
    : task === 'describe' ? '自动（首个已配置密钥的服务）' : '自动（本地）'
  return [['auto', auto], ...(LOCALI[task] || []).map(l => [l, NOMI_LOCALI[l] || l]),
    ...pronti.map(p => [p.id, `${p.nome} · ${p.modelli[task] || ''}`])]
}

function Backend({ services, task, value, onChange }) {
  return (
    <label className="numfield wide"><span>模型</span>
      <select value={value} onChange={e => onChange(e.target.value)}>
        {backendChoices(services, task).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select></label>
  )
}

const senzaServizi = (services, task) => !(services?.providers || []).some(p => p.pronto && p.fa.includes(task))

// 提示词和引擎选择，图生图还可调强度，用于生成、填充和编辑。
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
      {strength && <label className="slider"><span>强度</span>
        <input type="range" min={0.1} max={1} step={0.05} value={forza} onChange={e => setForza(+e.target.value)} />
        <span className="num">{forza.toFixed(2)}</span></label>}
      {hint && <p className="hint">{hint}</p>}
      {senzaServizi(services, task) && <p className="hint">尚未配置在线模型。 <button className="link" onClick={onSettings}>添加 API 密钥…</button></p>}
      <div className="modal-foot">
        <button onClick={onClose}>取消</button>
        <button className="primary" disabled={!text.trim()} onClick={via}>执行</button>
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
    <Modal title="生成式扩展画布" onClose={onClose}>
      <div className="row wrap">
        {[10, 25, 50].map(p => { const v = Math.round(lato * p / 100); return (
          <button key={p} onClick={() => setM({ left: v, top: v, right: v, bottom: v })}>四边各 +{p}%</button>) })}
        <button onClick={() => { const v = Math.round(project.width / 4); setM({ left: v, top: 0, right: v, bottom: 0 }) }}>加宽</button>
        <button onClick={() => { const v = Math.round(project.height / 4); setM({ left: 0, top: v, right: 0, bottom: v }) }}>加高</button>
      </div>
      <div className="grid2">
        {['left', 'right', 'top', 'bottom'].map(k => (
          <label key={k} className="numfield"><span>{titleCase(k)}</span>
            <input type="number" min={0} value={m[k]} onChange={e => setM({ ...m, [k]: Math.max(0, +e.target.value) })} /></label>))}
      </div>
      <p className="hint">新尺寸 {W}×{H} 像素。扩展区域将保存为带蒙版的顶层图层，保留原始图层。</p>
      <textarea rows={2} value={prompt} placeholder="描述画面以外的内容（可选）" onChange={e => setPrompt(e.target.value)} />
      <Backend services={services} task="inpaint" value={backend} onChange={setBackend} />
      <p className="hint">选择“自动（本地）”且不填写提示词时，会使用传统修复算法填充边缘，适合天空、墙面等简单背景。要生成新内容，请填写提示词并使用 ComfyUI 或在线模型。</p>
      <div className="modal-foot">
        <button onClick={onClose}>取消</button>
        <button className="primary" disabled={m.left + m.top + m.right + m.bottom === 0}
          onClick={() => onRun({ ...m, prompt: prompt.trim() || null, backend })}>扩展</button>
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
    <Modal title={target?.layer ? '描述图层' : '描述图片'} onClose={onClose}>
      <textarea rows={2} value={question} placeholder="输入有关图片的问题（可选）" onChange={e => setQuestion(e.target.value)} />
      <Backend services={services} task="describe" value={backend} onChange={setBackend} />
      {senzaServizi(services, 'describe') &&
        <p className="hint">图片描述需要视觉模型。 <button className="link" onClick={onSettings}>添加 API 密钥…</button></p>}
      {err && <p className="warn">{err}</p>}
      {out && <div className="described"><p>{out.text}</p>
        <div className="row"><small className="hint">{out.backend}</small><span className="grow" />
          <button onClick={() => navigator.clipboard?.writeText(out.text)}>复制</button></div></div>}
      <div className="modal-foot">
        <button onClick={onClose}>关闭</button>
        <button className="primary" disabled={busy} onClick={go}>{busy ? '正在分析…' : out ? '再次提问' : '描述图片'}</button>
      </div>
    </Modal>
  )
}

const COMPITI = { generate: '文生图', edit: '图生图', inpaint: '局部重绘', describe: '图片描述',
  upscale: '图片放大', cutout: '移除背景' }

// 在线模型设置：密钥、各任务模型及自动模式的默认服务。
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
      onSaved(r); setKey(''); setOk('已保存。')
    } catch (e) { setErr(e.message) }
  }
  return (
    <Modal title="AI 模型" onClose={onClose}>
      <p className="hint">本地 AI（rembg、OpenCV、ComfyUI）无需密钥。添加密钥后可使用在线模型，费用由服务商向你的账户收取。密钥仅保存在本机（~/.pedit/ai_api.json），不会写入文档。</p>
      <label className="numfield wide"><span>服务</span>
        <select value={pid} onChange={e => setPid(e.target.value)}>
          {services?.providers?.map(x => <option key={x.id} value={x.id}>
            {x.nome}{x.pronto ? ' ✓' : ''}{services.default === x.id ? '（默认）' : ''}</option>)}
        </select></label>
      {p?.nota && <p className="hint">{p.nota}</p>}
      {p?.vuole_chiave && <label className="numfield wide"><span>API 密钥</span>
        <input type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)}
          placeholder={p.chiave ? `${p.chiave}${p.chiave_da_altrove ? '（来自助手配置或环境变量）' : ''}` : '粘贴密钥'} /></label>}
      {p?.indirizzo_modificabile && <label className="numfield wide"><span>地址</span>
        <input value={url} placeholder="https://…/v1" onChange={e => setUrl(e.target.value)} /></label>}
      {p?.fa.map(c => (
        <label key={c} className="numfield wide"><span>{COMPITI[c] || c}</span>
          <input list={`m-${pid}`} value={models[c] ?? ''} onChange={e => setModels({ ...models, [c]: e.target.value })} /></label>))}
      <datalist id={`m-${pid}`}>{p?.suggeriti?.map(m => <option key={m} value={m} />)}</datalist>
      {p && !p.pronto && p.motivo && <p className="warn">{p.motivo}</p>}
      {err && <p className="warn">{err}</p>}
      {ok && <p className="hint">{ok}</p>}
      <div className="modal-foot">
        {p?.chiave && !p.chiave_da_altrove && <button onClick={() => save({ key: '' })}>移除密钥</button>}
        <label className="check" title="自动模式使用此服务生成、填充和编辑图片">
          <input type="checkbox" checked={services?.default === pid} onChange={e => save({ default: e.target.checked })} /> 设为自动模式的默认服务</label>
        <span className="grow" />
        <button onClick={onClose}>关闭</button>
        <button className="primary" onClick={() => save()}>保存</button>
      </div>
    </Modal>
  )
}

export function Question({ q, onAnswer }) {
  return (
    <Modal title="助手请求确认" onClose={() => onAnswer(false)}>
      <p>{q.text}</p>
      <div className="modal-foot">
        <button onClick={() => onAnswer(false)}>否</button>
        <button className="primary" onClick={() => onAnswer(true)}>是</button>
      </div>
    </Modal>
  )
}
