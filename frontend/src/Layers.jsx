// Pannello livelli: dall'alto (disegnato sopra) al basso, come in Photoshop.
import { useState } from 'react'
import Icon from './Icons.jsx'

const TIPO = { image: 'image', text: 'text', shape: 'shape', fill: 'fill', adjustment: 'adjust', paint: 'paint' }

export default function Layers({ project, revision, selectedId, setSelectedId, run }) {
  const [rename, setRename] = useState(null)
  const [dragId, setDragId] = useState(null)
  const layers = project?.layers || []
  const n = layers.length

  const drop = async targetId => {
    if (!dragId || dragId === targetId) return
    // la lista e' dall'alto: l'indice del documento e' rovesciato
    const i = layers.findIndex(l => l.id === targetId)
    await run('move_layer', { layer: dragId, index: n - 1 - i })
    setDragId(null)
  }

  return (
    <div className="layers">
      <div className="layer-list" role="list">
        {layers.map(ly => (
          <div key={ly.id} role="listitem"
            className={'layer' + (ly.id === selectedId ? ' sel' : '') + (ly.visible === false ? ' hidden' : '')}
            draggable onDragStart={() => setDragId(ly.id)} onDragOver={e => e.preventDefault()} onDrop={() => drop(ly.id)}
            onClick={() => setSelectedId(ly.id)}>
            <button className="icon-btn" title={ly.visible === false ? 'Show' : 'Hide'}
              onClick={e => { e.stopPropagation(); run('set_layer', { layer: ly.id, visible: ly.visible === false }) }}>
              <Icon name={ly.visible === false ? 'eyeOff' : 'eye'} size={15} />
            </button>
            <div className={'thumb t-' + ly.type}>
              {ly.type === 'adjustment' ? <Icon name="adjust" size={16} />
                : <img src={`/api/layer/${ly.id}/thumb?size=64&rev=${revision}`} alt="" />}
              {ly.mask && <span className="mask-dot" title="Has a mask" />}
            </div>
            <div className="layer-name">
              {rename === ly.id
                ? <input autoFocus defaultValue={ly.name} onClick={e => e.stopPropagation()}
                  onBlur={e => { setRename(null); if (e.target.value !== ly.name) run('set_layer', { layer: ly.id, name: e.target.value }) }}
                  onKeyDown={e => { if (e.key === 'Enter') e.target.blur(); if (e.key === 'Escape') setRename(null) }} />
                : <span onDoubleClick={() => setRename(ly.id)} title="Double click to rename">{ly.name || ly.id}</span>}
              <small>{TIPO[ly.type]}{ly.blend && ly.blend !== 'normal' ? ` · ${ly.blend}` : ''}
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
      </div>
      <div className="layer-actions">
        <button title="Paint layer" onClick={() => run('add_paint_layer', {})}><Icon name="paint" size={15} /></button>
        <button title="Fill layer (background)" onClick={() => run('add_fill', { kind: 'solid', color: '#ffffff', index: 0 })}><Icon name="fill" size={15} /></button>
        <button title="Gradient layer" onClick={() => run('add_fill', { kind: 'linear', color: '#1d2b64', color2: '#f8cdda', angle: 180 })}><Icon name="adjust" size={15} /></button>
        <span className="sep" />
        <button title="Move up" disabled={!selectedId} onClick={() => run('move_layer', { layer: selectedId, to: 'up' })}><Icon name="up" size={15} /></button>
        <button title="Move down" disabled={!selectedId} onClick={() => run('move_layer', { layer: selectedId, to: 'down' })}><Icon name="down" size={15} /></button>
        <button title="Duplicate" disabled={!selectedId} onClick={() => run('duplicate_layer', { layer: selectedId })}><Icon name="copy" size={15} /></button>
        <button title="Merge down" disabled={!selectedId} onClick={() => run('merge_down', { layer: selectedId })}><Icon name="merge" size={15} /></button>
        <button title="Delete layer" disabled={!selectedId} onClick={() => run('remove_layer', { layer: selectedId })}><Icon name="trash" size={15} /></button>
      </div>
    </div>
  )
}
