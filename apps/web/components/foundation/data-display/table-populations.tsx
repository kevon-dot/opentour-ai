'use client';

import { useId } from 'react';
import { Button } from '../tokens';
import { TableCell } from '../tokens/display';
import { Notice } from '../tokens/notice';
import type { TablePopulationsProps } from './table-populations.types';

const styles = `
.ot-population { display:grid; min-width:0; gap:var(--ot-space-16); padding:var(--ot-space-16); background:var(--ot-surface-canvas); font:var(--ot-type-body-size)/var(--ot-type-body-line) Geist,sans-serif; color:var(--ot-text-primary); overflow-wrap:anywhere }
.ot-population :is(p,h2) { margin:0 }
.ot-population h2 { font:600 var(--ot-type-heading-size)/var(--ot-type-heading-line) Geist,sans-serif }
.ot-population .controls { display:flex; flex-wrap:wrap; gap:var(--ot-space-12) }
.ot-population .field { font:var(--ot-type-label-weight) var(--ot-type-label-size)/var(--ot-type-label-line) Geist,sans-serif; display:grid; gap:var(--ot-space-8); background:var(--ot-surface-base); border:1px solid var(--ot-border-control); border-radius:var(--ot-radius-8); padding:var(--ot-space-16) }
.ot-population .field :is(select,input) { min-width:0; max-width:100%; background:transparent; padding:0; font:inherit; color:inherit; border:0 }
.ot-population small { font:var(--ot-type-small-size)/var(--ot-type-small-line) Geist,sans-serif; color:var(--ot-text-secondary) }
.ot-population .scroll { min-width:0; overflow-x:auto }
.ot-population table { font:inherit; table-layout:fixed; border-collapse:collapse; width:100%; min-width:640px }
.ot-population tr {height:44px}
.ot-population :is(td,th) { text-align:start; padding:0 }
.ot-population .ot-table-cell-frame { width:auto; min-width:0; height:100%; box-sizing:border-box }
.ot-population .ot-table-cell-value { font:inherit }
.ot-population tbody .ot-table-cell { font:var(--ot-type-small-size)/var(--ot-type-small-line) Geist,sans-serif }
.ot-population .ot-table-cell, .ot-population .time { border:1px solid var(--ot-border-default) }
.ot-population thead th { font:var(--ot-type-label-weight) var(--ot-type-label-size)/var(--ot-type-label-line) Geist,sans-serif }
.ot-population .time { background:var(--ot-action-subtle); padding:var(--ot-space-12) }
.ot-population .check { width:44px; text-align:center }
.ot-population .check { vertical-align:middle }
.ot-population .check input { appearance:none; display:block; margin:auto; width:20px; height:20px; border:1px solid var(--ot-border-control); border-radius:var(--ot-radius-4); background-color:var(--ot-surface-base) }
.ot-population .check input:checked { background:var(--ot-action-primary) url("data:image/svg+xml,%3Csvg%20width%3D%2216%22%20height%3D%2216%22%20viewBox%3D%220%200%2016%2016%22%20fill%3D%22none%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%0A%3Cg%20clip-path%3D%22url(%23clip0_334_5)%22%3E%0A%3Cpath%20d%3D%22M14.6663%207.38662V7.99995C14.6655%209.43756%2014.2%2010.8364%2013.3392%2011.9878C12.4785%2013.1393%2011.2685%2013.9816%209.88991%2014.3892C8.5113%2014.7968%207.03785%2014.7479%205.68932%2014.2497C4.3408%2013.7515%203.18944%2012.8307%202.40698%2011.6247C1.62452%2010.4186%201.25287%208.99199%201.34746%207.55749C1.44205%206.12299%201.99781%204.7575%202.93186%203.66467C3.86591%202.57183%205.1282%201.81021%206.53047%201.49338C7.93274%201.17656%209.39985%201.32151%2010.713%201.90662M14.6663%202.66662L7.99968%209.33995L5.99968%207.33995%22%20stroke%3D%22%231E1E1E%22%20stroke-width%3D%221.6%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%2F%3E%0A%3C%2Fg%3E%0A%3Cdefs%3E%0A%3CclipPath%20id%3D%22clip0_334_5%22%3E%0A%3Crect%20width%3D%2216%22%20height%3D%2216%22%20fill%3D%22white%22%2F%3E%0A%3C%2FclipPath%3E%0A%3C%2Fdefs%3E%0A%3C%2Fsvg%3E%0A") center no-repeat }
@media(forced-colors:active){.ot-population .check input {appearance:auto}}
.ot-population .record { display:block; text-align:start; width:100%; padding:0; background:transparent; border:0; color:inherit; font:inherit; cursor:pointer }
.ot-population .record-cell { padding:var(--ot-space-12); border:1px solid var(--ot-border-default); font:var(--ot-type-small-size)/var(--ot-type-small-line) Geist,sans-serif }
.ot-population tr[data-selected=true] :is(.record-cell,.ot-table-cell) { background:var(--ot-action-subtle) }
@media(min-width:700px){.ot-population .scroll-hint { display:none }}
.ot-population .counts,.ot-population caption {font:var(--ot-type-label-weight) var(--ot-type-label-size)/var(--ot-type-label-line) Geist,sans-serif}
.ot-population caption { text-align:start; padding-bottom:var(--ot-space-8) }
.ot-population .ot-button { min-height:44px; width:100% }
.ot-population .controls .ot-button-group { width:min(280px,100%) }
.ot-population .controls.recovery .ot-button-group {width:min(240px,100%)}
.ot-population > .field { width:min(360px,100%); box-sizing:border-box }
.ot-population tr[data-selected=true]:first-child :is(.record-cell,.ot-table-cell) { border-color:var(--ot-action-primary) }
.ot-population .time,.ot-population tr td:last-child { text-align:end }
.ot-population .sr { position:absolute; width:1px; height:1px; overflow:hidden; clip-path:inset(50%) }
@media(min-width:600px) and (max-width:1199px){.ot-population{padding:var(--ot-space-24)}}
@media(min-width:1200px){.ot-population{padding:var(--ot-space-32)}}
@media(max-width:599px){.ot-population .controls .ot-button-group{width:100%}}
@media(max-width:599px){.ot-population .controls {flex-direction:column}.ot-population .controls .ot-button{width:100%}}
`;

