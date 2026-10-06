import ForceGraph3D, { type ForceGraph3DInstance } from '3d-force-graph';
import { useEffect, useRef } from 'react';
import * as THREE from 'three';
import { photoUrl } from '../api';
import { makeMatcher, useStore } from '../store';
import { GROUP_COLOR, REL_COLOR } from '../theme';
import type { GLink, GNode } from '../types';

type FNode = GNode & { x?: number; y?: number; z?: number };
type FLink = Omit<GLink, 'source' | 'target'> & { source: string | FNode; target: string | FNode };
interface NodeObj { group: THREE.Group; fade: { m: THREE.Material; base: number }[] }

const endId = (e: string | FNode) => (typeof e === 'string' ? e : e.id);
const radius = (n: GNode) => 4.2 * Math.cbrt(Math.max(n.size, 0.8));

function rgba(hex: string, a: number) {
  const v = parseInt(hex.slice(1), 16);
  return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${a})`;
}

/** Face / logo badge: drawn into a canvas (monogram from /api/photo when there's no real image). */
function badgeSprite(n: GNode, r: number) {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(r * 1.7, r * 1.7, 1);
  const img = new Image();
  img.onload = () => {
    const ctx = canvas.getContext('2d')!;
    ctx.save();
    ctx.beginPath();
    ctx.arc(size / 2, size / 2, size / 2 - 5, 0, Math.PI * 2);
    ctx.clip();
    const s = Math.max(size / (img.width || size), size / (img.height || size));
    const w = (img.width || size) * s;
    const h = (img.height || size) * s;
    ctx.drawImage(img, (size - w) / 2, (size - h) / 2, w, h);
    ctx.restore();
    ctx.lineWidth = 6;
    ctx.strokeStyle = GROUP_COLOR[n.group];
    ctx.beginPath();
    ctx.arc(size / 2, size / 2, size / 2 - 4, 0, Math.PI * 2);
    ctx.stroke();
    tex.needsUpdate = true;
  };
  img.src = photoUrl(n.id);
  // keep the badge on the camera-facing surface of its sphere, so the sphere never hides it
  const tmp = new THREE.Vector3();
  sprite.onBeforeRender = (_r, _s, camera) => {
    const parent = sprite.parent;
    if (!parent) return;
    parent.getWorldPosition(tmp);
    tmp.subVectors(camera.position, tmp).normalize().multiplyScalar(r * 1.02).divideScalar(parent.scale.x || 1);
    sprite.position.copy(tmp);
    sprite.updateMatrixWorld();
  };
  return { sprite, mat };
}

function labelSprite(text: string, r: number) {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d')!;
  const font = '600 30px Inter, system-ui, sans-serif';
  ctx.font = font;
  const w = Math.ceil(ctx.measureText(text).width) + 20;
  canvas.width = w;
  canvas.height = 40;
  ctx.font = font;
  ctx.fillStyle = '#e9eefc';
  ctx.shadowColor = 'rgba(5,7,15,.95)';
  ctx.shadowBlur = 6;
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 10, 21);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false });
  const sprite = new THREE.Sprite(mat);
  const hgt = 5.5;
  sprite.scale.set((hgt * w) / 40, hgt, 1);
  sprite.position.set(0, -(r + 4), 0);
  return { sprite, mat };
}

export default function Graph3D() {
  const el = useRef<HTMLDivElement>(null);
  const fg = useRef<ForceGraph3DInstance | null>(null);
  const objs = useRef(new Map<string, NodeObj>());
  const nodes = useStore((s) => s.nodes);
  const links = useStore((s) => s.links);
  const lens = useStore((s) => s.lens);
  const byId = useStore((s) => s.byId);
  const employerOf = useStore((s) => s.employerOf);
  const selectedId = useStore((s) => s.selectedId);
  const matchRef = useRef(makeMatcher(lens, byId, employerOf));
  const hiddenRels = useRef(lens.hiddenRels);

  // build once per dataset; lens / selection changes restyle in place (no re-layout)
  useEffect(() => {
    if (!el.current || !nodes.length) return;
    const { select, setHover } = useStore.getState();
    const many = nodes.length > 150;
    let framed = false;
    objs.current.clear();
    const linkVisible = (l: FLink) => {
      const s = byId.get(endId(l.source));
      const t = byId.get(endId(l.target));
      return !hiddenRels.current.includes(l.rel) && matchRef.current(s) && matchRef.current(t);
    };
    const g = new ForceGraph3D(el.current, { controlType: 'orbit' })
      .backgroundColor('#05070f')
      .showNavInfo(false)
      .nodeId('id')
      .nodeLabel(() => '')
      .nodeThreeObject((o) => {
        const n = o as FNode;
        const r = radius(n);
        const color = new THREE.Color(GROUP_COLOR[n.group]);
        const group = new THREE.Group();
        const sphereMat = new THREE.MeshPhongMaterial({
          color, emissive: color, emissiveIntensity: 0.38, shininess: 110, specular: new THREE.Color('#ffffff'),
          transparent: true, opacity: 0.95,
        });
        group.add(new THREE.Mesh(new THREE.SphereGeometry(r, 28, 20), sphereMat));
        const fade: NodeObj['fade'] = [{ m: sphereMat, base: 0.95 }];
        if (n.type === 'person' || n.type === 'company') {
          const { sprite, mat } = badgeSprite(n, r);
          group.add(sprite);
          fade.push({ m: mat, base: 1 });
        }
        if (!many || n.type !== 'person' || n.size >= 2) {
          const { sprite, mat } = labelSprite(n.label, r);
          group.add(sprite);
          fade.push({ m: mat, base: 0.95 });
        }
        objs.current.set(n.id, { group, fade });
        return group;
      })
      .linkColor((o) => {
        const l = o as unknown as FLink;
        return linkVisible(l) ? rgba(REL_COLOR[l.rel], l.rel === 'opportunity' ? 0.95 : 0.6) : rgba(REL_COLOR[l.rel], 0.05);
      })
      .linkWidth((o) => ((o as unknown as FLink).rel === 'opportunity' ? 1.4 : 0.5))
      .linkOpacity(0.7)
      .linkDirectionalParticles((o) => {
        const l = o as unknown as FLink;
        return l.rel === 'opportunity' && linkVisible(l) ? 4 : 0;
      })
      .linkDirectionalParticleWidth(2.4)
      .linkDirectionalParticleSpeed(0.006)
      .linkDirectionalParticleColor(() => '#f5a524')
      .onNodeHover((o) => {
        if (el.current) el.current.style.cursor = o ? 'pointer' : 'grab';
        const n = o as FNode | null;
        if (!n || n.x === undefined) { setHover(null); return; }
        const p = g.graph2ScreenCoords(n.x, n.y ?? 0, n.z ?? 0);
        setHover({ id: n.id, x: p.x, y: p.y });
      })
      .onNodeClick((o) => select((o as FNode).id, { dossier: true }))
      .onBackgroundClick(() => setHover(null))
      .cooldownTicks(nodes.length > 400 ? 120 : 220)
      .cooldownTime(5000)
      .onEngineStop(() => {
        // frame the whole network once the layout settles (unless something is already selected)
        if (!framed && !useStore.getState().selectedId) g.zoomToFit(700, 40);
        framed = true;
      })
      .graphData({ nodes: nodes.map((n) => ({ ...n })), links: links.map((l) => ({ ...l })) });
    g.d3Force('charge')?.strength(-90);
    fg.current = g;
    const ro = new ResizeObserver(() => {
      if (el.current) g.width(el.current.clientWidth).height(el.current.clientHeight);
    });
    ro.observe(el.current);
    return () => { ro.disconnect(); g._destructor(); fg.current = null; el.current?.replaceChildren(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, links]);

  // lens: dim (never remove) non-matching nodes and links
  useEffect(() => {
    matchRef.current = makeMatcher(lens, byId, employerOf);
    hiddenRels.current = lens.hiddenRels;
    for (const [id, o] of objs.current) {
      const on = matchRef.current(byId.get(id));
      for (const f of o.fade) { f.m.opacity = on ? f.base : f.base * 0.12; f.m.transparent = true; }
    }
    const g = fg.current;
    if (g) g.linkColor(g.linkColor()).linkDirectionalParticles(g.linkDirectionalParticles());
  }, [lens, byId, employerOf]);

  // selection: enlarge the selected node and fly the camera to it
  useEffect(() => {
    for (const [id, o] of objs.current) o.group.scale.setScalar(id === selectedId ? 1.6 : 1);
    const g = fg.current;
    if (!g || !selectedId) return;
    const n = (g.graphData().nodes as FNode[]).find((x) => x.id === selectedId);
    if (!n || n.x === undefined) return;
    const d = Math.hypot(n.x, n.y ?? 0, n.z ?? 0);
    const dist = 120;
    const pos = d > 1 ? { x: n.x * (1 + dist / d), y: (n.y ?? 0) * (1 + dist / d), z: (n.z ?? 0) * (1 + dist / d) } : { x: 0, y: 0, z: dist };
    g.cameraPosition(pos, { x: n.x, y: n.y ?? 0, z: n.z ?? 0 }, 900);
  }, [selectedId, nodes]);

  return <div className="graph3d" ref={el} data-testid="graph3d" />;
}
