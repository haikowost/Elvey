import { useMemo } from 'react';
import { create } from 'zustand';
import type { GLink, GNode, GraphPayload, Rel, View } from './types';

export interface Lens {
  hiddenTypes: string[]; // entity-type lens keys switched off
  hiddenRels: Rel[];     // relationship types switched off
  regions: string[];     // empty = all regions
  brands: string[];      // empty = all brands
  oppsOnly: boolean;
}

export const EMPTY_LENS: Lens = { hiddenTypes: [], hiddenRels: [], regions: [], brands: [], oppsOnly: false };

export interface Hover { id: string; x: number; y: number }

interface State {
  source: 'seed' | 'live' | null;
  root: string | null;   // the default centre: Elvey, else the top account by sellout
  nodes: GNode[];
  links: GLink[];
  byId: Map<string, GNode>;
  employerOf: Map<string, string>;
  loadError: string | null;

  view: View;
  selectedId: string | null;
  trail: string[];
  dossierOpen: boolean;
  lens: Lens;
  hover: Hover | null;
  paletteOpen: boolean;

  setGraph: (g: GraphPayload) => void;
  setLoadError: (e: string) => void;
  setView: (v: View) => void;
  select: (id: string | null, opts?: { view?: View; dossier?: boolean }) => void;
  back: () => void;
  openDossier: (open: boolean) => void;
  setLens: (patch: Partial<Lens>) => void;
  setHover: (h: Hover | null) => void;
  setPalette: (open: boolean) => void;
}

export const useStore = create<State>((set, get) => ({
  source: null,
  root: null,
  nodes: [],
  links: [],
  byId: new Map(),
  employerOf: new Map(),
  loadError: null,

  // Spider (flat 2D radial) is the default: the 3D graph is kept, but it's hard to navigate at this size
  view: 'spider',
  selectedId: null,
  trail: [],
  dossierOpen: false,
  lens: EMPTY_LENS,
  hover: null,
  paletteOpen: false,

  setGraph: (g) => {
    const byId = new Map(g.nodes.map((n) => [n.id, n]));
    const employerOf = new Map<string, string>();
    for (const l of g.links) if (l.rel === 'works_at' && !employerOf.has(l.source)) employerOf.set(l.source, l.target);
    const sel = get().selectedId;
    const root = g.root && byId.has(g.root) ? g.root : defaultRoot(g.nodes);
    // nothing selected (or a deep link to an id this dataset doesn't have): centre on the root,
    // so the Spider opens on Elvey's network instead of an empty "pick something" screen
    const keep = sel && byId.has(sel);
    set({
      source: g.source, root, nodes: g.nodes, links: g.links, byId, employerOf,
      ...(keep ? {} : { selectedId: root, trail: root ? [root] : [] }),
    });
  },
  setLoadError: (e) => set({ loadError: e }),

  setView: (view) => set({ view, hover: null }),

  // One selection for every view. The trail is the drill-down breadcrumb: re-selecting something
  // already on it rewinds to that point instead of growing the trail.
  select: (id, opts) => {
    const { trail } = get();
    let next = trail;
    if (id) {
      const at = trail.indexOf(id);
      next = at >= 0 ? trail.slice(0, at + 1) : [...trail, id].slice(-12);
    }
    set({
      selectedId: id, trail: id ? next : [], hover: null,
      ...(opts?.view ? { view: opts.view } : {}),
      ...(opts?.dossier !== undefined ? { dossierOpen: opts.dossier && !!id } : {}),
    });
  },
  back: () => {
    const { trail } = get();
    if (trail.length < 2) return;
    const next = trail.slice(0, -1);
    set({ trail: next, selectedId: next[next.length - 1], hover: null });
  },
  openDossier: (open) => set({ dossierOpen: open && !!get().selectedId }),
  setLens: (patch) => set({ lens: { ...get().lens, ...patch } }),
  setHover: (hover) => set({ hover }),
  setPalette: (paletteOpen) => set({ paletteOpen }),
}));

/** Fallback centre when the API names none: an Elvey company, else the biggest company. */
export function defaultRoot(nodes: GNode[]): string | null {
  const companies = nodes.filter((n) => n.type === 'company');
  const pick = companies.filter((n) => n.group === 'elvey').sort((a, b) => b.size - a.size)[0]
    ?? companies.sort((a, b) => b.size - a.size)[0];
  return pick?.id ?? null;
}

export const lensKey = (n: GNode) => (n.type === 'person' ? 'person' : n.group);

/** Does this node pass the current lens? People inherit region/brands from their employer. */
export function makeMatcher(lens: Lens, byId: Map<string, GNode>, employerOf: Map<string, string>) {
  const active = lens.hiddenTypes.length || lens.regions.length || lens.brands.length || lens.oppsOnly;
  return (n: GNode | undefined): boolean => {
    if (!n) return false;
    if (!active) return true;
    if (lens.hiddenTypes.includes(lensKey(n))) return false;
    const emp = n.type === 'person' ? byId.get(employerOf.get(n.id) ?? '') : undefined;
    if (lens.regions.length && !lens.regions.includes(n.region ?? emp?.region ?? '')) return false;
    if (lens.brands.length) {
      const brands = n.brands.length ? n.brands : emp?.brands ?? [];
      if (!brands.some((b) => lens.brands.includes(b))) return false;
    }
    if (lens.oppsOnly && !n.oppCount) return false;
    return true;
  };
}

export function useMatcher() {
  const lens = useStore((s) => s.lens);
  const byId = useStore((s) => s.byId);
  const employerOf = useStore((s) => s.employerOf);
  return useMemo(() => makeMatcher(lens, byId, employerOf), [lens, byId, employerOf]);
}

export function lensActive(l: Lens) {
  return !!(l.hiddenTypes.length || l.hiddenRels.length || l.regions.length || l.brands.length || l.oppsOnly);
}
