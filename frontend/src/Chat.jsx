// Assistente: chiede in parole, lavora con le stesse operazioni dei pulsanti.
import { useEffect, useRef, useState } from 'react'
import { api, chatStream } from './api.js'
import Icon from './Icons.jsx'

function Settings({ llm, onSaved }) {
  const [pid, setPid] = useState(llm?.provider || 'claude_code')
  const p = llm?.providers?.find(x => x.id === pid)
  const [key, setKey] = useState('')
  const [model, setModel] = useState(p?.modello || '')
  const [url, setUrl] = useState(p?.indirizzo || '')
  useEffect(() => { setModel(p?.modello || ''); setUrl(p?.indirizzo || ''); setKey('') }, [pid])
  const save = async () => {
    const r = await api('/api/llm', { body: { provider: pid, key: key || null, model, url: p?.indirizzo_modificabile ? url : null } })
    onSaved(r)
  }
  return (
    <div className="llm-settings">
      <label className="numfield wide"><span>Model</span>
        <select value={pid} onChange={e => setPid(e.target.value)}>
          {llm?.providers?.map(x => <option key={x.id} value={x.id}>{x.nome}{x.pronto ? ' ✓' : ''}</option>)}
        </select></label>
      {p?.nota && <p className="hint">{p.nota}</p>}
      {p?.vuole_chiave && <label className="numfield wide"><span>API key</span>
        <input type="password" placeholder={p.chiave || 'paste the key'} value={key} onChange={e => setKey(e.target.value)} /></label>}
      {p?.indirizzo_modificabile && <label className="numfield wide"><span>Address</span>
        <input value={url} onChange={e => setUrl(e.target.value)} /></label>}
      <label className="numfield wide"><span>Model id</span>
        <input list="modelli" value={model} onChange={e => setModel(e.target.value)} placeholder="default" />
        <datalist id="modelli">{p?.modelli?.map(m => <option key={m} value={m} />)}</datalist></label>
      {p && !p.pronto && <p className="warn">{p.motivo}</p>}
      <button className="primary" onClick={save}>Use this model</button>
    </div>
  )
}

export default function Chat({ llm, setLlm, selection, selectedLayer }) {
  const [msgs, setMsgs] = useState([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [refs, setRefs] = useState([])
  const [showSettings, setShowSettings] = useState(!llm?.ok)
  const endRef = useRef(null)
  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }) }, [msgs])

  const push = ev => setMsgs(m => {
    const last = m[m.length - 1]
    if (ev.type === 'text') {
      if (last?.role === 'assistant' && last.kind === 'text') return [...m.slice(0, -1), { ...last, text: last.text + ev.text }]
      return [...m, { role: 'assistant', kind: 'text', text: ev.text }]
    }
    if (ev.type === 'tool') return [...m, { role: 'tool', name: ev.name, input: ev.input, status: 'run' }]
    if (ev.type === 'tool_done' || ev.type === 'tool_error') {
      const i = m.map(x => x.role === 'tool' && x.name === ev.name && x.status === 'run').lastIndexOf(true)
      if (i < 0) return m
      const c = [...m]
      c[i] = { ...c[i], status: ev.type === 'tool_done' ? 'ok' : 'err', error: ev.message }
      return c
    }
    if (ev.type === 'error') return [...m, { role: 'error', text: ev.message }]
    if (ev.type === 'note') return [...m, { role: 'note', text: ev.message }]
    if (ev.type === 'stopped') return [...m, { role: 'note', text: 'stopped' }]
    return m
  })

  const send = async () => {
    const q = text.trim()
    if (!q || busy) return
    setText('')
    setMsgs(m => [...m, { role: 'user', text: q, refs }])
    setBusy(true)
    const r = refs
    setRefs([])
    try { await chatStream({ question: q, refs: r }, push) } catch (e) { push({ type: 'error', message: e.message }) }
    setBusy(false)
  }

  return (
    <div className="chat">
      <div className="chat-top">
        <span className={'chip' + (llm?.ok ? '' : ' bad')} title={llm?.motivo || ''}>{llm?.nome || 'no model'}{llm?.model ? ` · ${llm.model}` : ''}</span>
        <span className="grow" />
        <button className="icon-btn" title="New conversation" onClick={() => { api('/api/chat/reset', { body: {} }); setMsgs([]) }}><Icon name="plus" size={15} /></button>
        <button className="icon-btn" title="Settings" onClick={() => setShowSettings(!showSettings)}><Icon name="gear" size={15} /></button>
      </div>
      {showSettings && <Settings llm={llm} onSaved={r => { setLlm(r); if (r.ok) setShowSettings(false) }} />}
      <div className="msgs">
        {!msgs.length && <div className="hint">
          Ask for an edit in plain words: <i>“remove the background and put it on a soft blue gradient”</i>,
          <i> “make it warmer and add a title at the bottom”</i>, <i>“remove the person on the left”</i>.
          Every step lands in the layers and undoes with Ctrl+Z.</div>}
        {msgs.map((m, i) => m.role === 'tool'
          ? <details key={i} className={'tool ' + m.status}><summary>{m.status === 'run' ? '⋯' : m.status === 'ok' ? '✓' : '✕'} {m.name}</summary>
            <pre>{JSON.stringify(m.input, null, 1)}</pre>{m.error && <p className="warn">{m.error}</p>}</details>
          : <div key={i} className={'msg ' + m.role}>{m.text}
            {m.refs?.length > 0 && <div className="refs">{m.refs.map((r, j) => <span key={j} className="tag">[{j + 1}] {r.type}</span>)}</div>}</div>)}
        <div ref={endRef} />
      </div>
      <div className="chat-refs">
        <button disabled={!selection} title="Point at the selected area"
          onClick={() => setRefs(r => [...r, { type: 'area', box: selection.box }])}>+ area</button>
        <button disabled={!selectedLayer} title="Point at the selected layer"
          onClick={() => setRefs(r => [...r, { type: 'layer', id: selectedLayer.id, name: selectedLayer.name }])}>+ layer</button>
        {refs.map((r, i) => <span key={i} className="tag" onClick={() => setRefs(x => x.filter((_, j) => j !== i))}>[{i + 1}] {r.type} ✕</span>)}
      </div>
      <div className="chat-input">
        <textarea rows={3} value={text} placeholder="What should change?" onChange={e => setText(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
        {busy
          ? <button className="icon-btn big" title="Stop" onClick={() => api('/api/chat/stop', { body: {} })}><Icon name="stop" /></button>
          : <button className="icon-btn big primary" title="Send" onClick={send}><Icon name="send" /></button>}
      </div>
    </div>
  )
}
