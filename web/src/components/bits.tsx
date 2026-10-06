import type { CSSProperties } from 'react';
import { photoUrl } from '../api';
import { GROUP_COLOR } from '../theme';
import type { Brief, GNode } from '../types';

type Entity = Pick<Brief, 'id' | 'label' | 'group'>;

export function Avatar({ n, large }: { n: Entity; large?: boolean }) {
  return (
    <img className={`avatar${large ? ' lg' : ''}`} src={photoUrl(n.id)} alt="" loading="lazy"
      style={{ '--c': GROUP_COLOR[n.group] } as CSSProperties} />
  );
}

export function ZohoChip({ zoho }: { zoho: boolean | null | undefined }) {
  if (zoho === null || zoho === undefined) return null; // projects/products aren't Zoho records
  return zoho ? <span className="zchip in">in Zoho</span> : <span className="zchip out">not synced</span>;
}

export function KycChip({ n }: { n: Pick<Brief, 'type' | 'kyc'> }) {
  return n.type === 'person' && n.kyc !== 'done' ? <span className="zchip kyc">KYC pending</span> : null;
}

export function OppBadge({ count }: { count: number }) {
  return count ? <span className="opp-badge" title="open opportunities">◆ {count}</span> : null;
}

export function Dot({ color }: { color: string }) {
  return <span className="dot" style={{ color }} />;
}

export const isGNode = (n: Brief | GNode): n is GNode => 'facts' in n;
