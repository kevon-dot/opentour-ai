import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import * as forms from './index';
import { initialHighlight, moveHighlight, optionStateText, resolveKey, SingleChoiceSelect } from './single-choice-select';
import type { SingleChoiceMove, SingleChoiceOption, SingleChoiceSelectProps } from './single-choice-select';
import { candidateOptions, replacedOptions, reviewOptions, selectCopy, sourceOptions } from './single-choice-select.fixtures';

const base: SingleChoiceSelectProps = { label: 'Source', options: sourceOptions, value: 'refund-ledger', onChange: () => {} };
const render = (props: Partial<SingleChoiceSelectProps> = {}) => renderToStaticMarkup(<SingleChoiceSelect {...base} {...props} />);
const triggerOf = (html: string) => /<button[^>]*>/.exec(html)![0];
const attribute = (tag: string, name: string) => new RegExp(` ${name}="([^"]*)"`).exec(tag)?.[1];
const count = (html: string, text: string) => html.split(text).length - 1;
/** Text directly inside the element carrying this id; ids are opaque, so they are never used as patterns. */
function textOf(html: string, id: string | undefined) {
  const start = id === undefined ? -1 : html.indexOf(`id="${id}"`);
  if (start < 0) return undefined;
  const open = html.indexOf('>', start);
  return html.slice(open + 1, html.indexOf('<', open));
}
const allDisabled: readonly SingleChoiceOption[] = sourceOptions.map(option => ({ ...option, disabled: true }));

