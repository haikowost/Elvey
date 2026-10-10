import { lazy, Suspense, useEffect } from 'react';
import { api } from './api';
import CommandPalette from './components/CommandPalette';
import Dossier from './components/Dossier';
import InsightCard from './components/InsightCard';
import Rail, { VIEWS } from './components/Rail';
import { useStore } from './store';
import type { View } from './types';
import Investigate from './views/Investigate';
import Org from './views/Org';
import People from './views/People';
import Spider from './views/Spider';

// three.js is most of the bundle: only fetch it when the 3D view is actually opened
const Graph3D = lazy(() => import('./views/Graph3D'));
const VIEW_KEYS = VIEWS.map((v) => v.key);

/** `#spider:veracitech` -> view + selected entity (bare token, so it can be bookmarked / shared). */
function readHash(): { view?: View; id?: string } {
  const raw = decodeURIComponent(window.location.hash.replace(/^#/, ''));
  if (!raw) return {};
  const [v, ...rest] = raw.split(':');
  const id = rest.join(':') || undefined;
  return VIEW_KEYS.includes(v as View) ? { view: v as View, id } : { id: raw };
}

function applyHash() {
  const { view, id } = readHash();
  const st = useStore.getState();
  // before the graph loads any id is accepted (setGraph drops it if unknown); after, ignore unknown ids
  const known = !id || st.source === null || st.byId.has(id);
  if (id && known && id !== st.selectedId) st.select(id, view ? { view } : undefined);
  else if (view && view !== st.view) st.setView(view);
}

function Breadcrumbs() {
  const trail = useStore((s) => s.trail);
  const byId = useStore((s) => s.byId);
  const select = useStore((s) => s.select);
  const back = useStore((s) => s.back);
  if (!trail.length) return null;
  return (
    <nav className="crumbs glass" aria-label="Breadcrumbs">
      <button className="crumb" onClick={back} disabled={trail.length < 2} aria-label="Back">←</button>
      {trail.map((id, i) => (
        <span key={`${id}-${i}`} style={{ display: 'contents' }}>
          {i ? <span className="crumb-sep">›</span> : null}
          <button className={`crumb${i === trail.length - 1 ? ' current' : ''}`} onClick={() => select(id)}>
            {byId.get(id)?.label ?? id}
          </button>
        </span>
      ))}
    </nav>
  );
}

export default function App() {
  const view = useStore((s) => s.view);
  const selectedId = useStore((s) => s.selectedId);
  const loadError = useStore((s) => s.loadError);
  const loaded = useStore((s) => s.source !== null);

  useEffect(() => {
    applyHash();
    api.graph().then((g) => useStore.getState().setGraph(g)).catch((e) => useStore.getState().setLoadError(String(e)));
    window.addEventListener('hashchange', applyHash);
    return () => window.removeEventListener('hashchange', applyHash);
  }, []);

  useEffect(() => {
    const token = `#${view}${selectedId ? `:${selectedId}` : ''}`;
    if (window.location.hash !== token) history.replaceState(null, '', token);
  }, [view, selectedId]);

  const title = VIEWS.find((v) => v.key === view)?.label;
  return (
    <div className="app">
      <Rail />
      <main className="main">
        {loadError ? <div className="empty"><div className="glass"><h2>Couldn't load the graph</h2><div className="err">{loadError}</div></div></div> : null}
        {loaded ? (
          <>
            {view === 'graph' ? <Suspense fallback={<div className="empty"><div className="muted">Loading 3D…</div></div>}><Graph3D /></Suspense> : null}
            {view === 'spider' ? <Spider /> : null}
            {view === 'org' ? <Org /> : null}
            {view === 'investigate' ? <Investigate /> : null}
            {view === 'people' ? <People /> : null}
          </>
        ) : !loadError ? <div className="empty"><div className="muted">Loading the deal network…</div></div> : null}
        <div className="topbar">
          <span className="view-title glass">{title}</span>
          {view !== 'graph' ? <Breadcrumbs /> : null}
        </div>
        <InsightCard />
        <Dossier />
      </main>
      <CommandPalette />
    </div>
  );
}
