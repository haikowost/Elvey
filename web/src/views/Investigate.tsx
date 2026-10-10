import { type ReactNode, useEffect, useState } from 'react';
import { api, photoUrl } from '../api';
import { Avatar, KycChip, OppBadge, ZohoChip } from '../components/bits';
import { Empty } from '../components/Empty';
import { useMatcher, useStore } from '../store';
import { GROUP_LABEL } from '../theme';
import type { Brief, Investigation, Opp } from '../types';

function RowTools({ id }: { id: string }) {
  const select = useStore((s) => s.select);
  return (
    <span className="row-tools">
      <button className="btn small" title="Centre in Spider" aria-label="Spider" onClick={() => select(id, { view: 'spider' })}>✳</button>
      <button className="btn small" title="Show in Spider" aria-label="Spider" onClick={() => select(id, { view: 'spider' })}>✳</button>
      <button className="btn small" title="Details" aria-label="Details" onClick={() => select(id, { dossier: true })}>ⓘ</button>
    </span>
  );
}

function EntityRow({ b, note }: { b: Brief; note?: ReactNode }) {
  const setHover = useStore((s) => s.setHover);
  return (
    <div className="row-item" data-id={b.id}
      onMouseEnter={(e) => {
        const r = e.currentTarget.getBoundingClientRect();
        const m = e.currentTarget.closest('.main')!.getBoundingClientRect();
        setHover({ id: b.id, x: r.right - m.left, y: r.top - m.top + 10 }); // right of the row, clear of its tools
      }}
      onMouseLeave={() => setHover(null)}>
      <img src={photoUrl(b.id)} alt="" loading="lazy" />
      <div className="grow">
        <div>{b.label}</div>
        <div className="muted" style={{ fontSize: 11.5 }}>{note ?? b.sub ?? GROUP_LABEL[b.group]}</div>
      </div>
      <KycChip n={b} />
      <OppBadge count={b.oppCount} />
      <RowTools id={b.id} />
    </div>
  );
}

function OppRow({ o }: { o: Opp }) {
  return (
    <div className="opp-row">
      <div><div>{o.t}</div><div className="muted" style={{ fontSize: 11 }}>{[o.p, o.via ? `via ${o.via}` : 'direct'].filter(Boolean).join(' · ')}</div></div>
      {o.val ? <span className="val">{o.val}</span> : null}
    </div>
  );
}

function Card({ title, count, children, empty }: { title: string; count?: number; children: ReactNode; empty: string }) {
  const has = Array.isArray(children) ? children.length > 0 : !!children;
  return (
    <section className="card glass" aria-label={title}>
      <h3>{title}{count !== undefined ? <span>· {count}</span> : null}</h3>
      {has ? children : <div className="muted">{empty}</div>}
    </section>
  );
}

export default function Investigate() {
  const selectedId = useStore((s) => s.selectedId);
  const byId = useStore((s) => s.byId);
  const employerOf = useStore((s) => s.employerOf);
  const match = useMatcher();
  const sel = selectedId ? byId.get(selectedId) : undefined;
  const accountId = sel?.type === 'company' ? sel.id : sel ? employerOf.get(sel.id) : undefined;
  const [inv, setInv] = useState<Investigation | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!accountId) return;
    let live = true;
    setErr(null);
    api.investigate(accountId).then((x) => live && setInv(x)).catch((e) => live && setErr(String(e)));
    return () => { live = false; };
  }, [accountId]);

  if (!accountId) {
    return <Empty title={sel ? `${sel.label} isn't linked to an account` : 'Pick an account'}
      text="Choose a company in the rail's account picker (or search with Ctrl/Cmd+K) to see its reps, opportunities, projects and linked consultants / end-users." />;
  }
  if (err) return <Empty title="Couldn't load this account" text={err} />;
  if (!inv || inv.account.id !== accountId) return <Empty title="Loading…" text="" />;

  const keep = <T extends Brief>(xs: T[]) => xs.filter((x) => match(byId.get(x.id)));
  const a = inv.account;
  const s = inv.stats;
  return (
    <div className="investigate" data-testid="investigate">
      <div className="inv-head">
        <Avatar n={a} large />
        <div>
          <h1>{a.label}</h1>
          <div className="muted">{a.sub ?? GROUP_LABEL[a.group]}{a.region ? ` · ${a.region}` : ''}{a.brands.length ? ` · ${a.brands.join(', ')}` : ''}</div>
          <div className="status-chips">
            <ZohoChip zoho={a.zoho} />
            <span className="zchip out">Elvey owner: {inv.owner ?? 'unassigned'}</span>
          </div>
        </div>
        <span style={{ marginLeft: 'auto' }}><RowTools id={a.id} /></span>
      </div>

      <div className="stats">
        {([['contacts', s.contacts], ['projects', s.projects], ['linked consultants', s.consultants],
          ['linked end-users', s.endusers], ['open opps', s.opportunities]] as const).map(([l, n]) => (
          <div className="stat glass" key={l} data-stat={l}>
            <div className="n" style={l === 'open opps' && n ? { color: 'var(--opp)' } : undefined}>{n}</div>
            <div className="l">{l}</div>
          </div>
        ))}
      </div>

      <div className="cards">
        <Card title="Contacts & reps" count={s.contacts} empty="No contacts captured yet.">
          {keep(inv.contacts).map((c) => <EntityRow key={c.id} b={c} />)}
        </Card>
        <Card title="Opportunities" count={s.opportunities} empty="No open opportunities recorded.">
          {inv.opportunities.map((o, i) => <OppRow key={i} o={o} />)}
        </Card>
        <Card title="Projects" count={s.projects} empty="Not linked to any project yet.">
          {keep(inv.projects).map((p) => <EntityRow key={p.id} b={p} note={[p.sub, p.brands.join(', ')].filter(Boolean).join(' · ')} />)}
        </Card>
        <Card title="Linked consultants / specifiers" count={s.consultants} empty="No specifier on a shared project.">
          {keep(inv.consultants).map((c) => <EntityRow key={c.id} b={c} note={`specifies ${c.via}`} />)}
        </Card>
        <Card title="Linked end-users" count={s.endusers} empty="No end-user linked through a shared project.">
          {keep(inv.endusers).map((e) => <EntityRow key={e.id} b={e} note={e.via === 'serves' ? 'served directly' : `via ${e.via}`} />)}
        </Card>
        {inv.competitors.length ? (
          <Card title="Competitors on shared projects" count={inv.competitors.length} empty="">
            {keep(inv.competitors).map((c) => <EntityRow key={c.id} b={c} note={`on ${c.via}`} />)}
          </Card>
        ) : null}
        <Card title="Suggested engagement" empty="Nothing obvious to do next.">
          {inv.plays.length ? <ul className="plays">{inv.plays.map((p) => <li key={p}>{p}</li>)}</ul> : null}
        </Card>
        {a.facts.length ? (
          <Card title="Key facts" empty="">
            <ul className="plays">{a.facts.map((f) => <li key={f}>{f}</li>)}</ul>
          </Card>
        ) : null}
      </div>
    </div>
  );
}