describe('OPE-317:controlled-select markup and state', () => {
  it('renders one named, non-submitting combobox trigger that owns a labelled, closed listbox', () => {
    const html = render({ helper: selectCopy.sourceHelper });
    const trigger = triggerOf(html);
    assert.equal(count(html, '<button'), 1);
    assert.equal(attribute(trigger, 'type'), 'button');
    assert.equal(attribute(trigger, 'role'), 'combobox');
    assert.equal(attribute(trigger, 'aria-haspopup'), 'listbox');
    assert.equal(attribute(trigger, 'aria-expanded'), 'false');
    assert.equal(attribute(trigger, 'aria-activedescendant'), undefined);
    assert.equal(textOf(html, attribute(trigger, 'aria-labelledby')), 'Source');
    assert.equal(textOf(html, attribute(trigger, 'aria-describedby')), selectCopy.sourceHelper);
    const list = /<div[^>]*role="listbox"[^>]*>/.exec(html)![0];
    assert.equal(attribute(list, 'id'), attribute(trigger, 'aria-controls'));
    assert.equal(attribute(list, 'aria-labelledby'), attribute(trigger, 'aria-labelledby'));
    assert.ok((list)?.includes(' hidden=""'));
    assert.ok((html)?.includes('data-highlighted') === false);
  });

  it('shows the controlled value by its option label and marks only that option selected', () => {
    const html = render({ value: 'policy-registry' });
    assert.ok((html)?.includes('<span class="ot-single-choice-value">Policy registry</span>'));
    assert.equal(count(html, 'aria-selected="true"'), 1);
    assert.equal(count(html, 'role="option"'), 3);
    assert.ok((/<div[^>]*aria-selected="true"[^>]*>/.exec(html)![0])?.includes('aria-disabled') === false);
    assert.ok((html)?.includes('>Policy registry</span><span class="ot-single-choice-option-state">Selected · policy authority</span>'));
    assert.ok((html)?.includes('>Refund ledger</span><span class="ot-single-choice-option-state">Available · completion authority</span>'));
  });

  it('matches the native option copy, including the visible reason on the disabled choice', () => {
    const html = render();
    assert.ok((html)?.includes('>Refund ledger</span><span class="ot-single-choice-option-state">Selected · completion authority</span>'));
    assert.ok((html)?.includes('>Policy registry</span><span class="ot-single-choice-option-state">Available · policy authority</span>'));
    assert.ok((html)?.includes('>Operator notes</span><span class="ot-single-choice-option-state">Disabled · not independent truth</span>'));
    assert.equal(count(html, 'aria-disabled="true"'), 1);
    assert.ok((/<div[^>]*aria-disabled="true"[^>]*>/.exec(html)![0])?.includes('aria-selected="false"'));
  });

  testCases([null, 'revoked-source', ''])('shows no choice for the unmatched value %j and never substitutes one', value => {
    const html = render({ value, placeholder: 'Choose a source' });
    assert.ok((html)?.includes('<span class="ot-single-choice-value">Choose a source</span>'));
    assert.ok((html)?.includes('aria-selected="true"') === false);
  });

  it('types the decorative caret after the generic placeholder in every control state (native 12:39 to 12:55)', () => {
    const generic = '<span class="ot-single-choice-value">Select a value</span><span aria-hidden="true" class="ot-single-choice-caret">  ▾</span></button>';
    for (const html of [render({ value: null }), render({ value: null, error: selectCopy.error }),
      render({ value: null, options: [], disabled: true, disabledReason: selectCopy.disabledReason }),
      render({ value: 'revoked-source' })]) {
      assert.ok((html)?.includes(generic));
      assert.equal(count(html, '▾'), 1);
      assert.equal(count(html, 'aria-hidden'), 1);
    }
  });

  it('shows a chosen value as its label alone, with no caret (native 31:38)', () => {
    assert.ok((render())?.includes('<span class="ot-single-choice-value">Refund ledger</span></button>'));
    for (const html of [render(), render({ placeholderCaret: true }), render({ value: 'policy-registry', error: selectCopy.error })]) {
      assert.ok((html)?.includes('▾') === false);
      assert.ok((html)?.includes('ot-single-choice-caret') === false);
      assert.ok((html)?.includes('aria-hidden') === false);
    }
  });

  it('shows the contextual error and disabled values without a caret (native 166:9963)', () => {
    const required = render({ label: selectCopy.errorLabel, placeholder: selectCopy.errorPlaceholder, error: selectCopy.error,
      options: candidateOptions, value: null });
    const unavailable = render({ label: selectCopy.disabledLabel, placeholder: selectCopy.disabledPlaceholder, disabled: true,
      disabledReason: selectCopy.disabledReason, options: [], value: null });
    assert.ok((required)?.includes('<span class="ot-single-choice-value">Choose an eligible artifact</span></button>'));
    assert.ok((unavailable)?.includes('<span class="ot-single-choice-value">No eligible artifacts</span></button>'));
    for (const html of [required, unavailable]) {
      assert.ok((html)?.includes('▾') === false);
      assert.ok((html)?.includes('aria-hidden') === false);
    }
  });

  it('lets the caller set the caret for a placeholder, and never makes it part of the name or the value', () => {
    const on = render({ value: null, placeholder: 'Choose a source', placeholderCaret: true });
    assert.ok((on)?.includes('<span class="ot-single-choice-value">Choose a source</span><span aria-hidden="true" class="ot-single-choice-caret">  ▾</span></button>'));
    const off = render({ value: null, placeholderCaret: false });
    assert.ok((off)?.includes('<span class="ot-single-choice-value">Select a value</span></button>'));
    assert.ok((off)?.includes('▾') === false);
    for (const html of [on, off]) assert.equal(textOf(html, attribute(triggerOf(html), 'aria-labelledby')), 'Source');
  });

  it('links the error message, marks the control invalid and shows it in place of the helper', () => {
    const html = render({ value: null, helper: selectCopy.helper, error: selectCopy.error });
    const trigger = triggerOf(html);
    assert.equal(attribute(trigger, 'aria-invalid'), 'true');
    assert.equal(textOf(html, attribute(trigger, 'aria-describedby')), selectCopy.error);
    assert.ok((html)?.includes('ot-single-choice--invalid'));
    assert.ok((html)?.includes(selectCopy.helper) === false);
    assert.ok((trigger)?.includes(' disabled=""') === false);
  });

  it('keeps a valid control free of error semantics and omits an absent description', () => {
    const withHelper = triggerOf(render({ helper: selectCopy.helper, error: '   ' }));
    assert.equal(attribute(withHelper, 'aria-invalid'), undefined);
    assert.ok((withHelper)?.includes('aria-describedby='));
    const bare = render();
    assert.equal(attribute(triggerOf(bare), 'aria-describedby'), undefined);
    assert.ok((bare)?.includes('ot-single-choice-support') === false);
    assert.ok((bare)?.includes('ot-single-choice--invalid') === false);
  });

  it('uses native disabled semantics with a linked visible reason and no error state', () => {
    const html = render({ value: null, options: [], disabled: true, placeholder: selectCopy.disabledPlaceholder,
      helper: selectCopy.helper, error: selectCopy.error, disabledReason: selectCopy.disabledReason });
    const trigger = triggerOf(html);
    assert.ok((trigger)?.includes(' disabled=""'));
    assert.equal(attribute(trigger, 'aria-invalid'), undefined);
    assert.equal(attribute(trigger, 'aria-expanded'), 'false');
    assert.equal(textOf(html, attribute(trigger, 'aria-describedby')), selectCopy.disabledReason);
    assert.ok((html)?.includes('ot-single-choice--disabled'));
    assert.ok((html)?.includes('ot-single-choice--invalid') === false);
    assert.ok((html)?.includes(selectCopy.error) === false);
    assert.ok((html)?.includes('role="option"') === false);
  });

  it('gives independent instances distinct ids for every relationship', () => {
    const html = renderToStaticMarkup(<><SingleChoiceSelect {...base} helper="First" /><SingleChoiceSelect {...base} label="Review source" options={reviewOptions} helper="Second" /></>);
    const ids = [...html.matchAll(/ id="([^"]+)"/g)].map(match => match[1]);
    assert.equal((ids)?.length, 2 * 3 + sourceOptions.length + reviewOptions.length);
    assert.equal(new Set(ids).size, ids.length);
    const triggers = [...html.matchAll(/<button[^>]*>/g)].map(match => match[0]);
    assert.deepEqual(triggers.map(tag => textOf(html, attribute(tag, 'aria-labelledby'))), ['Source', 'Review source']);
    assert.deepEqual(triggers.map(tag => textOf(html, attribute(tag, 'aria-describedby'))), ['First', 'Second']);
    assert.notEqual(attribute(triggers[0], 'aria-controls'), attribute(triggers[1], 'aria-controls'));
  });

  it('follows a replaced option list without keeping a stale label or selection', () => {
    assert.ok((render({ options: reviewOptions }))?.includes('<span class="ot-single-choice-value">Refund ledger</span>'));
    const replaced = render({ options: replacedOptions });
    assert.ok((replaced)?.includes('<span class="ot-single-choice-value">Select a value</span>'));
    assert.ok((replaced)?.includes('Refund ledger') === false);
    assert.ok((replaced)?.includes('aria-selected="true"') === false);
    assert.equal(count(replaced, 'role="option"'), replacedOptions.length);
  });

  it('escapes supplied text and adds no interactive content of its own', () => {
    const html = render({ label: '<img src=x onerror=alert(1)>', helper: '<a href="#">help</a>',
      options: [{ value: 'raw', label: '<b>Raw</b>', detail: '<script>detail</script>' }], value: 'raw' });
    assert.ok((html)?.includes('&lt;img src=x onerror=alert(1)&gt;'));
    assert.ok((html)?.includes('&lt;b&gt;Raw&lt;/b&gt;'));
    assert.ok((html)?.includes('&lt;script&gt;detail&lt;/script&gt;'));
    assert.doesNotMatch(html, /<(img|a|b|script|input|select)[\s>]/);
    assert.ok((html)?.includes('tabindex=') === false);
  });

  testCases<[string, Partial<SingleChoiceSelectProps>]>([
    ['select label', { label: '  ' }],
    ['select placeholder', { placeholder: '' }],
    ['change handler', { onChange: undefined as unknown as SingleChoiceSelectProps['onChange'] }],
    ['text or null', { value: 3 as unknown as string }],
    ['list of options', { options: undefined as unknown as SingleChoiceOption[] }],
    ['option value', { options: [{ value: ' ', label: 'Blank identity' }] }],
    ['option label', { options: [{ value: 'unnamed', label: '' }] }],
    ['must be unique', { options: [{ value: 'same', label: 'One' }, { value: 'same', label: 'Two' }] }],
  ])('refuses an invalid contract: %s', (message, props) => {
    assert.throws(() => render(props), message);
  });

  it('exports the control from the forms module', () => {
    assert.equal(forms.SingleChoiceSelect, SingleChoiceSelect);
    assert.deepEqual(forms.singleChoiceOptionStates, { selected: 'Selected', available: 'Available', disabled: 'Disabled' });
  });
});

