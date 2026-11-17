import React from 'react';
import { createHash } from 'node:crypto';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { StatusCopy } from './status-copy';
import { statusFixtures, toastVariants, type StatusCopyProps } from './status-copy.types';

// Digests pin independent native title/body/tone triples, including punctuation.
const approved = {
  'UI-TX-SAVE/dirty_valid': 'ac72bce97d9acf162ff6bbbbd8990c78ec0368eac1acabbfdebf1227c865a963',
  'UI-TX-SAVE/submitting': '630dc80a914cbeb61bd5991baab7e5d2a485b3896baad0ca1906424eac1ad3a6',
  'UI-TX-SAVE/saved': '0cd5280c2e4de69806af6cab9b2e6945748486a9b6a4f8ff1f25819b9ac5d667',
  'UI-TX-SAVE/failed': 'db3646cac89e2c2a5be6b6378fa5fd214db6cf0623a391e89eb3e2615135f092',
  'UI-TX-SAVE/conflict': 'bec47ec3aadf7a1cb6911892fc2ccafcc377084e9149f2d0c7cacb4c83b60eab',
  'UI-TX-CANCEL/running': 'a0265d724342d4037580761be4527f4306de19e428735791ea49db8e64aff1d5',
  'UI-TX-CANCEL/cancel_requested': 'b2143882ab50dc722f81a30d0d57a287443c551111aed21e253715994e5c8f22',
  'UI-TX-CANCEL/stopping': '77c8f8479c3a10ee77309a050ac092608dd47bca455659164ac5f9874158dee1',
  'UI-TX-CANCEL/cancelled': 'c4436a7307990301a160b1428ed3e5e96e74e121e179cf76a97fee1d4ab0c693',
  'UI-TX-CANCEL/outcome_unknown': 'b94f3af50bd7fd76195566d0d5a94736765d358b9b7467ab4be68625316357ec',
  'UI-TX-REVOKE/control_blocked': '4b15b339c3442b6a1c1aec00ed7ff822e51ea73d27d98c04e23c1419afc3302c',
  'UI-TX-REVOKE/pending_cells': 'a066df65a466c29897311bab741549e5f392121dacd365ef42f4dff219319ce0',
  'UI-TX-REVOKE/draining': 'ee70240f8c46395ce8f52e7c9029f43c57a34253c4e905c9024d962e3f9ea8f7',
  'UI-TX-REVOKE/confirmed': '901e0b36e1eef7ad5b28b8e8f5fe6149d1873b99e597de5dbed41998cc1bcf66',
  'UI-TX-REVOKE/reconciliation_required': 'f3289f40287a0a4303ba872d1b780053ed835434d631cd7bf88f30d00634b426',
  'UI-TX-PROMOTE/hold': '529d484bc109b4f84cc610afa91d979497cf3ec00c18cac119c743e83ff2959d',
  'UI-TX-PROMOTE/authorized': 'b1705b18358a226d56908676ab05a15ec6882cb3de3f186efcb24e5f752d0d79',
  'UI-TX-PROMOTE/staging': '57947e09c80b473434cf4c103afe4179ab9c5330a1a8dacd8eb69023eae63727',
  'UI-TX-PROMOTE/promotion_requested': 'af3b43c6b5dbb1174f418da5465e6e3e6d839228f448270cc7e4f6ca89ac88f8',
  'UI-TX-PROMOTE/verifying_traffic': 'a2dd650dd14782174c8553a3e19b763b0fcc5cc2dd5d45e18ec5801fe17d9dc7',
  'UI-TX-PROMOTE/active': '67d99c0f61e70cccdad8198882e002332f744d697918d4d43ed5f3498fa30f7c',
  'UI-TX-PROMOTE/runtime_unknown': '455e03babde54de513aabfc5104807b30545e70c897e340ed5b77a1f9ba2487a',
  'UI-TX-DELETE/preview': '163a5c1a5676dbfbf27887f2a55b04e711453f8d593a626da1c4c0213f10a782',
  'UI-TX-DELETE/requested': '527969cd2b49307d4aea191f0e0221509350ed6bd6c42b2c75fbb6c8af718199',
  'UI-TX-DELETE/partial': 'fe191f3f7c765d38dffada7e3b8eb89a25f2de775243241271f4cecf021163d7',
  'UI-TX-DELETE/confirmed': '69b57c97f318a1fc18723428ec0110ce8ad4c41e2acc94ac0cc4a82c73d276ea',
  'UI-TX-DELETE/unverifiable': '470c5c475f5e4047b7a022e3861dd7bb3f892da8497abebef6ff6a08d8cd8c7e',
  'UI-ARTIFACTS-A01/ready': '0667caf6ccda21d29e5ae667055d815fe79b609376fe9a8abe20c29c8dad787c',
  'UI-ARTIFACTS-A01/pending': '80b3b940899bb4e256b8fa8b47b41ded77f61ae34860badf8e39f2bc2daae86c',
  'UI-ARTIFACTS-A01/confirmed': 'f3ec864cc9b40ade680ea13f04c92e4872e476c3095a9766699a9eca1bc5d468',
  'UI-ARTIFACTS-A01/refused': '46a7412334d6682266e72e894741d16fe3fb37ca332fefee22f762eca853bdab',
  'UI-ARTIFACTS-A01/failure': '1e5a6b4c085607788fd95e195b08120c350f1854b1f89371349af7bd37f95bad',
  'UI-ARTIFACTS-A01/unknown': '0102d0be5d9ad59f6ead0c3d4b182171d2267f0ee4eda497c0aa2b5e59ddcae2',
  'UI-ARTIFACTS-A01/recovery': '638b12d12e2115757902971eb9185b5ae10323f537694d87673980b533ade098',
} as const;
describe('finite status copy', () => {
  it('exposes exactly the reviewed fixture set', () => {
    expect(Object.keys(statusFixtures).sort()).toEqual(Object.keys(approved).sort());
  });
  for (const fixture of Object.keys(approved) as (keyof typeof approved)[]) {
    it(fixture, () => {
      const html = renderToStaticMarkup(<StatusCopy presentation="notice" fixture={fixture} />);
      const parts = /^<div role="status" class="ot-notice ot-notice--([a-z]+)"><p class="ot-notice-title">([^<]+)<\/p><p class="ot-notice-body">([^<]+)<\/p><\/div>$/.exec(html);
      expect(parts).not.toBeNull();
      const [, tone, title, body] = parts!;
      expect(createHash('sha256').update([title, body, tone].join('\0')).digest('hex')).toBe(approved[fixture]);
    });
  }
  for (const fixture of Object.keys(toastVariants) as (keyof typeof toastVariants)[]) {
    it('renders the supplementary toast for ' + fixture, () => {
      const html = renderToStaticMarkup(<StatusCopy presentation="toast" fixture={fixture} />);
      expect(html).toContain('role="status"');
      expect(html).toContain('ot-toast--' + toastVariants[fixture]);
      expect(html).toContain(statusFixtures[fixture].body);
    });
  }
  it('allows the explicit action-required failure alert', () => {
    expect(renderToStaticMarkup(<StatusCopy presentation="toast" fixture="UI-TX-SAVE/failed" urgent />)).toContain('role="alert"');
  });
  it.each([
    { presentation: 'modal', fixture: 'UI-TX-SAVE/saved' },
    { presentation: 'notice', fixture: 'toString' },
    { presentation: 'notice', fixture: 'UI-TX-SAVE/failed', urgent: true },
    { presentation: 'toast', fixture: 'UI-TX-SAVE/saved', urgent: true },
    { presentation: 'toast', fixture: 'UI-TX-PROMOTE/active' },
  ])('rejects an unsupported presentation %#', props => {
    expect(() => renderToStaticMarkup(<StatusCopy {...props as StatusCopyProps} />)).toThrow();
  });
});
