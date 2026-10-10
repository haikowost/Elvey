import { useEffect, useMemo, useState } from 'react';
import { useStore } from '../store';
import { GROUP_COLOR, GROUP_LABEL, REL_COLOR, REL_LABEL, RELS } from '../theme';
import type { Group, View } from '../types';
import KycProgress from './KycProgress';
import Lenses from './Lenses';
import SavedViews from './SavedViews';

// Spider first (the default, flat and easy to navigate); the 3D graph stays available but last.
export const VIEWS: { key: View; label: string; icon: string; help: string }[] = [
  { key: 'spider', label: 'Spider', icon: '✳', help: 'Opens centred on Elvey. Rings group neighbours by relationship; click any spoke to re-centre, the breadcrumb (or ⌂) takes you back.' },
  { key: 'org', label: 'Org', icon: '⊤', help: 'Our side → integrators → end-users → consultants → competitors. Click a card to expand it.' },
  { key: 'people', label: 'People', icon: '☰', help: 'Everyone in KYC priority order (Key tiers first). ↻ Update from LinkedIn refreshes one person now — no Claude tokens.' },
  { key: 'investigate', label: 'Investigate', icon: '▤', help: 'Pick an account: its reps, opportunities, projects and linked consultants / end-users.' },
  { key: 'graph', label: '3D graph', icon: '◉', help: 'Everything at once. Drag to orbit, scroll to zoom; hover for the insight card, click for the dossier.' },
];

const PICKER_ORDER: Group[] = ['elvey', 'customer', 'supplier', 'enduser', 'consultant', 'competitor'];

export default function Rail() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const nodes = useStore((s) => s.nodes);
  const selectedId = useStore((s) => s.selectedId);
  const employerOf = useStore((s) => s.employerOf);
  const select = useStore((s) => s.select);
  const setPalette = useStore((s) => s.setPalette);
  const source = useStore((s) => s.source);
  const narrow = useNarrow();
  const [collapsed, setCollapsed] = useState(narrow);
  useEffect(() => setCollapsed(narrow), [narrow]);

  const companies = useMemo(() => {
    const by: Partial<Record<Group, typeof nodes>> = {};
    for (const n of nodes) if (n.type === 'company') (by[n.group] ??= []).push(n);
    for (const g of Object.values(by)) g!.sort((a, b) => b.size - a.size || a.label.localeCompare(b.label));
    return by;
  }, [nodes]);
  const accountValue = selectedId ? (nodes.find((n) => n.id === selectedId)?.type === 'company' ? selectedId : employerOf.get(selectedId) ?? '') : '';
  const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

  return (
    <nav className={`rail${collapsed ? ' collapsed' : ''}`} aria-label="Deal Intelligence navigation">
      <div className="brand">
        <div className="brand-mark">E</div>
        <div className="brand-name rail-text">ELVEY<small>Deal Intelligence</small></div>
        {source === 'seed' && !collapsed ? <span className="demo-pill" title="Illustrative demo dataset — not real deals">DEMO DATA</span> : null}
        <button className="icon-btn" style={{ marginLeft: source === 'seed' && !collapsed ? 4 : 'auto' }}
          onClick={() => setCollapsed(!collapsed)} aria-label={collapsed ? 'Expand menu' : 'Collapse menu'}>{collapsed ? '»' : '«'}</button>
      </div>

      <button className="search-btn" onClick={() => setPalette(true)} aria-label="Search">
        <span>⌕</span><span className="rail-text">Search…</span>
        <kbd className="rail-text">{isMac ? '⌘' : 'Ctrl'} K</kbd>
      </button>

      <section>
        <h4 className="rail-h">Views</h4>
        <div className="views" role="radiogroup" aria-label="View">
          {VIEWS.map((v) => (
            <button key={v.key} className="view-btn" role="radio" aria-checked={view === v.key} title={v.label}
              onClick={() => setView(v.key)}>
              <span className="view-ico">{v.icon}</span><span className="rail-text">{v.label}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="rail-section-body">
        <h4 className="rail-h">Account</h4>
        <select className="select" aria-label="Account picker" value={accountValue}
          onChange={(e) => e.target.value && select(e.target.value, { view: view === 'graph' ? 'investigate' : view })}>
          <option value="">Pick an account…</option>
          {PICKER_ORDER.filter((g) => companies[g]?.length).map((g) => (
            <optgroup key={g} label={GROUP_LABEL[g]}>
              {companies[g]!.map((n) => <option key={n.id} value={n.id}>{n.label}</option>)}
            </optgroup>
          ))}
        </select>
      </section>

      <div className="rail-section-body"><KycProgress /></div>
      <div className="rail-section-body"><Lenses /></div>
      <div className="rail-section-body"><SavedViews /></div>

      <section className="rail-section-body">
        <h4 className="rail-h">Legend</h4>
        <div className="legend">
          {(Object.keys(GROUP_COLOR) as Group[]).map((g) => (
            <span key={g}><span className="dot" style={{ color: GROUP_COLOR[g] }} />{GROUP_LABEL[g]}</span>
          ))}
        </div>
        <div className="legend" style={{ marginTop: 8 }}>
          {RELS.map((r) => <span key={r}><span className="bar" style={{ color: REL_COLOR[r] }} />{REL_LABEL[r]}</span>)}
        </div>
        <div className="help">
          {VIEWS.map((v) => <div key={v.key}><b>{v.label}:</b> {v.help}</div>)}
        </div>
      </section>
    </nav>
  );
}

function useNarrow() {
  const q = '(max-width: 900px)';
  const [narrow, setNarrow] = useState(() => window.matchMedia(q).matches);
  useEffect(() => {
    const m = window.matchMedia(q);
    const on = () => setNarrow(m.matches);
    m.addEventListener('change', on);
    return () => m.removeEventListener('change', on);
  }, []);
  return narrow;
}
