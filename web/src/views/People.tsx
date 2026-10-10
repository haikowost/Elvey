import { useMemo, useState } from 'react';
import { Avatar, KycChip } from '../components/bits';
import UpdateButton from '../components/UpdateButton';
import { useStore } from '../store';

// harvest order (src/priority.py): Key tiers, then P1..P3; everything else after
const TIER_ORDER = ['Key: Internal', 'Key: Supplier', 'Key: BD', 'Key: Competitor', 'Internal/Competitor (curated)',
  'P1 Key', 'P2 Active', 'P3 Reference', 'P4 Low relevance', 'P4 Noise / generic', 'Excluded'];
const tierRank = (t?: string | null) => { const i = TIER_ORDER.indexOf(t ?? ''); return i < 0 ? 99 : i; };
const LIMIT = 300;

/** Everyone, in KYC priority order, with a per-person "Update from LinkedIn" button. */
export default function People() {
  const nodes = useStore((s) => s.nodes);
  const byId = useStore((s) => s.byId);
  const employerOf = useStore((s) => s.employerOf);
  const select = useStore((s) => s.select);
  const [q, setQ] = useState('');
  const [tier, setTier] = useState('');
  const [pendingOnly, setPendingOnly] = useState(false);

  const people = useMemo(() => nodes.filter((n) => n.type === 'person')
    .sort((a, b) => tierRank(a.kycTier) - tierRank(b.kycTier) || (b.kycScore ?? -1) - (a.kycScore ?? -1) || a.label.localeCompare(b.label)), [nodes]);
  const tiers = useMemo(() => [...new Set(people.map((p) => p.kycTier).filter(Boolean) as string[])].sort((a, b) => tierRank(a) - tierRank(b)), [people]);
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return people.filter((p) => (!tier || p.kycTier === tier) && (!pendingOnly || p.kyc !== 'done') &&
      (!needle || `${p.label} ${p.sub ?? ''} ${byId.get(employerOf.get(p.id) ?? '')?.label ?? ''}`.toLowerCase().includes(needle)));
  }, [people, q, tier, pendingOnly, byId, employerOf]);

  return (
    <div className="people-view">
      <div className="people-bar glass">
        <input className="select" type="search" placeholder="Search people, role, company…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search people" />
        <select className="select" value={tier} onChange={(e) => setTier(e.target.value)} aria-label="KYC tier">
          <option value="">All tiers</option>
          {tiers.map((t) => <option key={t}>{t}</option>)}
        </select>
        <label className="muted"><input type="checkbox" checked={pendingOnly} onChange={(e) => setPendingOnly(e.target.checked)} /> KYC pending only</label>
        <span className="muted">{shown.length} people{shown.length > LIMIT ? ` · first ${LIMIT} shown` : ''}</span>
      </div>
      <ul className="people-list">
        {shown.slice(0, LIMIT).map((p) => {
          const emp = byId.get(employerOf.get(p.id) ?? '');
          return (
            <li key={p.id} className="people-row glass">
              <button className="people-who" onClick={() => select(p.id, { dossier: true })}>
                <Avatar n={p} />
                <span>
                  <b>{p.label}</b>
                  <span className="muted">{[...new Set([p.sub, emp?.label].filter(Boolean))].join(' · ')}</span>
                </span>
              </button>
              <span className="people-tier">{p.kycTier ?? '—'}{p.kycScore != null ? <span className="muted"> · {p.kycScore}</span> : null}</span>
              <KycChip n={p} />
              <UpdateButton nodeId={p.id} small />
            </li>
          );
        })}
      </ul>
    </div>
  );
}
