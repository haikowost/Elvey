import type { Brief, Dossier, Ego, GraphPayload, Investigation, KycProgress } from './types';

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

const enc = encodeURIComponent;

export const api = {
  graph: () => get<GraphPayload>('/api/graph'),
  entity: (id: string) => get<Dossier>(`/api/entity/${enc(id)}`),
  ego: (id: string, depth: 1 | 2, limit = 24) => get<Ego>(`/api/entity/${enc(id)}/ego?depth=${depth}&limit=${limit}`),
  progress: () => get<KycProgress>('/api/kyc/progress'),
  investigate: (id: string) => get<Investigation>(`/api/account/${enc(id)}/investigate`),
  search: (q: string) => get<Brief[]>(`/api/search?q=${enc(q)}`),
};

export const photoUrl = (id: string) => `/api/photo/${enc(id)}`;
