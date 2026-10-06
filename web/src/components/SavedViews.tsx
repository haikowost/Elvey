import { useState } from 'react';
import { EMPTY_LENS, type Lens, useStore } from '../store';
import type { View } from '../types';

interface Saved { name: string; view: View; id: string | null; lens: Lens }
const KEY = 'elvey.graph.savedViews';

// Per-viewer convenience only: storage can be blocked or empty (private window), so every access is guarded.
function load(): Saved[] {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) ?? '[]');
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}
function store(list: Saved[]) {
  try { localStorage.setItem(KEY, JSON.stringify(list)); } catch { /* storage unavailable: keep in memory only */ }
}

export default function SavedViews() {
  const [list, setList] = useState<Saved[]>(load);
  const view = useStore((s) => s.view);
  const selectedId = useStore((s) => s.selectedId);
  const lens = useStore((s) => s.lens);
  const byId = useStore((s) => s.byId);
  const save = () => {
    const label = selectedId ? byId.get(selectedId)?.label : null;
    const name = window.prompt('Name this view', `${view[0].toUpperCase()}${view.slice(1)}${label ? ` · ${label}` : ''}`);
    if (!name) return;
    const next = [...list.filter((s) => s.name !== name), { name, view, id: selectedId, lens }];
    setList(next);
    store(next);
  };
  const apply = (s: Saved) => {
    const st = useStore.getState();
    st.setLens({ ...EMPTY_LENS, ...s.lens });
    if (s.id && st.byId.has(s.id)) st.select(s.id, { view: s.view });
    else st.setView(s.view);
  };
  const remove = (name: string) => { const next = list.filter((s) => s.name !== name); setList(next); store(next); };

  return (
    <section aria-label="Saved views">
      <h4 className="rail-h">Saved views <button onClick={save}>+ save current</button></h4>
      <div className="saved">
        {list.length ? list.map((s) => (
          <div className="saved-row" key={s.name}>
            <button className="go" onClick={() => apply(s)} title={s.name}>{s.name}</button>
            <button className="icon-btn" onClick={() => remove(s.name)} aria-label={`Delete ${s.name}`}>✕</button>
          </div>
        )) : <div className="muted" style={{ fontSize: 11.5 }}>Pin an entity + view + lens combo to come back to it.</div>}
      </div>
    </section>
  );
}
