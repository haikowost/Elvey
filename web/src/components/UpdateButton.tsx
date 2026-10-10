import { useState } from 'react';
import { api } from '../api';
import { useStore } from '../store';

/** Graph person ids are 'c<contact id>' on live data; the demo seed has none. */
export const contactIdOf = (nodeId: string): number | null => (/^c\d+$/.test(nodeId) ? Number(nodeId.slice(1)) : null);

/**
 * "Update from LinkedIn" for one person: the local harvester opens LinkedIn in its own logged-in
 * window, reads the profile and saves it (1 of today's harvest cap, no Claude tokens). The graph is
 * reloaded afterwards so every view shows the new role / photo / history.
 */
export default function UpdateButton({ nodeId, small }: { nodeId: string; small?: boolean }) {
  const source = useStore((s) => s.source);
  const [state, setState] = useState<'idle' | 'busy' | 'done' | 'error'>('idle');
  const [msg, setMsg] = useState('');
  const id = contactIdOf(nodeId);
  if (source !== 'live' || id === null) return null;
  const run = async () => {
    setState('busy');
    setMsg('LinkedIn window opening…');
    try {
      const r = await api.harvest(id);
      setState('done');
      setMsg(`Updated (${r.result ?? 'ok'}) · today ${r.used_today}`);
      useStore.getState().setGraph(await api.graph());
    } catch (e) {
      setState('error');
      setMsg(String(e instanceof Error ? e.message : e));
    }
  };
  return (
    <span className="upd">
      <button className={`btn${small ? ' small' : ''}`} onClick={(e) => { e.stopPropagation(); void run(); }}
        disabled={state === 'busy'} aria-busy={state === 'busy'}
        title="Look this person up on LinkedIn now (uses 1 of today's harvest cap; no Claude tokens)">
        {state === 'busy' ? '↻ Updating…' : '↻ Update from LinkedIn'}
      </button>
      {msg ? <span className={`upd-msg${state === 'error' ? ' err' : ''}`} role="status">{msg}</span> : null}
    </span>
  );
}
