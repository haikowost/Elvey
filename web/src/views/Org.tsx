import { hierarchy, tree } from 'd3-hierarchy';
import { type CSSProperties, useEffect, useMemo, useRef, useState } from 'react';
import { photoUrl } from '../api';
import { OppBadge } from '../components/bits';
import { useMatcher, useStore } from '../store';
import { GROUP_COLOR, GROUP_LABEL } from '../theme';
import type { GLink, GNode, Group } from '../types';

interface ONode {
  key: string;
  kind: 'root' | 'tier' | 'entity' | 'more';
  label: string;
  id?: string;
  group?: Group;
  tier?: Group;
  count: number;          // how many children it has (shown on the card)
  expandable: boolean;
  children?: ONode[];
}

const TIERS: { group: Group; label: string }[] = [
  { group: 'elvey', label: 'Our side' },
  { group: 'customer', label: 'Integrators' },
  { group: 'supplier', label: 'Suppliers' },
  { group: 'enduser', label: 'End-users' },
  { group: 'consultant', label: 'Consultants' },
  { group: 'competitor', label: 'Competitors' },
];
const TIER_CAP = 30;
const CARD_W = 176;
const TYPE_ORDER = { company: 0, person: 1, project: 2, product: 3 } as const;

type Adj = Map<string, { rel: GLink['rel']; other: string; dir: 'in' | 'out' }[]>;

/** What expanding an entity reveals: sub-companies, its people, its projects, the end-users it serves. */
function childIds(id: string, adj: Adj, byId: Map<string, GNode>, parentId?: string): string[] {
  const n = byId.get(id);
  if (!n) return [];
  const out = new Set<string>();
  for (const e of adj.get(id) ?? []) {
    if (e.other === parentId) continue;
    const o = byId.get(e.other);
    if (!o) continue;
    if (n.type === 'company') {
      if ((e.rel === 'part_of' || e.rel === 'works_at') && e.dir === 'in') out.add(e.other);
      else if (o.type === 'project' && ['involved_in', 'specifies', 'opportunity'].includes(e.rel)) out.add(e.other);
      else if (e.rel === 'serves' && e.dir === 'out') out.add(e.other);
    } else if (n.type === 'project') {
      if (o.type !== 'project' && ['involved_in', 'specifies', 'opportunity'].includes(e.rel)) out.add(e.other);
    }
  }
  return [...out].sort((a, b) => {
    const x = byId.get(a)!;
    const y = byId.get(b)!;
    return TYPE_ORDER[x.type] - TYPE_ORDER[y.type] || y.size - x.size || x.label.localeCompare(y.label);
  });
}

