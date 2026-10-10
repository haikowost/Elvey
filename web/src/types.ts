export type NodeType = 'company' | 'person' | 'project' | 'product';
export type Group = 'elvey' | 'customer' | 'supplier' | 'enduser' | 'consultant' | 'competitor' | 'project' | 'product';
export type Rel =
  | 'works_at' | 'part_of' | 'does_business_with' | 'involved_in' | 'specifies'
  | 'serves' | 'competes' | 'opportunity' | 'knows';
export type View = 'graph' | 'spider' | 'org' | 'investigate';

export interface Opp { t?: string; p?: string; val?: string; via?: string | null }

export interface GNode {
  id: string;
  label: string;
  type: NodeType;
  group: Group;
  size: number;
  sub: string | null;
  photoUrl: string | null;
  facts: string[];
  opps: Opp[];
  region: string | null;
  brands: string[];
  zoho: boolean | null;
  kyc: 'done' | 'pending' | null;
  oppCount: number;
  kycTier?: string | null;   // MD's KYC priority tier (P1 Key, P2 Active, ...)
  kycScore?: number | null;
}

export interface GLink { source: string; target: string; rel: Rel; ex?: Record<string, unknown> }

export interface GraphPayload { source: 'seed' | 'live'; root?: string | null; nodes: GNode[]; links: GLink[] }

export interface Brief {
  id: string; label: string; type: NodeType; group: Group; sub: string | null; photoUrl: string | null;
  zoho: boolean | null; kyc: 'done' | 'pending' | null; oppCount: number; region: string | null;
  kycTier?: string | null; kycScore?: number | null;
}

export interface EgoNode extends Brief { ring: 1 | 2; rel: Rel; rels?: Rel[]; parent?: string; opp?: string; w?: number }
export interface Ego {
  center: Brief & { facts: string[] };
  groups: { rel: Rel; ids: string[]; total?: number }[];
  nodes: EgoNode[];
  links: GLink[];
}

export interface Dossier {
  node: GNode;
  employer: string | null;
  employer_id: string | null;
  enrichment: {
    summary?: string | null; experience?: { title?: string; company?: string; dates?: string }[];
    linkedin?: string | null; email?: string | null; phone?: string | null;
  };
  kyc_pending: boolean;
  relationships: { rel: Rel; items: (Brief & { dir: 'in' | 'out'; via: string | null; ex?: Record<string, unknown> })[] }[];
  opportunities: Opp[];
  investigable: boolean;
}

export interface KycProgress {
  contacts: number; role_known: number; linkedin_url: number; photo: number; enriched: number;
  no_profile: number; queued: number; used_today: number; daily_cap: number;
  tiers: { tier: string; label: string; total: number; enriched: number; pending: number; photo: number; pinned?: number }[];
  priority_mode?: boolean;
  last_harvest_run: ({ at: string; processed?: number; stopped?: string; tier?: string } & Record<string, unknown>) | null;
  last_linkedin_visit: string | null; last_ingest: string | null;
}

export interface Investigation {
  account: Brief & { facts: string[]; brands: string[] };
  owner: string | null;
  stats: { contacts: number; projects: number; consultants: number; endusers: number; opportunities: number };
  contacts: Brief[];
  opportunities: Opp[];
  projects: (Brief & { brands: string[]; opps: Opp[] })[];
  consultants: (Brief & { via: string })[];
  endusers: (Brief & { via: string })[];
  competitors: (Brief & { via: string })[];
  plays: string[];
}
