import { Tooltip, TooltipText } from './tooltip';
import { Metric, TableCell } from './display';
import './tooltip.fixtures.css';

export const tooltipCopy = {
  help: 'View exact values and source details.',
  evidence: ['Exact upper bound: 0.00993608194445772', 'Observed Nov 12, 09:42 UTC · 300 eligible cases'],
  long: 'View exact values and source details for the selected observation. Supplemental help can wrap across several lines. The required value and unavailable reason remain in the page, even after this help closes.',
} as const;

export type TooltipFixtureKind = 'evidence' | 'help' | 'long' | 'interactions' | 'edge' | 'scroll';
export function TooltipFixture({ kind = 'evidence' }: { kind?: TooltipFixtureKind }) {
  if (kind === 'evidence') return <div className="ot-tooltip-fixture">
    <header><p>OpenTour  /  Customer workspace</p><small>Acme Labs / Production</small>
      <button className="ot-button ot-button--secondary fixture-navigation">Open navigation</button></header>
    <main>
      <h1>Evidence details</h1><p className="fixture-description">Demo fixture · Receipt-status workflow · Nov 1–7</p>
      <Metric tone="blue" title="Critical error rate" value="0.0%" detail="0 / 300 · 95% upper bound 0.994%" />
      <div className="fixture-evidence-help">
        <Tooltip appearance="evidence" description={tooltipCopy.evidence}><button className="ot-button ot-button--primary">View exact upper bound</button></Tooltip>
        <div aria-hidden="true" className="fixture-reserved-help">{tooltipCopy.evidence.map(text => <p key={text}><TooltipText text={text} /></p>)}</div>
      </div>
      <section className="fixture-exact-values">
        <h2 id="exact-values-title">Always available exact values<span className="fixture-scroll-hint"> — scroll horizontally</span></h2>
        <div role="region" aria-labelledby="exact-values-title" tabIndex={0} className="fixture-exact-region">
          <table><thead><tr><TableCell header="column" tone="blue" value="Measure" /><TableCell header="column" tone="blue" value="Exact value" /></tr></thead>
            <tbody><tr><TableCell value="One-sided upper bound" /><TableCell value="0.00993608194445772" /></tr></tbody></table>
        </div>
      </section>
    </main>
  </div>;
  return <main className={`ot-tooltip-samples ot-tooltip-samples--${kind}`}>
    <h1>Supplemental help</h1>
    <p id="persistent-description">Required values remain available here.</p>
    <Tooltip description={kind === 'long' || kind === 'edge' ? tooltipCopy.long : tooltipCopy.help}>
      <button className="ot-button ot-button--secondary" aria-describedby="persistent-description" data-testid="first-trigger">View details</button>
    </Tooltip>
    {kind === 'interactions' && <>
      <Tooltip description="A different description for the comparison."><button className="ot-button ot-button--secondary" data-testid="second-trigger">Comparison details</button></Tooltip>
      <p id="unavailable-reason">Export is unavailable for your current access.</p>
      <Tooltip description="Export help is supplemental."><button className="ot-button ot-button--secondary" aria-disabled="true" aria-describedby="unavailable-reason">Export details</button></Tooltip>
      <button className="ot-button ot-button--quiet">Next control</button>
    </>}
    {kind === 'scroll' && <div className="fixture-scroll-space" />}
  </main>;
}
