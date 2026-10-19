import { useCallback, useEffect, useState } from 'react';
import { api } from './api.js';

const short = value => value ? value.slice(0, 12) : 'Not recorded';

export default function Operating({id, disabled, onUpdated, onOpenLane}) {
  const [state, setState] = useState(null), [manifest, setManifest] = useState(null);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [recovery, setRecovery] = useState(null);
  const refresh = useCallback(async () => {
    const [git, app, coverage] = await Promise.all([api(`/projects/${id}/operating`), api(`/projects/${id}/app`), api(`/projects/${id}/recovery-status`)]);
    setState(git); setManifest(app); setRecovery(coverage);
  }, [id]);
  useEffect(() => {
    let active = true;
    const load = () => { if (active && !document.hidden) refresh().catch(e => setError(e.message)); };
    load();
    window.addEventListener('unforge-workspace-changed', load);
    return () => { active = false; window.removeEventListener('unforge-workspace-changed', load); };
  }, [refresh]);
  async function observe(destination) {
    setBusy(true); setError('');
    try { await api(`/projects/${id}/releases/observe`, {destinationId: destination.id, tree: state.productionVersion || state.sourceVersion}); await refresh(); }
    catch (e) { setError(e.message); await refresh().catch(() => {}); }
    finally { setBusy(false); }
  }
  async function fromProduction() {
    setBusy(true); setError('');
    try { const lane = await api(`/projects/${id}/lanes`, {name: 'Work from production', baseRef: state.productionRef}); await onUpdated(); if (onOpenLane) onOpenLane(lane.id); else await refresh(); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  if (!state || !manifest) return <p role={error ? 'alert' : 'status'}>{error || 'Reading operating state…'}</p>;
  return <section className="operating-state" aria-label="Operating state">
    <div className="section-heading"><h2>Operating state</h2><button className="text-button" disabled={busy || disabled} onClick={() => refresh().catch(e => setError(e.message))}>Refresh</button></div>
    {error && <p className="error" role="alert">{error}</p>}
    <dl className="operating-grid">
      <div><dt>Working source</dt><dd><code>{short(state.sourceVersion)}</code></dd><dd>{state.branch} · {state.sourceDirty ? 'Unsaved changes' : 'Saved'}</dd></div>
      <div><dt>Production source</dt><dd><code>{short(state.productionVersion)}</code></dd>{state.productionRef && <dd>{state.productionRef}</dd>}</div>
      <div><dt>Observed deployment</dt><dd><code>{short(manifest.liveVersion)}</code></dd>{manifest.observedAt && <dd>{new Date(manifest.observedAt).toLocaleString()}</dd>}</div>
      <div><dt>Checks</dt><dd>{(manifest.checksStatus || 'not-configured').replaceAll('-', ' ')}</dd></div>
    </dl>
    {state.productionError && <p className="error">{state.productionError}</p>}
    {state.divergence?.productionOnly > 0 && <p className="notice">Working source is missing {state.divergence.productionOnly} production commits; it has {state.divergence.workingOnly} separate commits. <button className="text-button" disabled={busy || disabled} onClick={fromProduction}>Open a lane from production</button></p>}
    {!!manifest.destinations?.length && <div className="inline-actions">{manifest.destinations.map(destination => <span key={destination.id}><button className="secondary" disabled={busy || disabled} onClick={() => observe(destination)}>Observe {destination.id}</button>{/^https?:\/\//.test(destination.url || '') && <a className="text-button" href={destination.url} target="_blank" rel="noreferrer">Open app</a>}</span>)}</div>}
    {manifest.readiness && <p className={manifest.readiness.ok ? 'notice' : 'error'} role="status">Readiness: {manifest.readiness.ok ? 'Ready' : 'Needs attention'}{manifest.readiness.observedAt ? ` · ${new Date(manifest.readiness.observedAt).toLocaleString()}` : ''}{manifest.readiness.error ? ` · ${manifest.readiness.error}` : ''}</p>}
    {!!recovery?.adapters?.length && <section><h3>Hosted-data recovery</h3>{recovery.adapters.map(adapter => <p key={adapter.id} className={adapter.stale || adapter.state === 'failed' ? 'error' : 'notice'}><strong>{adapter.label}: {adapter.stale ? 'Stale' : 'Captured recently'}</strong>{adapter.cloudSnapshotAt ? ` · ${new Date(adapter.cloudSnapshotAt).toLocaleString()}` : ' · Capture not recorded'}{adapter.error ? ` · ${adapter.error}` : ''}<br/>{adapter.cloudUploadedAt ? `Upload reported ${new Date(adapter.cloudUploadedAt).toLocaleString()}. ` : ''}{adapter.restoreTested ? 'Restore test recorded.' : 'Application restore untested.'}</p>)}</section>}
    {!!state.worktrees?.length && <details><summary>{state.worktrees.length} Git worktree{state.worktrees.length === 1 ? '' : 's'}</summary>{state.worktrees.map(tree => <p key={tree.path}><code>{tree.path}</code><br/>{tree.branch || 'Detached'} · {short(tree.head)}{!tree.present ? ' · Path missing' : ''}{tree.prunable ? ' · Prunable registration' : ''}{tree.locked ? ' · Locked' : ''}</p>)}</details>}
  </section>;
}
