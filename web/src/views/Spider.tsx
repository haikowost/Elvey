import { useEffect, useMemo, useRef, useState } from 'react';
import { api, photoUrl } from '../api';
import { Empty } from '../components/Empty';
import { useMatcher, useStore } from '../store';
import { GROUP_COLOR, REL_COLOR, REL_LABEL } from '../theme';
import type { Ego, EgoNode, Rel } from '../types';
import { anchorOf, useSize } from '../useSize';

interface Placed { n: EgoNode; x: number; y: number; a: number; r: number }
const RING2_MAX = 80;

function layout(ego: Ego, keep: (n: EgoNode) => boolean) {
  const ring1 = ego.nodes.filter((n) => n.ring === 1 && keep(n));
  const kept1 = new Set(ring1.map((n) => n.id));
  const ring2 = ego.nodes
    .filter((n) => n.ring === 2 && n.parent && kept1.has(n.parent) && keep(n))
    .slice(0, RING2_MAX);
  const groups = ego.groups
    .map((g) => ({ rel: g.rel, ids: g.ids.filter((id) => kept1.has(id)) }))
    .filter((g) => g.ids.length);
  const N = ring1.length;
  const R1 = Math.max(170, Math.min(340, 60 + N * 13));
  const R2 = R1 + 150;
  const byId = new Map(ring1.map((n) => [n.id, n]));
  const weights = groups.map((g) => Math.max(g.ids.length, 1.6));
  const total = weights.reduce((a, b) => a + b, 0) || 1;
  const gap = groups.length > 1 ? 0.12 : 0;
  const usable = Math.PI * 2 - gap * groups.length;
  const placed1: Placed[] = [];
  const arcs: { rel: Rel; a0: number; a1: number; count: number }[] = [];
  let a = -Math.PI / 2;
  groups.forEach((g, gi) => {
    const span = (usable * weights[gi]) / total;
    arcs.push({ rel: g.rel, a0: a, a1: a + span, count: g.ids.length });
    g.ids.forEach((id, i) => {
      const ang = a + (span * (i + 0.5)) / g.ids.length;
      const n = byId.get(id)!;
      placed1.push({ n, a: ang, x: R1 * Math.cos(ang), y: R1 * Math.sin(ang), r: 9 + Math.min(n.oppCount, 3) + 2.4 * Math.cbrt(nodeSize(n)) });
    });
    a += span + gap;
  });
  const angleOf = new Map(placed1.map((p) => [p.n.id, p.a]));
  const kids = new Map<string, EgoNode[]>();
  for (const n of ring2) kids.set(n.parent!, [...(kids.get(n.parent!) ?? []), n]);
  const step = (Math.PI * 2) / Math.max(N, 1);
  const placed2: Placed[] = [];
  for (const [parent, list] of kids) {
    const base = angleOf.get(parent)!;
    const spread = Math.min(step * 0.9, 0.09 * list.length);
    list.forEach((n, i) => {
      const ang = base + (list.length > 1 ? -spread / 2 + (spread * i) / (list.length - 1) : 0);
      const rr = R2 + (i % 2) * 34;
      placed2.push({ n, a: ang, x: rr * Math.cos(ang), y: rr * Math.sin(ang), r: 6 });
    });
  }
  return { placed1, placed2, arcs, R1, R2 };
}

const nodeSize = (n: EgoNode) => (n.type === 'company' ? 2.5 : n.type === 'project' ? 2 : 1.5);

function arcPath(r: number, a0: number, a1: number) {
  const p = (t: number) => `${(r * Math.cos(t)).toFixed(1)} ${(r * Math.sin(t)).toFixed(1)}`;
  return `M ${p(a0)} A ${r} ${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${p(a1)}`;
}

