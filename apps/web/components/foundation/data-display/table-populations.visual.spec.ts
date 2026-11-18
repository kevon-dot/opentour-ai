import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { build } from 'vite';

let script: string;
let styles: string;
test.beforeAll(async () => {
  const root = join(process.cwd(), 'apps/web');
  const entry = join(root, 'table-browser-fixture.tsx');
  const result = await build({
    root, configFile: false, envFile: false, publicDir: false, logLevel: 'error',
    oxc: { jsx: { runtime: 'automatic' } },
    plugins: [{
      name: 'table-fixture', resolveId: id => id === entry ? entry : null,
      load: id => id === entry ? `
        import { createRoot } from 'react-dom/client';
        import { TablePopulations, PageSelectionNavigation } from './components/foundation/data-display/table-populations';
        const rows = Array.from({length:25}, (_,i) => ({
          id:'record-'+i,label:'Receipt '+String(25-i).padStart(3,'0'),lifecycle:i===0?'Eligible · Observed':i===1?'Protected · Sealed':'Synthetic · Draft',observed:i===0?'Nov 7, 09:42 UTC':i===1?'Nov 7, 09:39 UTC':'Nov 7, 09:30 UTC',
        }));
        const state = document.querySelector('main').dataset.state;
        const shown = state === 'populated' ? rows : state === 'partial' ? rows.slice(1,4) : [];
        window.intents=[];
        createRoot(document.querySelector('main')).render(state === 'navigation' ? <PageSelectionNavigation
          selectedIds={['a','b','c','d']} queryCount={128} revision="8" page={1} pages={13} pageSize={10}
          observed="09:42 UTC" previous={false} next={true} search="" onIntent={intent=>window.intents.push(intent)}/> : <TablePopulations
          caption="Source records" context="Receipt-status workflow · Locked population receipts-nov-01-07 · Revision 8"
          search={state === 'empty' ? '' : 'receipt'} filters={[]} total={state === 'empty' ? 0 : state === 'partial' ? null : 600}
          matching={shown.length ? 128 : 0} selection={{mode:'page', ids:state==='populated'?shown.map(r=>r.id):[]}} direction="descending"
          pageSize={25} pageSizes={[25,50,100]} position="Page 1" previous={false} next={true}
          state={state} rows={shown} reason="3 records shown. More results could not be loaded; this is not the complete population." expected={25} onIntent={intent=>window.intents.push(intent)}/>);` : null,
    }],
    build: { write: false, lib: { entry, name: 'TableFixture', formats: ['es'] } },
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  });
  const bundle = Array.isArray(result) ? result[0] : result;
  if (!('output' in bundle)) throw new Error('Missing table fixture bundle');
  const js = bundle.output.find(item => item.type === 'chunk');
  const css = bundle.output.find(item => item.type === 'asset' && item.fileName.endsWith('.css'));
  if (!js || js.type !== 'chunk' || !css || css.type !== 'asset') throw new Error('Missing table fixture assets');
  script = js.code;
  styles = String(css.source);
});


for (const width of [320,768,1280,1440]) for (const scale of [1,2]) {
  test(`table states at ${width}px and ${scale*100}% text`,async ({page},info)=>{
    await page.route('http://table.test/**', route => route.fulfill({contentType:'text/html',body:'<main></main>'}));
    await page.route('http://table.test/table.js', route => route.fulfill({contentType:'text/javascript',body:script}));

    await page.setViewportSize({width,height:900});
    for(const state of ['populated','partial','empty','filtered-to-nothing','navigation']) {
      await page.goto('http://table.test/');
      await page.setContent('<html lang="en"><main data-state="'+state+'"></main></html>');
      await page.addStyleTag({content:styles+`html{font-size:${scale*100}%}body{margin:0}`});
      await page.addScriptTag({type:'module',url:'http://table.test/table.js'});
      if(state==='navigation') {
        await expect(page.getByRole('heading',{name:'4 records selected'})).toBeVisible();
        await expect(page.getByRole('status')).toHaveText('Page 1 of 13 · 10 records per page · observed 09:42 UTC');
        await page.screenshot({path:info.outputPath('navigation.png'),fullPage:true});
        continue;
      }
      const table=page.getByRole('table',{name:'Records — scroll horizontally'});
      await expect(table).toBeVisible();
      await page.evaluate(()=>document.fonts.ready);
      expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBe(width);
      const region=page.getByRole('region',{name:'Records — scroll horizontally'});
      if(width===320) expect(await region.evaluate(el=>el.scrollWidth > el.clientWidth)).toBe(true);
      await expect(table.getByRole('columnheader',{name:'Observed time ↓'})).toHaveAttribute('aria-sort','descending');
      await page.screenshot({path:info.outputPath(state+'.png'),fullPage:true});
      if(state==='populated') {
        for(const [name,type] of [['Inspect Receipt 025','inspect'],['Next','next'],['Filter','filter'],['Sort: observed time ↓','sort'],['Select all 128 matching records','select-query'],['Review selected records','review']]) {
          await page.getByRole('button',{name,exact:true}).click();
          expect(await page.evaluate(()=>Reflect.get(window,'intents').splice(0))).toEqual([type==='inspect'?{type,id:'record-0'}:{type}]);
        }
        await expect(table.getByRole('row')).toHaveCount(26);
        await expect(page.getByRole('button',{name:'Previous'})).toBeDisabled();
        await page.getByRole('searchbox').fill('new query');
        await page.getByRole('searchbox').press('Enter');
        expect(await page.evaluate(()=>Reflect.get(window,'intents').splice(0))).toEqual([{type:'search',value:'new query'}]);
        await page.getByRole('combobox').selectOption('50');
        expect(await page.evaluate(()=>Reflect.get(window,'intents').splice(0))).toEqual([{type:'page-size',value:50}]);
        await expect(page.getByRole('combobox')).toHaveValue('25');
      }
    }
  });
}