export default function Org() {
  const nodes = useStore((s) => s.nodes);
  const links = useStore((s) => s.links);
  const byId = useStore((s) => s.byId);
  const selectedId = useStore((s) => s.selectedId);
  const select = useStore((s) => s.select);
  const setHover = useStore((s) => s.setHover);
  const hiddenTypes = useStore((s) => s.lens.hiddenTypes);
  const match = useMatcher();
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(['root', ...TIERS.map((t) => `root/tier:${t.group}`)]));
  const [showAll, setShowAll] = useState<Set<string>>(() => new Set());
  const box = useRef<HTMLDivElement>(null);

  const adj = useMemo(() => {
    const m: Adj = new Map();
    const add = (a: string, e: { rel: GLink['rel']; other: string; dir: 'in' | 'out' }) => m.set(a, [...(m.get(a) ?? []), e]);
    for (const l of links) { add(l.source, { rel: l.rel, other: l.target, dir: 'out' }); add(l.target, { rel: l.rel, other: l.source, dir: 'in' }); }
    return m;
  }, [links]);

  const data = useMemo(() => {
    const entity = (id: string, path: string, parentId?: string): ONode => {
      const n = byId.get(id)!;
      const key = `${path}/${id}`;
      const kids = childIds(id, adj, byId, parentId).filter((k) => match(byId.get(k)));
      const node: ONode = { key, kind: 'entity', id, label: n.label, group: n.group, count: kids.length, expandable: kids.length > 0 };
      if (expanded.has(key) && key.split('/').length < 9) node.children = kids.map((k) => entity(k, key, id));
      return node;
    };
    const tiers: ONode[] = [];
    for (const t of TIERS) {
      // a company that's part_of another company of the same tier is shown under its parent instead
      const members = nodes
        .filter((n) => n.type === 'company' && n.group === t.group && match(n))
        .filter((n) => !(adj.get(n.id) ?? []).some((e) => e.rel === 'part_of' && e.dir === 'out' && byId.get(e.other)?.group === t.group))
        .sort((a, b) => b.size - a.size || b.oppCount - a.oppCount || a.label.localeCompare(b.label));
      if (!members.length || hiddenTypes.includes(t.group)) continue;
      const key = `root/tier:${t.group}`;
      const capped = showAll.has(key) ? members : members.slice(0, TIER_CAP);
      const tier: ONode = { key, kind: 'tier', label: t.label, tier: t.group, group: t.group, count: members.length, expandable: true };
      if (expanded.has(key)) {
        tier.children = capped.map((m) => entity(m.id, key));
        if (capped.length < members.length) {
          tier.children.push({ key: `${key}/more`, kind: 'more', label: `+${members.length - capped.length} more`, group: t.group, count: 0, expandable: false });
        }
      }
      tiers.push(tier);
    }
    return { key: 'root', kind: 'root', label: 'Deal network', count: tiers.length, expandable: true, children: tiers } as ONode;
  }, [nodes, byId, adj, expanded, showAll, match, hiddenTypes]);

  const lay = useMemo(() => {
    const root = tree<ONode>().nodeSize([CARD_W + 20, 112])(hierarchy(data, (d) => d.children));
    // the synthetic root only ties the tiers together: the tiers themselves are the top row
    const all = root.descendants().filter((d) => d.depth > 0);
    const minX = Math.min(...all.map((d) => d.x));
    const maxX = Math.max(...all.map((d) => d.x));
    const maxY = Math.max(...all.map((d) => d.y));
    const dx = -minX + CARD_W / 2 + 30;
    return { all, links: root.links().filter((l) => l.source.depth > 0), dx, w: maxX - minX + CARD_W + 60, h: maxY + 150 };
  }, [data]);

  // expand the tier and the card of whatever is selected elsewhere, then bring it into view
  useEffect(() => {
    const n = selectedId ? byId.get(selectedId) : undefined;
    if (!n || n.type !== 'company') return;
    const tierKey = `root/tier:${n.group}`;
    setExpanded((s) => (s.has(tierKey) ? s : new Set([...s, tierKey])));
  }, [selectedId, byId]);
  useEffect(() => {
    if (!selectedId) return;
    const el = box.current?.querySelector(`[data-id="${CSS.escape(selectedId)}"]`);
    el?.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
  }, [selectedId, lay]);

  const toggle = (key: string) => setExpanded((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n; });
  const top = 64 - 112; // tiers (depth 1) start just under the top bar

  return (
    <div className="org" ref={box} data-testid="org">
      <div className="org-canvas" style={{ width: lay.w, height: lay.h + top }}>
        <svg width={lay.w} height={lay.h + top}>
          <defs><filter id="org-glow" filterUnits="userSpaceOnUse" x={0} y={0} width={lay.w} height={lay.h + top + 200}><feGaussianBlur stdDeviation="2.2" result="b" />
            <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge></filter></defs>
          {lay.links.map((l) => {
            const x1 = l.source.x + lay.dx, y1 = l.source.y + top + 58, x2 = l.target.x + lay.dx, y2 = l.target.y + top;
            const c = l.target.data.group ? GROUP_COLOR[l.target.data.group] : '#8fa3c8';
            return <path key={l.target.data.key} d={`M ${x1} ${y1} C ${x1} ${(y1 + y2) / 2}, ${x2} ${(y1 + y2) / 2}, ${x2} ${y2}`}
              fill="none" stroke={c} strokeOpacity={0.55} strokeWidth={1.4} filter="url(#org-glow)" />;
          })}
        </svg>
        {lay.all.map((d) => {
          const o = d.data;
          const style = { left: d.x + lay.dx - CARD_W / 2, top: d.y + top, '--c': o.group ? GROUP_COLOR[o.group] : '#8fa3c8' } as CSSProperties;
          if (o.kind !== 'entity') {
            return (
              <div key={o.key} className="ocard tier" style={style} data-key={o.key}
                onClick={() => (o.kind === 'more' ? setShowAll((s) => new Set([...s, o.key.replace(/\/more$/, '')])) : toggle(o.key))}>
                {o.label}{o.kind === 'tier' ? <div className="sb" style={{ textTransform: 'none', letterSpacing: 0 }}>{o.count} {expanded.has(o.key) ? '▾' : '▸'}</div> : null}
              </div>
            );
          }
          const n = byId.get(o.id!)!;
          const isOpen = expanded.has(o.key);
          return (
            <div key={o.key} className={`ocard${selectedId === n.id ? ' selected' : ''}`} style={style} data-id={n.id} data-key={o.key}
              role="button" aria-expanded={o.expandable ? isOpen : undefined} aria-label={n.label}
              onMouseEnter={(e) => {
                const r = e.currentTarget.getBoundingClientRect();
                const m = e.currentTarget.closest('.main')!.getBoundingClientRect();
                setHover({ id: n.id, x: r.right - m.left - 8, y: r.top - m.top + 20 });
              }}
              onMouseLeave={() => setHover(null)}
              onClick={() => {
                if (o.expandable) { toggle(o.key); select(n.id); } else select(n.id, { view: 'spider' });
              }}>
              <div className="row">
                <img src={photoUrl(n.id)} alt="" loading="lazy" />
                <div style={{ minWidth: 0 }}>
                  <div className="nm">{n.label}</div>
                  <div className="sb">{n.sub ?? GROUP_LABEL[n.group]}</div>
                </div>
              </div>
              <div className="meta">
                {o.expandable ? <span className="count">{isOpen ? '▾' : '▸'} {o.count}</span> : null}
                <OppBadge count={n.oppCount} />
                <span className="tools">
                  <button className="btn small" title="Centre in Spider" aria-label={`Spider ${n.label}`}
                    onClick={(e) => { e.stopPropagation(); select(n.id, { view: 'spider' }); }}>✳</button>
                  {n.type === 'company' ? (
                    <button className="btn small" title="Investigate" aria-label={`Investigate ${n.label}`}
                      onClick={(e) => { e.stopPropagation(); select(n.id, { view: 'investigate' }); }}>▤</button>
                  ) : null}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
