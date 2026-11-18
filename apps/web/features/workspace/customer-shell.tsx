'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { Badge, Button, NavItem, Tabs } from '../../components/foundation/tokens';
import { Breadcrumb, Link } from '../../components/foundation/tokens/primitives';
import type { CustomerShellProps } from './customer-shell.types';
import './customer-shell.css';

function Rail({ shell }: { shell: CustomerShellProps }) {
  return <div className="ot-shell-rail">
    <strong className="ot-shell-brand">{shell.brand}</strong>
    <div className="ot-shell-workspace">
      <strong>{shell.workspace.customer}</strong><span>{shell.workspace.scope}</span>
    </div>
    <nav aria-label="Workspace navigation" className="ot-shell-links">
      {shell.navigation.map(link => <NavItem key={link.href} {...link} current={link.href === shell.activeHref} />)}
    </nav>
    <div className="ot-shell-spacer" aria-hidden="true" />
    <nav aria-label="Workspace utilities" className="ot-shell-utilities">
      {shell.utilities.map((link, i) => <span key={link.href}>
        {i > 0 && <span aria-hidden="true"> · </span>}<Link size="small" {...link} current={link.href === shell.activeHref} />
      </span>)}
    </nav>
    <div className="ot-shell-delivery"><strong>{shell.delivery.title}</strong><span>{shell.delivery.detail}</span></div>
    <span className="ot-shell-profile">{shell.profile}</span>
  </div>;
}

export function CustomerShell(shell: CustomerShellProps) {
  const mainId = useId();
  const dialogId = useId();
  const [expanded, setExpanded] = useState(false);
  const drawer = useRef<HTMLDialogElement>(null);
  const desktopRail = useRef<HTMLElement>(null);
  // Crossing the desktop breakpoint while the mobile modal is open must release modal focus.
  useEffect(() => {
    const query = matchMedia('(min-width: 1024px)');
    function restoreDesktop() {
      if (query.matches && drawer.current?.open) {
        drawer.current.close();
        desktopRail.current?.querySelector<HTMLElement>('[aria-current="page"]')?.focus();
      }
    }
    query.addEventListener('change', restoreDesktop);
    return () => query.removeEventListener('change', restoreDesktop);
  }, []);

  return <div className="ot-shell ot-foundation">
    <a className="ot-shell-skip" href={'#' + mainId}>Skip to main content</a>
    <aside ref={desktopRail} aria-label="Workspace" className="ot-shell-sidebar"><Rail shell={shell} /></aside>
    <div className="ot-shell-application">
      <header>
        <div className="ot-shell-mobile">
          <strong className="ot-shell-brand">{shell.brand}</strong>
          <button type="button" className="ot-button ot-button--secondary" aria-haspopup="dialog" aria-controls={dialogId}
            aria-expanded={expanded} onClick={() => { drawer.current?.showModal(); setExpanded(true); }}>Open navigation</button>
          <span>{shell.workspace.customer} · {shell.context.mode}</span>
        </div>
        <div className="ot-shell-context">
          <Breadcrumb {...shell.breadcrumb} />
          <Badge tone="learning" label={shell.context.origin} /><Badge tone="info" label={shell.context.mode} />
          <span>{shell.context.cloud} · {shell.context.region}</span>
        </div>
      </header>
      <main id={mainId} tabIndex={-1} className="ot-shell-content">
        <div className="ot-shell-heading">
          <div><h1>{shell.page.title}</h1><p>{shell.page.subtitle}</p></div>
          <Button label={shell.page.primaryAction.label} onPress={shell.page.primaryAction.onPress} />
        </div>
        <Tabs {...shell.tabs} />
      </main>
    </div>
    <dialog id={dialogId} ref={drawer} onClose={() => setExpanded(false)} aria-label="Workspace navigation" className="ot-shell-drawer"
      onKeyDown={event => {
        if (event.key !== 'Tab' || event.altKey || event.ctrlKey || event.metaKey) return;
        const controls = event.currentTarget.querySelectorAll<HTMLElement>('button:enabled, a[href]');
        const first = controls[0], last = controls[controls.length - 1];
        const destination = event.shiftKey && event.target === first ? last :
          !event.shiftKey && event.target === last ? first : undefined;
        if (destination) { event.preventDefault(); destination.focus(); }
      }}
      onClick={event => { if (event.target instanceof Element && event.target.closest('a[href]')) drawer.current?.close(); }}>
      <Button label="Close navigation" variant="secondary" onPress={() => drawer.current?.close()} />
      <Rail shell={shell} />
    </dialog>
  </div>;
}