export function TablePopulations(p: TablePopulationsProps) {
  const labelId = useId();
  const helperId = useId();
  const selected = new Set(p.selection.mode === 'page' ? p.selection.ids : p.selection.visibleIds);
  const count = p.selection.mode === 'query' ? p.selection.count : p.rows.filter(row => selected.has(row.id)).length;
  const arrow = p.direction === 'descending' ? '↓' : '↑';
  return <section className="ot-population" aria-labelledby={labelId}>
    <style>{styles}</style>
    <h2 id={labelId}>{p.caption}</h2><p>{p.context}</p>
    <form onSubmit={event => {
      event.preventDefault();
      p.onIntent({ type: 'search', value: String(new FormData(event.currentTarget).get('search') ?? '') });
    }}>
      <label className="field">Search records
        <input key={p.search} name="search" type="search" defaultValue={p.search} />
        <small>Search on Enter. Clear filters also clears this search.</small>
      </label>
    </form>
    <div className="controls">
      <Button variant="secondary" label="Filter" onPress={() => p.onIntent({ type:'filter' })}/>
      <Button variant="secondary" label={`Sort: observed time ${arrow}`} onPress={() => p.onIntent({ type:'sort' })}/>
      {p.filters.length > 0 && p.state !== 'filtered-to-nothing' && <Button variant="secondary" label="Clear filters" onPress={() => p.onIntent({ type:'clear' })}/>}
    </div>
    {p.filters.length > 0 && p.state !== 'filtered-to-nothing' && <p>Filters: {p.filters.join(' · ')}</p>}
    {count > 0 && <Notice tone="info" title={p.selection.mode === 'page' ? `${count} selected on this page` : `${count} selected across this query`}
      body={p.selection.mode === 'page' ? `Only the ${count} visible record ${count === 1 ? 'ID is' : 'IDs are'} selected.` : `Frozen selection revision ${p.selection.revision} · ${count} matching records. New arrivals are excluded.`}/>}
    {count > 0 && <div className="controls">
      {p.state === 'populated' && p.selection.mode === 'page' && p.matching !== null && <Button variant="secondary" label={`Select all ${p.matching} matching records`} onPress={() => p.onIntent({type:'select-query'})}/>}
      {p.selection.mode === 'query' && <Button variant="secondary" label="Clear selection" onPress={() => p.onIntent({type:'clear-selection'})}/>}
      <Button label="Review selected records" onPress={() => p.onIntent({type:'review'})}/>
    </div>}
    {p.state === 'partial' && <Notice title="Partial results" body={p.reason} tone="unknown"/>}
    <p className="counts">{p.state === 'empty' ? '0 total records' : p.state === 'filtered-to-nothing'
      ? [`0 matching of ${p.total ?? 'unknown'} total`, ...p.filters].join(' · ')
      : p.total === null ? `${p.rows.length} shown · Total not available` : `${p.rows.length} shown · ${p.matching ?? 'Unknown'} matching of ${p.total} total`}</p>
    {p.state === 'partial' && <p className="counts">{p.rows.length} received · {p.expected ?? 'Unknown'} expected</p>}
    <div className="scroll" role="region" aria-label="Records — scroll horizontally" tabIndex={0}>
      <table><caption>Records<span className="scroll-hint"> — scroll horizontally</span></caption><thead><tr>
        {p.state === 'populated' && <th scope="col" className="check"><input type="checkbox" aria-label="Select all records on this page" checked={p.rows.length > 0 && p.rows.every(r => selected.has(r.id))} disabled={!p.rows.length} onChange={() => p.onIntent({type:'select-page'})}/></th>}
        <TableCell header="column" tone="blue" value="Record"/><TableCell header="column" tone="blue" value="Origin / lifecycle"/>
        <th scope="col" className="time" aria-sort={p.direction}>Observed time {arrow}</th>
      </tr></thead><tbody>{p.rows.map(row => <tr key={row.id} data-selected={selected.has(row.id)}>
        {p.state === 'populated' && <td className="check"><input type="checkbox" checked={selected.has(row.id)} aria-label={`Select ${row.label}`} onChange={() => p.onIntent({type:'select-row',id:row.id})}/></td>}
        <th scope="row" className="record-cell"><button type="button" className="record" aria-label={`Inspect ${row.label}`} onClick={() => p.onIntent({type:'inspect',id:row.id})}>{row.label}</button></th>
        <TableCell value={row.lifecycle} tone={selected.has(row.id) ? 'blue' : 'default'}/><TableCell value={row.observed} tone={selected.has(row.id) ? 'blue' : 'default'}/>
      </tr>)}</tbody></table>
    </div>
    {p.state === 'empty' && <><Notice title="No records yet" body="Connect a source to import your first records." tone="info"/><div className="controls recovery"><Button variant="secondary" label="Connect a source" onPress={() => p.onIntent({type:'connect'})}/></div></>}
    {p.state === 'filtered-to-nothing' && <><Notice title="No matches" body="No records match these filters." tone="info"/><div className="controls recovery"><Button variant="secondary" label="Clear filters" onPress={() => p.onIntent({type:'clear'})}/></div></>}
    <label className="field">Rows per page<select value={p.pageSize} aria-describedby={helperId} onChange={e => p.onIntent({type:'page-size',value:Number(e.target.value)})}>
      {p.pageSizes.map(size => <option key={size} value={size}>{size}</option>)}</select><small id={helperId}>Whole number 1–100; default 25.</small></label>
    <p className="sr" role="status">{p.position} · {p.rows.length} shown · Observed time {p.direction}</p>
    <nav aria-label="Record pages" className="controls"><Button variant="quiet" label="Previous" disabled={!p.previous} onPress={() => p.onIntent({type:'previous'})}/>
      <Button variant="quiet" label="Next" disabled={!p.next} onPress={() => p.onIntent({type:'next'})}/></nav>
  </section>;
}

