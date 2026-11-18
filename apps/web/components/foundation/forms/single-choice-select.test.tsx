import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
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
    expect(count(html, '<button')).toBe(1);
    expect(attribute(trigger, 'type')).toBe('button');
    expect(attribute(trigger, 'role')).toBe('combobox');
    expect(attribute(trigger, 'aria-haspopup')).toBe('listbox');
    expect(attribute(trigger, 'aria-expanded')).toBe('false');
    expect(attribute(trigger, 'aria-activedescendant')).toBeUndefined();
    expect(textOf(html, attribute(trigger, 'aria-labelledby'))).toBe('Source');
    expect(textOf(html, attribute(trigger, 'aria-describedby'))).toBe(selectCopy.sourceHelper);
    const list = /<div[^>]*role="listbox"[^>]*>/.exec(html)![0];
    expect(attribute(list, 'id')).toBe(attribute(trigger, 'aria-controls'));
    expect(attribute(list, 'aria-labelledby')).toBe(attribute(trigger, 'aria-labelledby'));
    expect(list).toContain(' hidden=""');
    expect(html).not.toContain('data-highlighted');
  });

  it('shows the controlled value by its option label and marks only that option selected', () => {
    const html = render({ value: 'policy-registry' });
    expect(html).toContain('<span class="ot-single-choice-value">Policy registry</span>');
    expect(count(html, 'aria-selected="true"')).toBe(1);
    expect(count(html, 'role="option"')).toBe(3);
    expect(/<div[^>]*aria-selected="true"[^>]*>/.exec(html)![0]).not.toContain('aria-disabled');
    expect(html).toContain('>Policy registry</span><span class="ot-single-choice-option-state">Selected · policy authority</span>');
    expect(html).toContain('>Refund ledger</span><span class="ot-single-choice-option-state">Available · completion authority</span>');
  });

  it('matches the native option copy, including the visible reason on the disabled choice', () => {
    const html = render();
    expect(html).toContain('>Refund ledger</span><span class="ot-single-choice-option-state">Selected · completion authority</span>');
    expect(html).toContain('>Policy registry</span><span class="ot-single-choice-option-state">Available · policy authority</span>');
    expect(html).toContain('>Operator notes</span><span class="ot-single-choice-option-state">Disabled · not independent truth</span>');
    expect(count(html, 'aria-disabled="true"')).toBe(1);
    expect(/<div[^>]*aria-disabled="true"[^>]*>/.exec(html)![0]).toContain('aria-selected="false"');
  });

  it.each([null, 'revoked-source', ''])('shows no choice for the unmatched value %j and never substitutes one', value => {
    const html = render({ value, placeholder: 'Choose a source' });
    expect(html).toContain('<span class="ot-single-choice-value">Choose a source</span>');
    expect(html).not.toContain('aria-selected="true"');
  });

  it('types the decorative caret after the generic placeholder in every control state (native 12:39 to 12:55)', () => {
    const generic = '<span class="ot-single-choice-value">Select a value</span><span aria-hidden="true" class="ot-single-choice-caret">  ▾</span></button>';
    for (const html of [render({ value: null }), render({ value: null, error: selectCopy.error }),
      render({ value: null, options: [], disabled: true, disabledReason: selectCopy.disabledReason }),
      render({ value: 'revoked-source' })]) {
      expect(html).toContain(generic);
      expect(count(html, '▾')).toBe(1);
      expect(count(html, 'aria-hidden')).toBe(1);
    }
  });

  it('shows a chosen value as its label alone, with no caret (native 31:38)', () => {
    expect(render()).toContain('<span class="ot-single-choice-value">Refund ledger</span></button>');
    for (const html of [render(), render({ placeholderCaret: true }), render({ value: 'policy-registry', error: selectCopy.error })]) {
      expect(html).not.toContain('▾');
      expect(html).not.toContain('ot-single-choice-caret');
      expect(html).not.toContain('aria-hidden');
    }
  });

  it('shows the contextual error and disabled values without a caret (native 166:9963)', () => {
    const required = render({ label: selectCopy.errorLabel, placeholder: selectCopy.errorPlaceholder, error: selectCopy.error,
      options: candidateOptions, value: null });
    const unavailable = render({ label: selectCopy.disabledLabel, placeholder: selectCopy.disabledPlaceholder, disabled: true,
      disabledReason: selectCopy.disabledReason, options: [], value: null });
    expect(required).toContain('<span class="ot-single-choice-value">Choose an eligible artifact</span></button>');
    expect(unavailable).toContain('<span class="ot-single-choice-value">No eligible artifacts</span></button>');
    for (const html of [required, unavailable]) {
      expect(html).not.toContain('▾');
      expect(html).not.toContain('aria-hidden');
    }
  });

  it('lets the caller set the caret for a placeholder, and never makes it part of the name or the value', () => {
    const on = render({ value: null, placeholder: 'Choose a source', placeholderCaret: true });
    expect(on).toContain('<span class="ot-single-choice-value">Choose a source</span><span aria-hidden="true" class="ot-single-choice-caret">  ▾</span></button>');
    const off = render({ value: null, placeholderCaret: false });
    expect(off).toContain('<span class="ot-single-choice-value">Select a value</span></button>');
    expect(off).not.toContain('▾');
    for (const html of [on, off]) expect(textOf(html, attribute(triggerOf(html), 'aria-labelledby'))).toBe('Source');
  });

  it('links the error message, marks the control invalid and shows it in place of the helper', () => {
    const html = render({ value: null, helper: selectCopy.helper, error: selectCopy.error });
    const trigger = triggerOf(html);
    expect(attribute(trigger, 'aria-invalid')).toBe('true');
    expect(textOf(html, attribute(trigger, 'aria-describedby'))).toBe(selectCopy.error);
    expect(html).toContain('ot-single-choice--invalid');
    expect(html).not.toContain(selectCopy.helper);
    expect(trigger).not.toContain(' disabled=""');
  });

  it('keeps a valid control free of error semantics and omits an absent description', () => {
    const withHelper = triggerOf(render({ helper: selectCopy.helper, error: '   ' }));
    expect(attribute(withHelper, 'aria-invalid')).toBeUndefined();
    expect(withHelper).toContain('aria-describedby=');
    const bare = render();
    expect(attribute(triggerOf(bare), 'aria-describedby')).toBeUndefined();
    expect(bare).not.toContain('ot-single-choice-support');
    expect(bare).not.toContain('ot-single-choice--invalid');
  });

  it('uses native disabled semantics with a linked visible reason and no error state', () => {
    const html = render({ value: null, options: [], disabled: true, placeholder: selectCopy.disabledPlaceholder,
      helper: selectCopy.helper, error: selectCopy.error, disabledReason: selectCopy.disabledReason });
    const trigger = triggerOf(html);
    expect(trigger).toContain(' disabled=""');
    expect(attribute(trigger, 'aria-invalid')).toBeUndefined();
    expect(attribute(trigger, 'aria-expanded')).toBe('false');
    expect(textOf(html, attribute(trigger, 'aria-describedby'))).toBe(selectCopy.disabledReason);
    expect(html).toContain('ot-single-choice--disabled');
    expect(html).not.toContain('ot-single-choice--invalid');
    expect(html).not.toContain(selectCopy.error);
    expect(html).not.toContain('role="option"');
  });

  it('gives independent instances distinct ids for every relationship', () => {
    const html = renderToStaticMarkup(<><SingleChoiceSelect {...base} helper="First" /><SingleChoiceSelect {...base} label="Review source" options={reviewOptions} helper="Second" /></>);
    const ids = [...html.matchAll(/ id="([^"]+)"/g)].map(match => match[1]);
    expect(ids).toHaveLength(2 * 3 + sourceOptions.length + reviewOptions.length);
    expect(new Set(ids).size).toBe(ids.length);
    const triggers = [...html.matchAll(/<button[^>]*>/g)].map(match => match[0]);
    expect(triggers.map(tag => textOf(html, attribute(tag, 'aria-labelledby')))).toEqual(['Source', 'Review source']);
    expect(triggers.map(tag => textOf(html, attribute(tag, 'aria-describedby')))).toEqual(['First', 'Second']);
    expect(attribute(triggers[0], 'aria-controls')).not.toBe(attribute(triggers[1], 'aria-controls'));
  });

  it('follows a replaced option list without keeping a stale label or selection', () => {
    expect(render({ options: reviewOptions })).toContain('<span class="ot-single-choice-value">Refund ledger</span>');
    const replaced = render({ options: replacedOptions });
    expect(replaced).toContain('<span class="ot-single-choice-value">Select a value</span>');
    expect(replaced).not.toContain('Refund ledger');
    expect(replaced).not.toContain('aria-selected="true"');
    expect(count(replaced, 'role="option"')).toBe(replacedOptions.length);
  });

  it('escapes supplied text and adds no interactive content of its own', () => {
    const html = render({ label: '<img src=x onerror=alert(1)>', helper: '<a href="#">help</a>',
      options: [{ value: 'raw', label: '<b>Raw</b>', detail: '<script>detail</script>' }], value: 'raw' });
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
    expect(html).toContain('&lt;b&gt;Raw&lt;/b&gt;');
    expect(html).toContain('&lt;script&gt;detail&lt;/script&gt;');
    expect(html).not.toMatch(/<(img|a|b|script|input|select)[\s>]/);
    expect(html).not.toContain('tabindex=');
  });

  it.each<[string, Partial<SingleChoiceSelectProps>]>([
    ['select label', { label: '  ' }],
    ['select placeholder', { placeholder: '' }],
    ['change handler', { onChange: undefined as unknown as SingleChoiceSelectProps['onChange'] }],
    ['text or null', { value: 3 as unknown as string }],
    ['list of options', { options: undefined as unknown as SingleChoiceOption[] }],
    ['option value', { options: [{ value: ' ', label: 'Blank identity' }] }],
    ['option label', { options: [{ value: 'unnamed', label: '' }] }],
    ['must be unique', { options: [{ value: 'same', label: 'One' }, { value: 'same', label: 'Two' }] }],
  ])('refuses an invalid contract: %s', (message, props) => {
    expect(() => render(props)).toThrow(message);
  });

  it('exports the control from the forms module', () => {
    expect(forms.SingleChoiceSelect).toBe(SingleChoiceSelect);
    expect(forms.singleChoiceOptionStates).toEqual({ selected: 'Selected', available: 'Available', disabled: 'Disabled' });
  });
});

