import { useEffect, useState } from 'react';
import { api } from '../api';
import { useStore } from '../store';
import { GROUP_LABEL, REL_COLOR, REL_LABEL } from '../theme';
import type { Dossier as D } from '../types';
import { Avatar, Dot, KycChip, OppBadge, ZohoChip } from './bits';

export default function Dossier() {
  const id = useStore((s) => s.selectedId);
  const open = useStore((s) => s.dossierOpen);
  const openDossier = useStore((s) => s.openDossier);
  const select = useStore((s) => s.select);
  const [d, setD] = useState<D | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !id) return;
    let live = true;
    setErr(null);
    api.entity(id).then((x) => live && setD(x)).catch((e) => live && setErr(String(e)));
    return () => { live = false; };
  }, [id, open]);

  if (!open || !id) return null;
  if (err) return <aside className="dossier glass"><button className="icon-btn close" onClick={() => openDossier(false)}>✕</button><p className="err">{err}</p></aside>;
  if (!d || d.node.id !== id) return <aside className="dossier glass"><p className="muted">Loading…</p></aside>;

  const n = d.node;
  const e = d.enrichment;
  return (
    <aside className="dossier glass" aria-label={`${n.label} dossier`}>
      <button className="icon-btn close" onClick={() => openDossier(false)} aria-label="Close">✕</button>
      <div className="dossier-head">
        <Avatar n={n} large />
        <div>
          <h2>{n.label}</h2>
          <div className="muted">{n.sub ?? GROUP_LABEL[n.group]}</div>
          <div className="status-chips"><ZohoChip zoho={n.zoho} /><KycChip n={n} /><OppBadge count={n.oppCount} /></div>
        </div>
      </div>
      <div className="actions">
        <button className="btn" onClick={() => select(id, { view: 'spider' })}>Center in Spider</button>
        {d.investigable || d.employer_id ? (
          <button className="btn" onClick={() => select(d.investigable ? id : d.employer_id, { view: 'investigate' })}>
            Investigate{d.investigable ? '' : ` ${d.employer}`}
          </button>
        ) : null}
      </div>

      {n.facts.length ? (<><h3>Key facts</h3><ul className="facts">{n.facts.map((f) => <li key={f}>{f}</li>)}</ul></>) : null}

      {n.type === 'person' ? (
        <>
          <h3>LinkedIn</h3>
          {d.kyc_pending ? (
            <div className="pending">KYC pending — this contact hasn't been enriched yet. The harvester will fill in their summary and work history.</div>
          ) : (
            <>
              {e.summary ? <p>{e.summary}</p> : null}
              {e.experience?.length ? (
                <ul className="exp">{e.experience.slice(0, 5).map((x, i) => (
                  <li key={i}><b>{x.title}</b>{x.company ? ` · ${x.company}` : ''} <span className="muted mono">{x.dates}</span></li>
                ))}</ul>
              ) : null}
            </>
          )}
          {e.linkedin || e.email || e.phone ? (
            <p className="muted">
              {e.linkedin && /^https?:\/\//i.test(e.linkedin) ? <a href={e.linkedin} target="_blank" rel="noreferrer">LinkedIn profile</a> : null}
              {e.email ? <> · {e.email}</> : null}{e.phone ? <> · <span className="mono">{e.phone}</span></> : null}
            </p>
          ) : null}
        </>
      ) : null}

      {d.opportunities.length ? (
        <>
          <h3>Opportunities</h3>
          {d.opportunities.map((o, i) => (
            <div className="opp-row" key={i}>
              <div><div>{o.t}</div><div className="muted" style={{ fontSize: 11 }}>{[o.p, o.via ? `via ${o.via}` : null].filter(Boolean).join(' · ')}</div></div>
              {o.val ? <span className="val">{o.val}</span> : null}
            </div>
          ))}
        </>
      ) : null}

      {d.relationships.map((g) => (
        <div key={g.rel}>
          <h3><span style={{ color: REL_COLOR[g.rel] }}>●</span> {REL_LABEL[g.rel]} ({g.items.length})</h3>
          {g.items.map((it) => (
            <button key={`${g.rel}-${it.id}`} className="rel-row" onClick={() => select(it.id, { dossier: true })}>
              <Dot color={`var(--${it.group})`} />
              <span>{it.label}</span>
              <span className="via">{it.via ? `via ${it.via}` : typeof it.ex?.owner === 'string' ? `owner ${it.ex.owner}` : it.sub ?? ''}</span>
            </button>
          ))}
        </div>
      ))}
    </aside>
  );
}
