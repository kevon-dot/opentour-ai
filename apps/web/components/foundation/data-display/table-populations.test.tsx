import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { TablePopulations, PageSelectionNavigation } from './table-populations';
import type { TablePopulationsProps } from './table-populations.types';

const base = {
  caption:'Source records',context:'Receipt-status workflow',search:'',filters:[],
  total:600,matching:128,selection:{mode:'page',ids:['one','other-page']},
  direction:'descending',pageSize:25,pageSizes:[25,50,100],position:'Page 1',
  previous:false,next:true,onIntent:() => {},
} as const;
const rows = [{id:'one',label:'Receipt 025',lifecycle:'Eligible · Observed',observed:'09:42 UTC'}];
describe('table populations', () => {
  testCases([
    [{state:'empty',rows:[],matching:0,total:0},['No records yet','Connect a source','0 total records']],
    [{state:'filtered-to-nothing',rows:[],matching:0,filters:['Status: held']},['No matches','Clear filters','0 matching of 600 total','Status: held']],
    [{state:'partial',rows,expected:null,reason:'Source timed out',total:null},['Partial results','1 received · Unknown expected','Source timed out','1 shown · Total not available']],
    [{state:'populated',rows},['1 selected on this page','Only the 1 visible record ID is selected.','Select all 128 matching records']],
    [{state:'populated',rows,selection:{mode:'query',count:128,revision:'rev-8',visibleIds:['one']}},['128 selected across this query','Frozen selection revision rev-8 · 128 matching records. New arrivals are excluded.']],
  ] as const)('exposes the supplied population and scope', (state,labels) => {
    const props: TablePopulationsProps = {...base,...state};
    const html = renderToStaticMarkup(<TablePopulations {...props}/>);
    for(const label of labels) assert.ok((html)?.includes(label));
    assert.ok((html)?.includes('<caption class="sr">Records<span class="scroll-hint"> — scroll horizontally</span></caption>'));
    assert.ok((html)?.includes('aria-sort="descending"'));
    assert.ok((html)?.includes('Whole number 1–100; default 25.'));
    if(state.state === 'partial') assert.ok((html)?.includes('Select all 128') === false);
  });
});

it('renders the bound operation scope and cursor controls without inventing totals', () => {
  const html = renderToStaticMarkup(<PageSelectionNavigation selectedIds={['a','b','c','d']} queryCount={128} revision="r8"
    page={1} pages={13} pageSize={10} observed="09:42 UTC" search="" previous={false} next={true} onIntent={()=>{}}/>);
  assert.ok((html)?.includes('4 records selected'));
  assert.ok((html)?.includes('Page selection: 4 / query result: 128. Bulk action binds the frozen selected IDs and revision.'));
  assert.ok((html)?.includes('Page 1 of 13 · 10 records per page · observed 09:42 UTC'));
});