export default function Spider() {
  const selectedId = useStore((s) => s.selectedId);
  const select = useStore((s) => s.select);
  const setHover = useStore((s) => s.setHover);
  const openDossier = useStore((s) => s.openDossier);
  const byId = useStore((s) => s.byId);
  const hiddenRels = useStore((s) => s.lens.hiddenRels);
  const match = useMatcher();
  const [depth, setDepth] = useState<1 | 2>(2);
  const [ego, setEgo] = useState<Ego | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ref, size] = useSize<HTMLDivElement>();
  const [zoom, setZoom] = useState({ k: 1, x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; zx: number; zy: number } | null>(null);

  useEffect(() => {
    if (!selectedId) return;
    let live = true;
    setErr(null);
    api.ego(selectedId, depth).then((e) => live && setEgo(e)).catch((e) => live && setErr(String(e)));
    return () => { live = false; };
  }, [selectedId, depth]);

  const lay = useMemo(() => {
    if (!ego) return null;
    // lenses: hide spokes whose node fails the lens or whose every relationship is switched off
    return layout(ego, (n) => match(byId.get(n.id)) && (n.ring === 2 || (n.rels ?? [n.rel]).some((r) => !hiddenRels.includes(r))));
  }, [ego, match, byId, hiddenRels]);

  const fit = lay && size.w ? Math.min(1.25, Math.min(size.w, size.h - 60) / (2 * ((depth === 2 && lay.placed2.length ? lay.R2 + 34 : lay.R1) + 120))) : 1;
  useEffect(() => setZoom({ k: 1, x: 0, y: 0 }), [selectedId, depth]);

  if (!selectedId) return <Empty title="Nothing centred yet" text="Search (Ctrl/Cmd+K), pick an account, or click any node in another view to centre it here." />;
  if (err) return <Empty title="Couldn't load this network" text={err} />;

  const k = fit * zoom.k;
  const cx = size.w / 2 + zoom.x;
  const cy = (size.h + 40) / 2 + zoom.y;
  const center = ego?.center;
  // anchor on the node's own circle, not its group (whose box includes the label and would put the card over the node)
  const hoverOn = (id: string) => (e: React.MouseEvent) =>
    setHover({ id, ...anchorOf(e.currentTarget.querySelector('circle[role="button"]') ?? e.currentTarget) });

  return (
    <div className="spider" ref={ref} data-testid="spider">
      <svg role="img" aria-label={center ? `Network around ${center.label}` : 'Network'}
        onWheel={(e) => {
          const f = e.deltaY < 0 ? 1.12 : 1 / 1.12;
          setZoom((z) => ({ ...z, k: Math.min(4, Math.max(0.3, z.k * f)) }));
        }}
        onMouseDown={(e) => { drag.current = { x: e.clientX, y: e.clientY, zx: zoom.x, zy: zoom.y }; }}
        onMouseMove={(e) => {
          const d = drag.current;
          if (d) setZoom((z) => ({ ...z, x: d.zx + e.clientX - d.x, y: d.zy + e.clientY - d.y }));
        }}
        onMouseUp={() => { drag.current = null; }} onMouseLeave={() => { drag.current = null; }}>
        <defs>
          <clipPath id="spider-circle" clipPathUnits="objectBoundingBox"><circle cx=".5" cy=".5" r=".5" /></clipPath>
          {/* user-space region: a bbox-relative one vanishes on perfectly vertical / horizontal spokes */}
          <filter id="spider-glow" filterUnits="userSpaceOnUse" x={-3000} y={-3000} width={6000} height={6000}><feGaussianBlur stdDeviation="3" result="b" />
            <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
          <style>{'@keyframes flow{to{stroke-dashoffset:-24}} .flow{stroke-dasharray:6 6;animation:flow 1.1s linear infinite}'}</style>
        </defs>
        {lay && center ? (
          <g transform={`translate(${cx} ${cy}) scale(${k})`}>
            <circle r={lay.R1} fill="none" stroke="rgba(120,160,230,.10)" strokeDasharray="2 6" />
            {depth === 2 && lay.placed2.length ? <circle r={lay.R2 + 17} fill="none" stroke="rgba(120,160,230,.06)" strokeDasharray="2 8" /> : null}
            {lay.arcs.map((g) => {
              const mid = (g.a0 + g.a1) / 2;
              const lr = lay.R1 * 0.56;
              return (
                <g key={g.rel}>
                  <path d={arcPath(lay.R1 + 22, g.a0 + 0.02, g.a1 - 0.02)} fill="none" stroke={REL_COLOR[g.rel]} strokeOpacity={0.55}
                    strokeWidth={3} strokeLinecap="round" />
                  <text className="arc-label" x={lr * Math.cos(mid)} y={lr * Math.sin(mid)} textAnchor="middle" fill={REL_COLOR[g.rel]}>
                    {REL_LABEL[g.rel]} · {g.count}
                  </text>
                </g>
              );
            })}
            {lay.placed2.map((p) => {
              const parent = lay.placed1.find((q) => q.n.id === p.n.parent);
              return parent ? <line key={`l2-${p.n.id}`} x1={parent.x} y1={parent.y} x2={p.x} y2={p.y} stroke="rgba(143,163,200,.22)" /> : null;
            })}
            {lay.placed1.map((p) => {
              const opp = p.n.rel === 'opportunity';
              return (
                <line key={`l1-${p.n.id}`} x1={0} y1={0} x2={p.x} y2={p.y} stroke={REL_COLOR[p.n.rel]}
                  strokeWidth={opp ? 2.6 : 1.3} strokeOpacity={opp ? 0.95 : 0.55} className={opp ? 'flow' : undefined}
                  filter={opp ? 'url(#spider-glow)' : undefined} />
              );
            })}
            {lay.placed2.map((p) => (
              <g key={`n2-${p.n.id}`} className="snode" transform={`translate(${p.x} ${p.y})`} opacity={0.55}
                onMouseEnter={hoverOn(p.n.id)} onMouseLeave={() => setHover(null)}
                onClick={(e) => { e.stopPropagation(); select(p.n.id); }} data-id={p.n.id}>
                <circle r={p.r} fill={GROUP_COLOR[p.n.group]} fillOpacity={0.35} stroke={GROUP_COLOR[p.n.group]} />
                <circle r={p.r + 5} fill="transparent" role="button" aria-label={`Re-centre on ${p.n.label}`} />
                {lay.placed2.length <= 40 ? (
                  <text className="spoke-label r2" x={Math.cos(p.a) * (p.r + 4)} y={Math.sin(p.a) * (p.r + 4) + 3}
                    textAnchor={Math.cos(p.a) >= 0 ? 'start' : 'end'}>{p.n.label}</text>
                ) : null}
              </g>
            ))}
            {lay.placed1.map((p) => (
              <g key={`n1-${p.n.id}`} className="snode" transform={`translate(${p.x} ${p.y})`} data-id={p.n.id}
                onMouseEnter={hoverOn(p.n.id)} onMouseLeave={() => setHover(null)}
                onClick={(e) => { e.stopPropagation(); select(p.n.id); }}>
                {p.n.oppCount ? <circle r={p.r + 4} fill="none" stroke="#f5a524" strokeWidth={2} filter="url(#spider-glow)" /> : null}
                <circle r={p.r} fill="#0b1326" stroke={GROUP_COLOR[p.n.group]} strokeWidth={2.5} />
                <image href={photoUrl(p.n.id)} x={-p.r + 2} y={-p.r + 2} width={2 * p.r - 4} height={2 * p.r - 4} clipPath="url(#spider-circle)" />
                <text className="spoke-label" x={Math.cos(p.a) * (p.r + 6)} y={Math.sin(p.a) * (p.r + 6) + 4}
                  textAnchor={Math.cos(p.a) >= 0 ? 'start' : 'end'}>{p.n.label}</text>
                <circle r={p.r + 4} fill="transparent" role="button" aria-label={`Re-centre on ${p.n.label}`} />
              </g>
            ))}
            <g className="snode" onClick={(e) => { e.stopPropagation(); openDossier(true); }}
              onMouseEnter={hoverOn(center.id)} onMouseLeave={() => setHover(null)}>
              <circle r={44} fill="none" stroke={GROUP_COLOR[center.group]} strokeOpacity={0.35} strokeWidth={10} filter="url(#spider-glow)" />
              <circle r={36} fill="#0b1326" stroke={GROUP_COLOR[center.group]} strokeWidth={3} />
              <image href={photoUrl(center.id)} x={-33} y={-33} width={66} height={66} clipPath="url(#spider-circle)" />
              <text className="spoke-label" y={58} textAnchor="middle" style={{ fontSize: 15, fontWeight: 600 }}>{center.label}</text>
              {center.sub ? <text className="spoke-label r2" y={74} textAnchor="middle">{center.sub}</text> : null}
              <circle r={44} fill="transparent" role="button" aria-label={`${center.label} details`} />
            </g>
          </g>
        ) : null}
      </svg>
      <div className="depth-toggle glass" style={{ position: 'absolute', right: 14, bottom: 14 }}>
        <button className={`btn small${depth === 1 ? ' primary' : ''}`} onClick={() => setDepth(1)}>1 hop</button>
        <button className={`btn small${depth === 2 ? ' primary' : ''}`} onClick={() => setDepth(2)}>2 hops</button>
        <button className="btn small" onClick={() => setZoom({ k: 1, x: 0, y: 0 })}>Fit</button>
      </div>
      {ego && lay && !lay.placed1.length ? (
        <div className="empty" style={{ pointerEvents: 'none', alignItems: 'end', paddingBottom: 80 }}>
          <div className="glass">{ego.nodes.length ? 'The current lenses hide every neighbour — reset them in the rail.' : 'No relationships recorded for this entity yet.'}</div>
        </div>
      ) : null}
    </div>
  );
}