/** Compact controlled presentation used by the operation-list selection context. */
export function PageSelectionNavigation(p: import('./table-populations.types').PageSelectionNavigationProps) {
  return <section className="ot-population ot-page-selection" aria-label="Table selection and navigation">
    <style>{styles + `
      .ot-page-selection { padding:var(--ot-space-24); background:var(--ot-surface-base); max-width:576px; box-sizing:border-box }
      .ot-page-selection h2 { font:600 var(--ot-type-section-size)/var(--ot-type-section-line) Geist,sans-serif }
      .ot-page-selection p { color:var(--ot-text-secondary) }
      .ot-page-selection input { box-sizing:border-box; width:100%; padding:var(--ot-space-12); border:2px solid var(--ot-action-primary); border-radius:var(--ot-radius-6); font:inherit }
      .ot-page-selection .controls { flex-direction:row }
      .ot-page-selection .controls .ot-button-group { width:auto }
      .ot-page-selection .ot-button { min-height:36px; width:auto }
      .ot-page-selection > .ot-button-group .ot-button { min-width:min(112px,100%) }
      .ot-page-selection nav .ot-button-group:first-child .ot-button { min-width:min(89px,100%) }
      .ot-page-selection nav .ot-button-group:last-child .ot-button { min-width:min(64px,100%) }
    `}</style>
    <h2>{p.selectedIds.length} records selected</h2>
    <p>Page selection: {p.selectedIds.length} / query result: {p.queryCount}. Bulk action binds the frozen selected IDs and revision.</p>
    <form onSubmit={event => {
      event.preventDefault();
      p.onIntent({type:'search',value:String(new FormData(event.currentTarget).get('search') ?? '')});
    }}>
      <input key={p.search} name="search" defaultValue={p.search} aria-label="Search operation ID" placeholder="Search operation ID"/>
    </form>
    <Button variant="secondary" label="Clear filters" onPress={() => p.onIntent({type:'clear'})}/>
    <small role="status">Page {p.page} of {p.pages} · {p.pageSize} records per page · observed {p.observed}</small>
    <nav className="controls" aria-label="Operation pages"><Button variant="quiet" label="Previous" disabled={!p.previous} onPress={() => p.onIntent({type:'previous'})}/>
      <Button variant="quiet" label="Next" disabled={!p.next} onPress={() => p.onIntent({type:'next'})}/></nav>
    <small>Column sort announces direction. Row menus keep focus and selection stable. Filtering resets incompatible selection.</small>
  </section>;
}
