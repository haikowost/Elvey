import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { useStore } from '../store';
import { GROUP_LABEL } from '../theme';
import type { Brief } from '../types';
import { Avatar } from './bits';

/** Ctrl/Cmd+K: find any person, org or project and jump to it in the current view. */
export default function CommandPalette() {
  const open = useStore((s) => s.paletteOpen);
  const setPalette = useStore((s) => s.setPalette);
  const select = useStore((s) => s.select);
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<Brief[]>([]);
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setPalette(!useStore.getState().paletteOpen); }
      if (e.key === 'Escape') setPalette(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [setPalette]);

  useEffect(() => { if (open) { setQ(''); setHits([]); setActive(0); setTimeout(() => input.current?.focus(), 0); } }, [open]);

  useEffect(() => {
    if (!q.trim()) { setHits([]); return; }
    let live = true;
    const t = setTimeout(() => api.search(q).then((h) => { if (live) { setHits(h); setActive(0); } }).catch(() => {}), 120);
    return () => { live = false; clearTimeout(t); };
  }, [q]);

  if (!open) return null;
  const go = (b: Brief) => { setPalette(false); select(b.id, { dossier: true }); };
  return (
    <div className="palette-back" onMouseDown={() => setPalette(false)}>
      <div className="palette glass" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-label="Search">
        <input ref={input} value={q} placeholder="Find a person, company or project…" aria-label="Search entities"
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(a + 1, hits.length - 1)); }
            if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
            if (e.key === 'Enter' && hits[active]) go(hits[active]);
          }} />
        <ul>
          {hits.map((h, i) => (
            <li key={h.id}>
              <button className={i === active ? 'active' : ''} onMouseEnter={() => setActive(i)} onClick={() => go(h)}>
                <Avatar n={h} />
                <span><div>{h.label}</div><div className="muted" style={{ fontSize: 11.5 }}>{h.sub}</div></span>
                <span className="kind">{h.type === 'person' ? 'person' : GROUP_LABEL[h.group]}</span>
              </button>
            </li>
          ))}
          {q.trim() && !hits.length ? <li className="muted" style={{ padding: 10 }}>No matches.</li> : null}
        </ul>
      </div>
    </div>
  );
}
