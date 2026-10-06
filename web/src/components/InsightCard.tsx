import { useEffect, useRef, useState } from 'react';
import { useStore } from '../store';
import { GROUP_LABEL } from '../theme';
import { Avatar, KycChip, OppBadge, ZohoChip } from './bits';

/** Compact hover card anchored to a node (§6.5). Lightweight: built from the graph payload only;
 *  the heavy dossier loads on click. Stays open while the pointer is over it, so its actions work. */
export default function InsightCard() {
  const hover = useStore((s) => s.hover);
  const byId = useStore((s) => s.byId);
  const employerOf = useStore((s) => s.employerOf);
  const select = useStore((s) => s.select);
  const setHover = useStore((s) => s.setHover);
  const [shown, setShown] = useState(hover);
  const overCard = useRef(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (hover) { setShown(hover); return; }
    const t = setTimeout(() => { if (!overCard.current) setShown(null); }, 350);
    return () => clearTimeout(t);
  }, [hover]);

  const n = shown ? byId.get(shown.id) : undefined;
  if (!shown || !n) return null;

  const parent = ref.current?.parentElement ?? document.querySelector<HTMLElement>('.main');
  const w = parent?.clientWidth ?? 1200;
  const h = parent?.clientHeight ?? 800;
  // beside the anchor, never over it: flip to the left when the right side would overflow
  const CARD = 264;
  const left = shown.x + 26 + CARD <= w - 8 ? shown.x + 26 : Math.max(8, shown.x - 26 - CARD);
  const top = Math.min(Math.max(8, shown.y - 30), h - 220);
  const employer = n.type === 'person' ? byId.get(employerOf.get(n.id) ?? '') : undefined;
  const investigateId = n.type === 'company' ? n.id : employer?.id;
  const headline = n.facts[0] ?? (n.opps[0]?.t ? `Opportunity: ${n.opps[0].t}` : undefined);
  const close = () => { overCard.current = false; setShown(null); setHover(null); };

  return (
    <div ref={ref} className="insight glass" style={{ left, top }} role="dialog" aria-label={`${n.label} insight`}
      onMouseEnter={() => { overCard.current = true; }}
      onMouseLeave={() => { overCard.current = false; setShown(null); }}>
      <div className="insight-head">
        <Avatar n={n} />
        <div>
          <div className="insight-name">{n.label}</div>
          <div className="insight-sub">{n.sub ?? GROUP_LABEL[n.group]}</div>
        </div>
      </div>
      {headline ? <div className="insight-fact">{headline}</div> : null}
      {n.type === 'person' && n.kyc !== 'done' && !n.facts.length ? <div className="insight-fact muted">KYC pending — not enriched yet</div> : null}
      <div className="status-chips">
        <ZohoChip zoho={n.zoho} />
        <KycChip n={n} />
        <OppBadge count={n.oppCount} />
      </div>
      <div className="actions">
        {investigateId ? (
          <button className="btn" onClick={() => { close(); select(investigateId, { view: 'investigate' }); }}>Investigate</button>
        ) : null}
        <button className="btn" onClick={() => { close(); select(n.id, { view: 'spider' }); }}>Center here</button>
        <button className="btn" onClick={() => { close(); select(n.id, { dossier: true }); }}>Details</button>
      </div>
    </div>
  );
}
