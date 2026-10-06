import type { Group, Rel } from './types';

// Mirrors the CSS tokens in styles.css (§8 of the build brief); three.js / SVG need raw values.
export const GROUP_COLOR: Record<Group, string> = {
  elvey: '#f5a524', customer: '#22d3ee', enduser: '#2dd4bf', consultant: '#e879f9',
  competitor: '#fb5a7d', supplier: '#60a5fa', project: '#a78bfa', product: '#34d399',
};

export const REL_COLOR: Record<Rel, string> = {
  works_at: '#5f7bb0', part_of: '#f5a524', does_business_with: '#22d3ee', involved_in: '#a78bfa',
  specifies: '#e879f9', serves: '#2dd4bf', competes: '#fb5a7d', opportunity: '#f5a524', knows: '#8fa3c8',
};

export const REL_LABEL: Record<Rel, string> = {
  works_at: 'Works at', part_of: 'Part of', does_business_with: 'Does business with', involved_in: 'Involved in',
  specifies: 'Specifies', serves: 'Serves', competes: 'Competes', opportunity: 'Opportunity', knows: 'Knows',
};

export const RELS = Object.keys(REL_LABEL) as Rel[];

// Lens "entity type" keys: a person's key is 'person', everything else its group.
export const TYPE_LENSES: { key: string; label: string; color: string }[] = [
  { key: 'person', label: 'People', color: '#e9eefc' },
  { key: 'elvey', label: 'Elvey / Pentagon', color: GROUP_COLOR.elvey },
  { key: 'customer', label: 'Integrators / resellers', color: GROUP_COLOR.customer },
  { key: 'supplier', label: 'Suppliers', color: GROUP_COLOR.supplier },
  { key: 'enduser', label: 'End-users', color: GROUP_COLOR.enduser },
  { key: 'consultant', label: 'Consultants', color: GROUP_COLOR.consultant },
  { key: 'competitor', label: 'Competitors', color: GROUP_COLOR.competitor },
  { key: 'project', label: 'Projects', color: GROUP_COLOR.project },
  { key: 'product', label: 'Products', color: GROUP_COLOR.product },
];

export const REGIONS = ['West', 'North', 'Cape', 'East', 'Export'];
export const BRANDS = ['IQSIGHT', 'Milestone', 'SmartSuite', 'Vaxtor', 'SAFR', 'Tridium'];

export const GROUP_LABEL: Record<Group, string> = {
  elvey: 'Elvey', customer: 'Integrator / reseller', supplier: 'Supplier', enduser: 'End-user',
  consultant: 'Consultant', competitor: 'Competitor', project: 'Project', product: 'Product',
};
