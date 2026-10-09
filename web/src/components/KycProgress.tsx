import { useEffect, useState } from 'react';
import { api } from '../api';
import type { KycProgress as Progress } from '../types';

const pct = (n: number, d: number) => (d ? Math.round((100 * n) / d) : 0);

function Bar({ label, n, of }: { label: string; n: number; of: number }) {
  return (
    <div className="kyc-row" title={`${n} of ${of}`}>
      <span>{label}</span><b>{n}</b>
      <div className="kyc-bar" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={of} aria-valuenow={n}>
        <i style={{ width: `${pct(n, of)}%` }} />
      </div>
    </div>
  );
}

/** Did the LinkedIn harvest actually run? Live counts from /api/kyc/progress (refreshes every minute). */
export default function KycProgress() {
  const [p, setP] = useState<Progress | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => api.progress().then((x) => live && (setP(x), setErr(null))).catch((e) => live && setErr(String(e)));
    load();
    const t = window.setInterval(load, 60_000);
    return () => { live = false; window.clearInterval(t); };
  }, []);
  if (err) return null; // seed/demo mode or an older server: just leave the panel out
  if (!p) return null;
  const run = p.last_harvest_run;
  const ageDays = run ? (Date.now() - new Date(run.at).getTime()) / 864e5 : Infinity;
  return (
    <section className="kyc" data-testid="kyc-progress" aria-label="KYC progress">
      <h4 className="rail-h">KYC progress</h4>
      <Bar label="Role known" n={p.role_known} of={p.contacts} />
      <Bar label="LinkedIn URL" n={p.linkedin_url} of={p.contacts} />
      <Bar label="Photo" n={p.photo} of={p.contacts} />
      {p.tiers.map((t) => <Bar key={t.tier} label={t.label} n={t.enriched} of={t.total} />)}
      <div className={`kyc-run${ageDays > 2 ? ' stale' : ''}`}>
        {run ? <>Last harvest {new Date(run.at).toLocaleString('en-ZA')}{run.stopped ? ` — stopped: ${run.stopped}` : ''}</> : 'Harvest has never run'}
        <br />{p.contacts} contacts · today {p.used_today}/{p.daily_cap} · {p.queued} queued
      </div>
    </section>
  );
}
