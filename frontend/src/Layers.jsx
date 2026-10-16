// Pannello livelli: dall'alto (disegnato sopra) al basso, come in Photoshop.
// I gruppi si aprono e chiudono; trascinare su un gruppo ci mette dentro il
// livello, su un livello lo mette al suo posto (anche fuori da un gruppo).
import { useState } from 'react'
import Icon from './Icons.jsx'
import { flatLayers, siblingsOf } from './util.js'

const TIPO = { image: 'image', text: 'text', shape: 'shape', fill: 'fill', adjustment: 'adjust', paint: 'paint', group: 'group' }

export default function Layers({ project, revision, selectedId, setSelectedId, run }) {
  const [rename, setRename] = useState(null)
  const [dragId, setDragId] = useState(null)
  const [over, setOver] = useState(null)
  const layers = project?.layers || []
  const rows = flatLayers(layers, true)
  const sel = rows.find(l => l.id === selectedId)

  const drop = async (target, e) => {
    setOver(null)
    if (!dragId || dragId === target.id) return
    // su un gruppo con Shift o nella sua meta' destra: dentro; altrimenti al suo posto
    const dentro = target.type === 'group' && (e.shiftKey || e.nativeEvent.offsetX > e.currentTarget.clientWidth / 2)
    if (dentro) await run('move_layer', { layer: dragId, into: target.id })
    else {
      const fratelli = siblingsOf(layers, target.id)
      const i = fratelli.findIndex(l => l.id === target.id)
      // la lista e' dall'alto: l'indice del documento e' rovesciato
      const into = target.parent || 'root'
      const stessa = fratelli.some(l => l.id === dragId)
      await run('move_layer', stessa ? { layer: dragId, index: fratelli.length - 1 - i }
        : { layer: dragId, into, index: fratelli.length - i })
    }
    setDragId(null)
  }

  return (
    <div className="layers">
      <div className="layer-list" role="list">
        {rows.map(ly => (
          <div key={ly.id} role="listitem"
            className={'layer' + (ly.id === selectedId ? ' sel' : '') + (ly.visible === false || ly.hiddenByParent ? ' hidden' : '')
              + (over === ly.id ? ' over' : '') + (ly.type === 'group' ? ' group' : '')}
            style={{ paddingLeft: 6 + ly.depth * 14 }}
            draggable onDragStart={() => setDragId(ly.id)} onDragOver={e => { e.preventDefault(); setOver(ly.id) }}
            onDragLeave={() => setOver(o => (o === ly.id ? null : o))} onDrop={e => drop(ly, e)}
            onClick={() => setSelectedId(ly.id)}>
            {ly.type === 'group'
              ? <button className="icon-btn caret" title={ly.collapsed ? 'Open group' : 'Close group'}
                onClick={e => { e.stopPropagation(); run('set_layer', { layer: ly.id, collapsed: !ly.collapsed }) }}>
                {ly.collapsed ? '▸' : '▾'}</button>
              : ly.clip ? <span className="clip-mark" title="Clipped to the layer below">↳</span> : null}
            <button className="icon-btn" title={ly.visible === false ? 'Show' : 'Hide'}
              onClick={e => { e.stopPropagation(); run('set_layer', { layer: ly.id, visible: ly.visible === false }) }}>
              <Icon name={ly.visible === false ? 'eyeOff' : 'eye'} size={15} />
            </button>
            <div className={'thumb t-' + ly.type}>
              {ly.type === 'adjustment' ? <Icon name="adjust" size={16} />
                : ly.type === 'group' ? <Icon name="folder" size={16} />
                  : <img src={`/api/layer/${ly.id}/thumb?size=64&rev=${revision}`} alt="" />}
              {ly.mask && <span className="mask-dot" title="Has a mask" />}
            </div>
            <div className="layer-name">
              {rename === ly.id
                ? <input autoFocus defaultValue={ly.name} onClick={e => e.stopPropagation()}
                  onBlur={e => { setRename(null); if (e.target.value !== ly.name) run('set_layer', { layer: ly.id, name: e.target.value }) }}
                  onKeyDown={e => { if (e.key === 'Enter') e.target.blur(); if (e.key === 'Escape') setRename(null) }} />
                : <span onDoubleClick={() => setRename(ly.id)} title="Double click to rename">{ly.name || ly.id}</span>}
              <small>{TIPO[ly.type]}{ly.type === 'group' ? ` · ${ly.children?.length || 0}` : ''}
                {ly.blend && !['normal', 'pass_through'].includes(ly.blend) ? ` · ${ly.blend.replace(/_/g, ' ')}` : ''}
                {ly.opacity !== undefined && ly.opacity !== 1 ? ` · ${Math.round(ly.opacity * 100)}%` : ''}
                {ly.effects?.length ? ` · fx ${ly.effects.length}` : ''}</small>
            </div>
            <button className={'icon-btn' + (ly.locked ? ' on' : '')} title={ly.locked ? 'Unlock' : 'Lock'}
              onClick={e => { e.stopPropagation(); run('set_layer', { layer: ly.id, locked: !ly.locked }) }}>
              <Icon name={ly.locked ? 'lock' : 'unlock'} size={14} />
            </button>
          </div>
        ))}
        {!layers.length && <p className="empty">No layers yet. Import an image, add text, or ask the assistant.</p>}
        {dragId && <div className="drop-root" onDragOver={e => e.preventDefault()}
          onDrop={() => { run('move_layer', { layer: dragId, into: 'root', index: 0 }); setDragId(null) }}>
          Drop here: bottom of the document</div>}
      </div>
      <div className="layer-actions">
        <button title="Paint layer" onClick={() => run('add_paint_layer', {})}><Icon name="paint" size={15} /></button>
        <button title="Fill layer (background)" onClick={() => run('add_fill', { kind: 'solid', color: '#ffffff', index: 0 })}><Icon name="fill" size={15} /></button>
        <button title="Gradient layer" onClick={() => run('add_fill', { kind: 'linear', color: '#1d2b64', color2: '#f8cdda', angle: 180 })}><Icon name="adjust" size={15} /></button>
        <button title={sel ? 'Put the selected layer in a new group' : 'New empty group'}
          onClick={() => (sel ? run('group_layers', { layers: [sel.id] }) : run('add_group', {}))
            .then(r => r?.result?.id && setSelectedId(r.result.id))}><Icon name="folder" size={15} /></button>
        <button title="Clip to the layer below (clipping mask)" disabled={!sel || sel.type === 'group'}
          className={sel?.clip ? 'on' : ''} onClick={() => run('set_layer', { layer: sel.id, clip: !sel.clip })}>↳</button>
        <span className="sep" />
        <button title="Move up" disabled={!selectedId} onClick={() => run('move_layer', { layer: selectedId, to: 'up' })}><Icon name="up" size={15} /></button>
        <button title="Move down" disabled={!selectedId} onClick={() => run('move_layer', { layer: selectedId, to: 'down' })}><Icon name="down" size={15} /></button>
        {sel?.parent && <button title="Take out of the group" onClick={() => run('move_layer', { layer: sel.id, into: 'root' })}>⇱</button>}
        <button title="Duplicate" disabled={!selectedId} onClick={() => run('duplicate_layer', { layer: selectedId })}><Icon name="copy" size={15} /></button>
        {sel?.type === 'group'
          ? <button title="Ungroup" onClick={() => run('ungroup', { layer: sel.id })}><Icon name="merge" size={15} /></button>
          : <button title="Merge down" disabled={!selectedId} onClick={() => run('merge_down', { layer: selectedId })}><Icon name="merge" size={15} /></button>}
        <button title="Delete layer" disabled={!selectedId} onClick={() => run('remove_layer', { layer: selectedId })}><Icon name="trash" size={15} /></button>
      </div>
    </div>
  )
}