describe('OPE-317:keyboard-pointer highlight rules', () => {
  // reviewOptions: 0 Refund ledger, 1 Operator notes (disabled), 2 Policy registry, 3 Audit export.
  it.each<[number, SingleChoiceMove, number]>([
    [0, 'next', 2], [2, 'next', 3], [3, 'next', 3], [3, 'previous', 2], [2, 'previous', 0], [0, 'previous', 0],
    [1, 'next', 2], [1, 'previous', 0], [-1, 'next', 0], [-1, 'previous', 3], [2, 'first', 0], [0, 'last', 3],
  ])('moves from %i with %s to %i, skipping the disabled choice and never wrapping', (from, move, expected) => {
    expect(moveHighlight(reviewOptions, from, move)).toBe(expected);
  });

  it('never lands on a trailing disabled choice and reports no target when nothing can be chosen', () => {
    expect(moveHighlight(sourceOptions, 1, 'next')).toBe(1);
    expect(moveHighlight(sourceOptions, 0, 'last')).toBe(1);
    for (const move of ['first', 'last', 'next', 'previous'] as const) {
      expect(moveHighlight(allDisabled, 0, move)).toBe(-1);
      expect(moveHighlight([], -1, move)).toBe(-1);
    }
  });

  it('opens on the current choice when it can be chosen, otherwise on the first choice that can', () => {
    expect(initialHighlight(reviewOptions, 'policy-registry')).toBe(2);
    expect(initialHighlight(reviewOptions, 'operator-notes')).toBe(0);
    expect(initialHighlight(reviewOptions, null)).toBe(0);
    expect(initialHighlight(reviewOptions, 'revoked-source')).toBe(0);
    expect(initialHighlight([{ value: 'held', label: 'Held', disabled: true }, ...candidateOptions], null)).toBe(1);
    expect(initialHighlight(allDisabled, 'refund-ledger')).toBe(-1);
  });

  it.each(['Enter', ' ', 'ArrowDown', 'ArrowUp'])('opens a closed list with %j on the current choice without a request', key => {
    expect(resolveKey(key, false, -1, reviewOptions, 'policy-registry')).toEqual({ open: true, highlight: 2, choose: -1, handled: true });
  });

  it('opens a closed list at either end with Home and End', () => {
    expect(resolveKey('Home', false, -1, reviewOptions, 'policy-registry')).toEqual({ open: true, highlight: 0, choose: -1, handled: true });
    expect(resolveKey('End', false, -1, reviewOptions, 'policy-registry')).toEqual({ open: true, highlight: 3, choose: -1, handled: true });
  });

  it.each(['Escape', 'Tab', 'a', 'Shift'])('leaves a closed list and the key %j alone', key => {
    expect(resolveKey(key, false, -1, reviewOptions, 'policy-registry')).toEqual({ open: false, highlight: -1, choose: -1, handled: false });
  });

  it.each(['Enter', ' ', 'ArrowDown', 'Home'])('has nothing to open with %j when no options are supplied', key => {
    expect(resolveKey(key, false, -1, [], null)).toEqual({ open: false, highlight: -1, choose: -1, handled: false });
  });

  it.each<[string, number, number]>([['ArrowDown', 0, 2], ['ArrowDown', 3, 3], ['ArrowUp', 2, 0], ['ArrowUp', 0, 0], ['Home', 3, 0], ['End', 0, 3]])(
    'moves an open list with %s from %i to %i and requests nothing', (key, from, to) => {
      expect(resolveKey(key, true, from, reviewOptions, 'refund-ledger')).toEqual({ open: true, highlight: to, choose: -1, handled: true });
    });

  it.each(['Enter', ' '])('requests the highlighted enabled choice with %j and closes', key => {
    expect(resolveKey(key, true, 2, reviewOptions, 'refund-ledger')).toEqual({ open: false, highlight: -1, choose: 2, handled: true });
  });

  it.each(['Enter', ' '])('blocks %j on a disabled or absent highlight: the list closes and nothing is requested', key => {
    expect(resolveKey(key, true, 1, reviewOptions, 'refund-ledger')).toEqual({ open: false, highlight: -1, choose: -1, handled: true });
    expect(resolveKey(key, true, -1, allDisabled, 'refund-ledger')).toEqual({ open: false, highlight: -1, choose: -1, handled: true });
    expect(resolveKey(key, true, 9, reviewOptions, 'refund-ledger')).toEqual({ open: false, highlight: -1, choose: -1, handled: true });
  });

  it('dismisses with Escape as a consumed key and with Tab as an unconsumed one, never with a request', () => {
    expect(resolveKey('Escape', true, 2, reviewOptions, 'refund-ledger')).toEqual({ open: false, highlight: -1, choose: -1, handled: true });
    expect(resolveKey('Tab', true, 2, reviewOptions, 'refund-ledger')).toEqual({ open: false, highlight: -1, choose: -1, handled: false });
  });

  it('ignores every other key while open', () => {
    for (const key of ['a', 'Shift', 'PageDown', 'F2']) {
      expect(resolveKey(key, true, 2, reviewOptions, 'refund-ledger')).toEqual({ open: true, highlight: 2, choose: -1, handled: false });
    }
  });

  it('never requests a value from any key other than Enter or Space', () => {
    const keys = ['ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape', 'Tab', 'PageUp', 'PageDown', 'a', 'Shift'];
    for (const key of keys) for (const open of [true, false]) for (const highlight of [-1, 0, 1, 2, 3]) {
      expect(resolveKey(key, open, highlight, reviewOptions, 'refund-ledger').choose).toBe(-1);
    }
  });
});

describe('OPE-317 option state text', () => {
  it('states selection, availability and a disabled reason in words', () => {
    const [refund, policy, notes] = sourceOptions;
    expect(optionStateText(refund, true)).toBe('Selected · completion authority');
    expect(optionStateText(policy, false)).toBe('Available · policy authority');
    expect(optionStateText(notes, false)).toBe('Disabled · not independent truth');
    expect(optionStateText(notes, true)).toBe('Selected · Disabled · not independent truth');
  });

  it('keeps the state word when no detail or reason is supplied', () => {
    expect(optionStateText({ value: 'plain', label: 'Plain' }, false)).toBe('Available');
    expect(optionStateText({ value: 'plain', label: 'Plain', detail: '  ' }, true)).toBe('Selected');
    expect(optionStateText({ value: 'held', label: 'Held', disabled: true }, false)).toBe('Disabled');
    expect(optionStateText({ value: 'held', label: 'Held', disabled: true, detail: 'awaiting review' }, false)).toBe('Disabled · awaiting review');
  });
});
