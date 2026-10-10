import type { Brief, Dossier, Ego, GraphPayload, HarvestResult, Investigation, KycProgress } from './types';

async function post<T>(url: string): Promise<T> {
  const r = await fetch(url, { method: 'POST' });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((j as { detail?: string }).detail || `${r.status}`);
  return j as T;
}

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
  /** "Update from LinkedIn": one contact, now, through the local harvester (no Claude involved). */
  harvest: (contactId: number) => post<HarvestResult>(`/api/contacts/${contactId}/harvest`),
};

export const photoUrl = (id: string) => `/api/photo/${enc(id)}`;