describe('OPE-317:keyboard-pointer highlight rules', () => {
  // reviewOptions: 0 Refund ledger, 1 Operator notes (disabled), 2 Policy registry, 3 Audit export.
  testCases<[number, SingleChoiceMove, number]>([
    [0, 'next', 2], [2, 'next', 3], [3, 'next', 3], [3, 'previous', 2], [2, 'previous', 0], [0, 'previous', 0],
    [1, 'next', 2], [1, 'previous', 0], [-1, 'next', 0], [-1, 'previous', 3], [2, 'first', 0], [0, 'last', 3],
  ])('moves from %i with %s to %i, skipping the disabled choice and never wrapping', (from, move, expected) => {
    assert.equal(moveHighlight(reviewOptions, from, move), expected);
  });

  it('never lands on a trailing disabled choice and reports no target when nothing can be chosen', () => {
    assert.equal(moveHighlight(sourceOptions, 1, 'next'), 1);
    assert.equal(moveHighlight(sourceOptions, 0, 'last'), 1);
    for (const move of ['first', 'last', 'next', 'previous'] as const) {
      assert.equal(moveHighlight(allDisabled, 0, move), -1);
      assert.equal(moveHighlight([], -1, move), -1);
    }
  });

  it('opens on the current choice when it can be chosen, otherwise on the first choice that can', () => {
    assert.equal(initialHighlight(reviewOptions, 'policy-registry'), 2);
    assert.equal(initialHighlight(reviewOptions, 'operator-notes'), 0);
    assert.equal(initialHighlight(reviewOptions, null), 0);
    assert.equal(initialHighlight(reviewOptions, 'revoked-source'), 0);
    assert.equal(initialHighlight([{ value: 'held', label: 'Held', disabled: true }, ...candidateOptions], null), 1);
    assert.equal(initialHighlight(allDisabled, 'refund-ledger'), -1);
  });

  testCases(['Enter', ' ', 'ArrowDown', 'ArrowUp'])('opens a closed list with %j on the current choice without a request', key => {
    assert.deepEqual(resolveKey(key, false, -1, reviewOptions, 'policy-registry'), { open: true, highlight: 2, choose: -1, handled: true });
  });

  it('opens a closed list at either end with Home and End', () => {
    assert.deepEqual(resolveKey('Home', false, -1, reviewOptions, 'policy-registry'), { open: true, highlight: 0, choose: -1, handled: true });
    assert.deepEqual(resolveKey('End', false, -1, reviewOptions, 'policy-registry'), { open: true, highlight: 3, choose: -1, handled: true });
  });

  testCases(['Escape', 'Tab', 'a', 'Shift'])('leaves a closed list and the key %j alone', key => {
    assert.deepEqual(resolveKey(key, false, -1, reviewOptions, 'policy-registry'), { open: false, highlight: -1, choose: -1, handled: false });
  });

  testCases(['Enter', ' ', 'ArrowDown', 'Home'])('has nothing to open with %j when no options are supplied', key => {
    assert.deepEqual(resolveKey(key, false, -1, [], null), { open: false, highlight: -1, choose: -1, handled: false });
  });

  testCases<[string, number, number]>([['ArrowDown', 0, 2], ['ArrowDown', 3, 3], ['ArrowUp', 2, 0], ['ArrowUp', 0, 0], ['Home', 3, 0], ['End', 0, 3]])(
    'moves an open list with %s from %i to %i and requests nothing', (key, from, to) => {
      assert.deepEqual(resolveKey(key, true, from, reviewOptions, 'refund-ledger'), { open: true, highlight: to, choose: -1, handled: true });
    });

  testCases(['Enter', ' '])('requests the highlighted enabled choice with %j and closes', key => {
    assert.deepEqual(resolveKey(key, true, 2, reviewOptions, 'refund-ledger'), { open: false, highlight: -1, choose: 2, handled: true });
  });

  testCases(['Enter', ' '])('blocks %j on a disabled or absent highlight: the list closes and nothing is requested', key => {
    assert.deepEqual(resolveKey(key, true, 1, reviewOptions, 'refund-ledger'), { open: false, highlight: -1, choose: -1, handled: true });
    assert.deepEqual(resolveKey(key, true, -1, allDisabled, 'refund-ledger'), { open: false, highlight: -1, choose: -1, handled: true });
    assert.deepEqual(resolveKey(key, true, 9, reviewOptions, 'refund-ledger'), { open: false, highlight: -1, choose: -1, handled: true });
  });

  it('dismisses with Escape as a consumed key and with Tab as an unconsumed one, never with a request', () => {
    assert.deepEqual(resolveKey('Escape', true, 2, reviewOptions, 'refund-ledger'), { open: false, highlight: -1, choose: -1, handled: true });
    assert.deepEqual(resolveKey('Tab', true, 2, reviewOptions, 'refund-ledger'), { open: false, highlight: -1, choose: -1, handled: false });
  });

  it('ignores every other key while open', () => {
    for (const key of ['a', 'Shift', 'PageDown', 'F2']) {
      assert.deepEqual(resolveKey(key, true, 2, reviewOptions, 'refund-ledger'), { open: true, highlight: 2, choose: -1, handled: false });
    }
  });

  it('never requests a value from any key other than Enter or Space', () => {
    const keys = ['ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape', 'Tab', 'PageUp', 'PageDown', 'a', 'Shift'];
    for (const key of keys) for (const open of [true, false]) for (const highlight of [-1, 0, 1, 2, 3]) {
      assert.equal(resolveKey(key, open, highlight, reviewOptions, 'refund-ledger').choose, -1);
    }
  });
});

describe('OPE-317 option state text', () => {
  it('states selection, availability and a disabled reason in words', () => {
    const [refund, policy, notes] = sourceOptions;
    assert.equal(optionStateText(refund, true), 'Selected · completion authority');
    assert.equal(optionStateText(policy, false), 'Available · policy authority');
    assert.equal(optionStateText(notes, false), 'Disabled · not independent truth');
    assert.equal(optionStateText(notes, true), 'Selected · Disabled · not independent truth');
  });

  it('keeps the state word when no detail or reason is supplied', () => {
    assert.equal(optionStateText({ value: 'plain', label: 'Plain' }, false), 'Available');
    assert.equal(optionStateText({ value: 'plain', label: 'Plain', detail: '  ' }, true), 'Selected');
    assert.equal(optionStateText({ value: 'held', label: 'Held', disabled: true }, false), 'Disabled');
    assert.equal(optionStateText({ value: 'held', label: 'Held', disabled: true, detail: 'awaiting review' }, false), 'Disabled · awaiting review');
  });
});
