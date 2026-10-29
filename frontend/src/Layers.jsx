// 图层面板按从上到下显示，与 Photoshop 的堆叠方式一致。
// 组支持展开和折叠；拖到组上可移入组内，
// 拖到其他图层上则移动到该位置，也可移出原组。
import { useState } from 'react'
import Icon from './Icons.jsx'
import { flatLayers, siblingsOf, titleCase } from './util.js'

const TIPO = { image: '图片', text: '文字', shape: '形状', fill: '填充', adjustment: '调整', paint: '绘画', group: '组' }

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
    // 按住 Shift 或拖到组行右半边时移入组内，否则移动到组所在位置。
    const dentro = target.type === 'group' && (e.shiftKey || e.nativeEvent.offsetX > e.currentTarget.clientWidth / 2)
    if (dentro) await run('move_layer', { layer: dragId, into: target.id })
    else {
      const fratelli = siblingsOf(layers, target.id)
      const i = fratelli.findIndex(l => l.id === target.id)
      // 面板从上到下排列，与文档索引方向相反。
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
              ? <button className="icon-btn caret" title={ly.collapsed ? '展开组' : '折叠组'}
                onClick={e => { e.stopPropagation(); run('set_layer', { layer: ly.id, collapsed: !ly.collapsed }) }}>
                {ly.collapsed ? '▸' : '▾'}</button>
              : ly.clip ? <span className="clip-mark" title="已剪贴到下方图层">↳</span> : null}
            <button className="icon-btn" title={ly.visible === false ? '显示' : '隐藏'}
              onClick={e => { e.stopPropagation(); run('set_layer', { layer: ly.id, visible: ly.visible === false }) }}>
              <Icon name={ly.visible === false ? 'eyeOff' : 'eye'} size={15} />
            </button>
            <div className={'thumb t-' + ly.type}>
              {ly.type === 'adjustment' ? <Icon name="adjust" size={16} />
                : ly.type === 'group' ? <Icon name="folder" size={16} />
                  : <img src={`/api/layer/${ly.id}/thumb?size=64&rev=${revision}`} alt="" />}
              {ly.mask && <span className="mask-dot" title="包含蒙版" />}
            </div>
            <div className="layer-name">
              {rename === ly.id
                ? <input autoFocus defaultValue={ly.name} onClick={e => e.stopPropagation()}
                  onBlur={e => { setRename(null); if (e.target.value !== ly.name) run('set_layer', { layer: ly.id, name: e.target.value }) }}
                  onKeyDown={e => { if (e.key === 'Enter') e.target.blur(); if (e.key === 'Escape') setRename(null) }} />
                : <span onDoubleClick={() => setRename(ly.id)} title="双击重命名">{ly.name || ly.id}</span>}
              <small>{TIPO[ly.type]}{ly.type === 'group' ? ` · ${ly.children?.length || 0}` : ''}
                {ly.blend && !['normal', 'pass_through'].includes(ly.blend) ? ` · ${titleCase(ly.blend)}` : ''}
                {ly.opacity !== undefined && ly.opacity !== 1 ? ` · ${Math.round(ly.opacity * 100)}%` : ''}
                {ly.effects?.length ? ` · 效果 ${ly.effects.length}` : ''}</small>
            </div>
            <button className={'icon-btn' + (ly.locked ? ' on' : '')} title={ly.locked ? '解锁' : '锁定'}
              onClick={e => { e.stopPropagation(); run('set_layer', { layer: ly.id, locked: !ly.locked }) }}>
              <Icon name={ly.locked ? 'lock' : 'unlock'} size={14} />
            </button>
          </div>
        ))}
        {!layers.length && <p className="empty">尚无图层。请导入图片、添加文字，或让助手帮你编辑。</p>}
        {dragId && <div className="drop-root" onDragOver={e => e.preventDefault()}
          onDrop={() => { run('move_layer', { layer: dragId, into: 'root', index: 0 }); setDragId(null) }}>
          拖到这里：移至文档最底层</div>}
      </div>
      <div className="layer-actions">
        <button title="绘画图层" onClick={() => run('add_paint_layer', {})}><Icon name="paint" size={15} /></button>
        <button title="填充图层（背景）" onClick={() => run('add_fill', { kind: 'solid', color: '#ffffff', index: 0 })}><Icon name="fill" size={15} /></button>
        <button title="渐变图层" onClick={() => run('add_fill', { kind: 'linear', color: '#1d2b64', color2: '#f8cdda', angle: 180 })}><Icon name="adjust" size={15} /></button>
        <button title={sel ? '将所选图层放入新组' : '新建空组'}
          onClick={() => (sel ? run('group_layers', { layers: [sel.id] }) : run('add_group', {}))
            .then(r => r?.result?.id && setSelectedId(r.result.id))}><Icon name="folder" size={15} /></button>
        <button title="剪贴到下方图层（剪贴蒙版）" disabled={!sel || sel.type === 'group'}
          className={sel?.clip ? 'on' : ''} onClick={() => run('set_layer', { layer: sel.id, clip: !sel.clip })}>↳</button>
        <span className="sep" />
        <button title="上移" disabled={!selectedId} onClick={() => run('move_layer', { layer: selectedId, to: 'up' })}><Icon name="up" size={15} /></button>
        <button title="下移" disabled={!selectedId} onClick={() => run('move_layer', { layer: selectedId, to: 'down' })}><Icon name="down" size={15} /></button>
        {sel?.parent && <button title="移出组" onClick={() => run('move_layer', { layer: sel.id, into: 'root' })}>⇱</button>}
        <button title="创建副本" disabled={!selectedId} onClick={() => run('duplicate_layer', { layer: selectedId })}><Icon name="copy" size={15} /></button>
        {sel?.type === 'group'
          ? <button title="取消编组" onClick={() => run('ungroup', { layer: sel.id })}><Icon name="merge" size={15} /></button>
          : <button title="向下合并" disabled={!selectedId} onClick={() => run('merge_down', { layer: selectedId })}><Icon name="merge" size={15} /></button>}
        <button title="删除图层" disabled={!selectedId} onClick={() => run('remove_layer', { layer: selectedId })}><Icon name="trash" size={15} /></button>
      </div>
    </div>
  )
}
