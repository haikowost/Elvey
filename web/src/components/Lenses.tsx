import { useMemo } from 'react';
import { EMPTY_LENS, lensActive, useStore } from '../store';
import { BRANDS, REGIONS, REL_COLOR, REL_LABEL, RELS, TYPE_LENSES } from '../theme';

const toggle = <T,>(list: T[], v: T) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

/** Filters shared by every view at once: 3D dims what doesn't match, the 2D views leave it out. */
export default function Lenses() {
  const lens = useStore((s) => s.lens);
  const setLens = useStore((s) => s.setLens);
  const nodes = useStore((s) => s.nodes);

  // the brief's fixed lists, plus whatever else the data actually uses (live accounts' clusters / brand focus)
  const { regions, brands, types } = useMemo(() => {
    const r = new Set(REGIONS);
    const b = new Map<string, number>();
    const t = new Set<string>();
    for (const n of nodes) {
      if (n.region) r.add(n.region);
      for (const x of n.brands) b.set(x, (b.get(x) ?? 0) + 1);
      t.add(n.type === 'person' ? 'person' : n.group);
    }
    const extra = [...b.entries()].filter(([x]) => !BRANDS.includes(x)).sort((x, y) => y[1] - x[1]).slice(0, 8).map(([x]) => x);
    return { regions: [...r], brands: [...BRANDS, ...extra], types: t };
  }, [nodes]);

  return (
    <section aria-label="Lenses">
      <h4 className="rail-h">Lenses {lensActive(lens) ? <button onClick={() => setLens(EMPTY_LENS)}>reset</button> : null}</h4>

      <label className="switch-row">
        <input type="checkbox" checked={lens.oppsOnly} onChange={(e) => setLens({ oppsOnly: e.target.checked })} />
        <span className="opp-badge">◆</span> Opportunities only
      </label>

      <div className="muted" style={{ fontSize: 11, margin: '8px 0 4px' }}>Entity type</div>
      <div className="chips">
        {TYPE_LENSES.filter((t) => types.has(t.key)).map((t) => (
          <button key={t.key} className={`chip toggle${lens.hiddenTypes.includes(t.key) ? ' off' : ''}`}
            aria-pressed={!lens.hiddenTypes.includes(t.key)} onClick={() => setLens({ hiddenTypes: toggle(lens.hiddenTypes, t.key) })}>
            <span className="dot" style={{ color: t.color }} />{t.label}
          </button>
        ))}
      </div>

      <div className="muted" style={{ fontSize: 11, margin: '8px 0 4px' }}>Relationship</div>
      <div className="chips">
        {RELS.map((r) => (
          <button key={r} className={`chip toggle${lens.hiddenRels.includes(r) ? ' off' : ''}`}
            aria-pressed={!lens.hiddenRels.includes(r)} onClick={() => setLens({ hiddenRels: toggle(lens.hiddenRels, r) })}>
            <span className="bar" style={{ color: REL_COLOR[r] }} />{REL_LABEL[r]}
          </button>
        ))}
      </div>

      <div className="muted" style={{ fontSize: 11, margin: '8px 0 4px' }}>Region {lens.regions.length ? '' : '· all'}</div>
      <div className="chips">
        {regions.map((r) => (
          <button key={r} className={`chip toggle${lens.regions.includes(r) ? ' on' : ''}`} aria-pressed={lens.regions.includes(r)}
            onClick={() => setLens({ regions: toggle(lens.regions, r) })}>{r}</button>
        ))}
      </div>

      <div className="muted" style={{ fontSize: 11, margin: '8px 0 4px' }}>Brand / product line {lens.brands.length ? '' : '· all'}</div>
      <div className="chips">
        {brands.map((b) => (
          <button key={b} className={`chip toggle${lens.brands.includes(b) ? ' on' : ''}`} aria-pressed={lens.brands.includes(b)}
            onClick={() => setLens({ brands: toggle(lens.brands, b) })}>{b}</button>
        ))}
      </div>
    </section>
  );
}
