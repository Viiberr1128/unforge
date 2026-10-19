import { useEffect, useRef, useState } from 'react';
import { api } from './api.js';
import Consequences from './Consequences.jsx';
import { useLeaveGuard } from './useLeaveGuard.js';

export default function Lanes({id, dirty, onBlocked, onUpdated, initialLaneId}) {
  const [state, setState] = useState(null);
  const [app, setApp] = useState(null);
  const [releases, setReleases] = useState(null);
  const [name, setName] = useState('');
  const [claims, setClaims] = useState('');
  const [parent, setParent] = useState('');
  const [selected, setSelected] = useState(null);
  const [path, setPath] = useState('README.md');
  const [content, setContent] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [editing, setEditing] = useState(false);
  const [loaded, setLoaded] = useState(false), [baseContent, setBaseContent] = useState(null);
  const [conflicts, setConflicts] = useState(null), [resolutions, setResolutions] = useState({});
  const [review, setReview] = useState(null);
  const readEpoch = useRef(0), active = useRef(true);
  const resolutionDrafts = useRef({});
  useEffect(() => { active.current = true; return () => {active.current = false; readEpoch.current += 1;}; }, []);
  const [host, setHost] = useState({id: 'web', type: 'cloudflare-pages', project: '', url: '', directory: 'dist'});
  const [handoff, setHandoff] = useState({title: '', note: '', path: '', incoming: ''});
  useLeaveGuard(editing || busy, onBlocked);
  async function refresh() {
    const [lanes, manifest, published] = await Promise.all([
      api(`/projects/${id}/lanes`),
      api(`/projects/${id}/app`),
      api(`/projects/${id}/releases`),
    ]);
    setState(lanes);
    setApp(manifest);
    setReleases(published);
    if (selected) {
      const current = lanes.lanes.find(item => item.id === selected.id);
      if (current) setSelected(current);
    }
    return lanes;
  }
  useEffect(() => {
    let current = true;
    refresh().then(value => { const lane = value.lanes.find(item => item.id === initialLaneId); if (current && lane) return openLane(lane); }).catch(e => {if (current) setError(e.message);});
    return () => {current = false;};
  }, [id, initialLaneId]);
  async function loadFile(item = selected, manageBusy = true, epoch = ++readEpoch.current) {
    if (manageBusy) setBusy(true); setError('');
    try {
      const value = await api(`/projects/${id}/lanes/${item.id}/content?path=${encodeURIComponent(path)}`);
      if (active.current && epoch === readEpoch.current) {setBaseContent(value.content); setContent(value.content ?? ''); setLoaded(true); setEditing(false);}
    } catch (e) { if (active.current && epoch === readEpoch.current) {setError(e.message); setLoaded(false);} }
    finally { if (manageBusy && active.current && epoch === readEpoch.current) setBusy(false); }
  }
  async function openLane(item, manageBusy = true) {
    const epoch = ++readEpoch.current;
    if (manageBusy) setBusy(true);
    if (manageBusy) resolutionDrafts.current = {};
    setSelected(item); setError(''); setNotice(''); setLoaded(false); setConflicts(null); setEditing(false);
    try {
      if (item.status === 'conflict') {
        const value = await api(`/projects/${id}/lanes/${item.id}/conflicts`);
        if (active.current && epoch === readEpoch.current) {
          const paths = new Set(value.files.map(file => file.path));
          resolutionDrafts.current = Object.fromEntries(Object.entries(resolutionDrafts.current).filter(([file]) => paths.has(file)));
          setConflicts(value); setResolutions(Object.fromEntries(value.files.map(file => [file.path, resolutionDrafts.current[file.path]?.text ?? file.current ?? ''])));
        }
      } else await loadFile(item, false, epoch);
    } catch (e) { if (active.current && epoch === readEpoch.current) setError(e.message); }
    finally { if (manageBusy && active.current && epoch === readEpoch.current) setBusy(false); }
  }
  async function resolve(file, strategy) {
    await run(async () => {
      const value = await api(`/projects/${id}/lanes/${selected.id}/resolve`, {path: file.path, strategy, content: resolutions[file.path], expectedContent: file.current, revision: conflicts.revision});
      delete resolutionDrafts.current[file.path];
      await openLane(value, false);
      return {...value, editorLoaded:true};
    }, value => value.status === 'conflict' ? 'File resolved. Choose the remaining conflicts.' : 'Conflicts resolved and lane saved.');
  }
  async function reviewLane(item) {
    setBusy(true); setError(''); setNotice(''); setReview(null);
    try { setReview(await api(`/projects/${id}/lanes/${item.id}/review`)); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  async function run(action, success) {
    setBusy(true); setError(''); setNotice('');
    try { const result = await action(); await refresh(); await onUpdated(); setNotice(typeof success === 'function' ? success(result) : success); if (result?.editorLoaded) setEditing(Object.keys(resolutionDrafts.current).length > 0); if (result?.status === 'conflict' && !result.editorLoaded) await openLane(result, false); else if (result?.head && selected && !result.editorLoaded) {setLoaded(false);setEditing(false);} }
    catch (e) { setError(e.message); await refresh().catch(() => {}); }
    finally { setBusy(false); }
  }
  if (!state || !app) return <p role={error ? 'alert' : 'status'}>{error || 'Opening lanes…'}</p>;
  const open = state.lanes.filter(item => item.status !== 'closed');
  return <section className="project-care">
    <div className="section-heading"><h2>Lanes & ship</h2></div>
    {error && <p role="alert" className="error">{error}</p>}
    {notice && <p role="status" className="notice">{notice}</p>}
    <div className={app.githubAbsent ? 'notice' : 'error'} role="status">
      <strong>{app.githubAbsent ? 'Source, checks, and deployment match.' : 'Release verification needs attention.'}</strong>
      {app.blockers.map(item => <p key={item}>{item}</p>)}
    </div>
    {dirty && <p className="notice">Save the working source before merging.</p>}
    <form className="brief-form" onSubmit={event => { event.preventDefault(); run(() => api(`/projects/${id}/lanes`, {name, parent: parent || null, claimedPaths: claims.split('\n').map(value => value.trim()).filter(Boolean)}), 'Lane opened. Work here; live files stay untouched.'); setName(''); setClaims(''); }}>
      <label>New lane<input value={name} disabled={busy || dirty} onChange={event => setName(event.target.value)} placeholder="Fix the save button"/></label>
      <label>Stack on (optional)<select value={parent} disabled={busy} onChange={event => setParent(event.target.value)}>
        <option value="">Working source</option>
        {open.filter(item => item.status !== 'merged').map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label>
      <label>Claimed paths — folders end in / (optional)<textarea rows={3} value={claims} disabled={busy} onChange={event => setClaims(event.target.value)} placeholder={'src/\npackage.json'}/></label>
      <div className="form-actions"><button disabled={busy || dirty || !name.trim()}>Open lane</button></div>
    </form>
    <div className="care-rows">{open.map(item => <article key={item.id}>
      <div className="section-heading"><div><h3>{item.name} · {item.status.replace('_', ' ')}</h3><p>{item.changedFiles?.length || 0} changed files{item.parent ? ' · stacked' : ''}</p></div>
        <span className={`status-label ${item.status === 'merged' ? 'checked' : ''}`}>{item.status.replace('_', ' ')}</span></div>
      {item.conflictFiles?.length ? <p className="error">Overlaps: {item.conflictFiles.join(', ')}</p> : null}
      {item.claimConflicts?.length ? <p className="notice">Claims overlap: {item.claimConflicts.map(conflict => conflict.laneName).join(', ')}</p> : null}
      <div className="inline-actions">
        <button className="secondary" disabled={busy || editing} onClick={() => openLane(item)}>{item.status === 'conflict' ? 'Resolve conflicts' : 'Open'}</button>
        {item.status === 'needs_restack' ? <button className="secondary" disabled={busy || dirty || editing} onClick={() => run(() => api(`/projects/${id}/lanes/${item.id}/restack`, {}), result => result.status === 'conflict' ? 'Choose the conflicting contents below.' : 'Lane restacked onto working source.')}>Restack</button> : null}
        {item.status !== 'merged' ? <button disabled={busy || dirty || editing || item.status === 'conflict'} onClick={() => reviewLane(item)}>Review &amp; merge</button> : null}
      </div>
    </article>)}</div>
    {review && <section className="brief-form" aria-label={`Review ${review.name}`}>
      <div className="section-heading"><div><h3>Review {review.incomingTitle || review.name}</h3><p><code>{review.head.slice(0, 12)}</code> · {review.changedPathCount} changed {review.changedPathCount === 1 ? 'path' : 'paths'}</p></div><button className="secondary" disabled={busy} onClick={() => setReview(null)}>Close</button></div>
      {review.incomingNote && <p>{review.incomingNote}</p>}
      <p className={review.checks.status === 'passed' ? 'notice' : 'error'}>Checks: {review.checks.status.replaceAll('-', ' ')}{review.checks.error ? ` — ${review.checks.error}` : ''}</p>
      {!!review.claimedPaths.length && <p><strong>Claims:</strong> {review.claimedPaths.join(', ')}</p>}
      {!!review.outOfClaimPathCount && <p className="error"><strong>Outside claims:</strong> {review.outOfClaimPaths.join(', ')}{review.outOfClaimPathsTruncated ? ` and ${review.outOfClaimPathCount - review.outOfClaimPaths.length} more` : ''}</p>}
      {!!review.claimConflicts.length && <p className="notice"><strong>Overlapping lanes:</strong> {review.claimConflicts.map(conflict => conflict.laneName).join(', ')}</p>}
      {review.parentPending && <p className="error">Merge {review.parent.name} first.</p>}
      {review.needsRestack && <p className="error">Working source moved. Restack this lane, then review it again.</p>}
      {review.production?.version && <p><strong>Production:</strong> {review.production.relation?.kind || 'unknown'} · <code>{review.production.version.slice(0, 12)}</code></p>}
      <div className="impact-line"><strong>Changed paths</strong><ul>{review.changedPaths.slice(0, 100).map(item => <li key={item.path}><code>{item.status}</code> {item.path}</li>)}</ul>{(review.changedPathsTruncated || review.changedPaths.length > 100) && <p className="note">Showing 100 of {review.changedPathCount} paths.</p>}</div>
      {review.consequences ? <Consequences compact report={review.consequences}/> : <p className="notice">Consequences unavailable{review.consequenceError ? `: ${review.consequenceError}` : '.'}</p>}
      <details><summary>Raw diff</summary><pre>{review.diff || '(no text diff)'}</pre>{review.diffTruncated && <p className="note">Diff truncated. Digest: <code>{review.diffSha256}</code></p>}</details>
      {review.dirty && <p className="error">Save the lane before merging.</p>}
      <div className="form-actions">{review.needsRestack ? <button disabled={busy || dirty || review.dirty} onClick={() => run(async () => { const result = await api(`/projects/${id}/lanes/${review.laneId}/restack`, {}); setReview(null); return result; }, result => result.status === 'conflict' ? 'Choose the conflicting contents below.' : 'Lane restacked. Review the new candidate.')}>Restack lane</button> : <button disabled={busy || dirty || review.dirty || review.parentPending} onClick={() => run(async () => { try { const result = await api(`/projects/${id}/lanes/${review.laneId}/merge`, {expectedHead: review.head, expectedLive: review.liveHead, expectedReviewDigest: review.reviewDigest}); setReview(null); return result; } catch (error) { setReview(null); throw error; } }, `Lane merged into working source${review.outOfClaimPathCount ? ` with ${review.outOfClaimPathCount} outside-claim ${review.outOfClaimPathCount === 1 ? 'path' : 'paths'}` : ''}.`)}>{review.outOfClaimPathCount ? `Merge with ${review.outOfClaimPathCount} outside-claim ${review.outOfClaimPathCount === 1 ? 'path' : 'paths'}` : 'Merge reviewed lane'}</button>}</div>
    </section>}
    {selected?.status === 'conflict' && conflicts && <section><h3>Resolve {selected.name}</h3>{conflicts.files.map(file => <article className="request conflict-choice" key={file.path}><h4>{file.path}</h4>{resolutionDrafts.current[file.path] && resolutionDrafts.current[file.path].currentHash !== file.currentHash && <p className="notice">The file changed. Your draft is preserved. <button disabled={busy} onClick={() => {resolutionDrafts.current[file.path].currentHash = file.currentHash;setResolutions(current => ({...current}));}}>Use current contents as the base</button></p>}{file.readable && <details><summary>Compare contents</summary><h5>Lane</h5><pre>{file.ours ?? '(deleted)'}</pre><h5>New base</h5><pre>{file.theirs ?? '(deleted)'}</pre></details>}<div className="inline-actions"><button disabled={busy} onClick={() => resolve(file, 'ours')}>Keep lane version</button><button className="secondary" disabled={busy} onClick={() => resolve(file, 'theirs')}>Use new base version</button></div>{file.readable && <><label>Resolved text<textarea rows={6} value={resolutions[file.path] ?? ''} disabled={busy} onChange={event => {resolutionDrafts.current[file.path] = {text:event.target.value, currentHash:file.currentHash};setResolutions(current => ({...current, [file.path]: event.target.value}));setEditing(true);}}/></label><button disabled={busy || (resolutionDrafts.current[file.path] && resolutionDrafts.current[file.path].currentHash !== file.currentHash)} onClick={() => resolve(file, 'edit')}>Use edited text</button></>}</article>)}</section>}
    {selected && selected.status !== 'merged' && selected.status !== 'conflict' && <form className="brief-form" onSubmit={event => { event.preventDefault(); run(async () => { await api(`/projects/${id}/lanes/${selected.id}/file`, {path, content, expectedContent: baseContent}); return api(`/projects/${id}/lanes/${selected.id}/save`, {message: message || 'Update lane'}); }, 'Lane saved.'); }}>
      <h3>Edit {selected.name}</h3>
      <label>File path<input value={path} disabled={busy || editing} onChange={event => { setPath(event.target.value); setLoaded(false); }}/></label>
      <button type="button" className="secondary" disabled={busy || editing || !path.trim()} onClick={() => loadFile()}>Load file</button>
      <label>File text<textarea rows={8} value={loaded ? content : ''} disabled={busy || !loaded} onChange={event => { setContent(event.target.value); setEditing(true); }}/></label>
      <label>What changed?<input value={message} disabled={busy} onChange={event => setMessage(event.target.value)} placeholder="Describe the change"/></label>
      <div className="form-actions">{editing && <button type="button" className="secondary" disabled={busy} onClick={() => {setContent(baseContent ?? '');setEditing(false);}}>Discard edit</button>}<button disabled={busy || !loaded || !editing || !path.trim()}>Write and save in this lane</button></div>
    </form>}
    <details><summary>GitHub import and remotes</summary><div className="inline-actions">
      <button className="secondary" disabled={busy} onClick={() => run(() => api(`/projects/${id}/github/import`, {}), 'GitHub run steps are now local checks. Deploy steps stay on your own host.')}>Import GitHub Actions</button>
      <button className="secondary" disabled={busy} onClick={() => run(() => api(`/projects/${id}/github/archive`, {}), 'GitHub remotes removed and workflow files parked. Save a version.')}>Leave GitHub for this app</button>
    </div></details>
    <form className="brief-form" onSubmit={event => { event.preventDefault(); run(() => api(`/projects/${id}/exchange/export`, {laneId: selected?.id, title: handoff.title, note: handoff.note, path: handoff.path}), 'Change file written. Send it any way you send a file.'); }}>
      <h3>Send a change</h3>
      <label>Title<input value={handoff.title} disabled={busy} onChange={event => setHandoff(current => ({...current, title: event.target.value}))} placeholder="Fix the save button"/></label>
      <label>Save as<input value={handoff.path} disabled={busy} onChange={event => setHandoff(current => ({...current, path: event.target.value}))} placeholder="/Users/you/Desktop/change.unforge-change"/></label>
      <div className="form-actions"><button disabled={busy || !selected || !handoff.title.trim() || !handoff.path.trim()}>Write change file</button></div>
    </form>
    <form className="brief-form" onSubmit={event => { event.preventDefault(); run(() => api(`/projects/${id}/exchange/import`, {path: handoff.incoming}), 'Change imported as a lane. Review, then merge.'); }}>
      <label>Open a change file<input value={handoff.incoming} disabled={busy} onChange={event => setHandoff(current => ({...current, incoming: event.target.value}))} placeholder="/absolute/path/to/change.unforge-change"/></label>
      <div className="form-actions"><button disabled={busy || !handoff.incoming.trim()}>Import change</button></div>
    </form>
    <form className="brief-form" onSubmit={event => { event.preventDefault(); run(() => api(`/projects/${id}/releases/bind`, {destination: host}), 'Destination saved. Sign in to Wrangler or the Supabase CLI with YOUR account before publishing.'); }}>
      <h3>Your host</h3>
      <label>Kind<select value={host.type} disabled={busy} onChange={event => setHost(current => ({...current, type: event.target.value}))}>
        <option value="cloudflare-pages">Cloudflare Pages (your account)</option>
        <option value="supabase-functions">Supabase functions (your project)</option>
        <option value="local">Folder on this computer</option>
        <option value="fly">Fly (observe existing deployment)</option>
        <option value="cloudflare-worker">Cloudflare Worker (observe existing deployment)</option>
      </select></label>
      <label>Name<input value={host.id} disabled={busy} onChange={event => setHost(current => ({...current, id: event.target.value}))} placeholder="web"/></label>
      {host.type === 'cloudflare-pages' ? <>
        <label>Pages project<input value={host.project} disabled={busy} onChange={event => setHost(current => ({...current, project: event.target.value}))} placeholder="my-app"/></label>
        <label>Public URL<input value={host.url} disabled={busy} onChange={event => setHost(current => ({...current, url: event.target.value}))} placeholder="https://my-app.com"/></label>
        <label>Static folder<input value={host.directory} disabled={busy} onChange={event => setHost(current => ({...current, directory: event.target.value}))} placeholder="dist"/></label>
      </> : null}
      {host.type === 'supabase-functions' ? <>
        <label>Your project ref<input value={host.projectRef || ''} disabled={busy} onChange={event => setHost(current => ({...current, projectRef: event.target.value}))} placeholder="your 20-character ref"/></label>
        <label>Public URL<input value={host.url} disabled={busy} onChange={event => setHost(current => ({...current, url: event.target.value}))} placeholder="https://my-app.com"/></label>
      </> : null}
      {host.type === 'local' ? <label>Folder<input value={host.path || ''} disabled={busy} onChange={event => setHost(current => ({...current, path: event.target.value}))} placeholder="/absolute/or/relative/folder"/></label> : null}
      {host.type === 'fly' && <label>Fly app<input value={host.app || ''} disabled={busy} onChange={event => setHost(current => ({...current, app: event.target.value}))}/></label>}
      {host.type === 'cloudflare-worker' && <><label>Worker<input value={host.worker || ''} disabled={busy} onChange={event => setHost(current => ({...current, worker: event.target.value}))}/></label><label>Wrangler profile<input value={host.profile || ''} disabled={busy} onChange={event => setHost(current => ({...current, profile: event.target.value}))}/></label></>}
      {['fly','cloudflare-worker'].includes(host.type) && <label>Public URL<input value={host.url || ''} disabled={busy} onChange={event => setHost(current => ({...current, url: event.target.value}))}/></label>}
      {host.type !== 'local' && <label>Readiness URL (optional)<input value={host.readinessUrl || ''} disabled={busy} onChange={event => setHost(current => ({...current, readinessUrl: event.target.value}))}/></label>}
      <div className="form-actions"><button disabled={busy || !host.id.trim()}>Save destination</button></div>
    </form>
    <div className="inline-actions">
      <button className="secondary" disabled={busy || editing || (selected && selected.status !== 'merged' ? selected.dirty : dirty)} onClick={() => run(() => api(`/projects/${id}/checks`, {laneId: selected?.status !== 'merged' ? selected?.id : null}), result => result.status === 'passed' ? 'Checks passed.' : result.status === 'not-configured' ? 'Configure meaningful checks in .unforge/app.json.' : 'Checks failed. Read their output in the receipt.')}>Run checks</button>
      {app.destinations?.map(item => <span key={item.id}><button className="secondary" disabled={busy} onClick={() => run(() => api(`/projects/${id}/releases/observe`, {destinationId: item.id}), `Deployment observed for ${item.id}.`)}>Observe {item.id}</button>{!['fly','cloudflare-worker'].includes(item.type) && <><button disabled={busy || dirty} onClick={() => run(() => api(`/projects/${id}/releases/publish`, {destinationId: item.id}), `Published and observed ${item.id}.`)}>Publish {item.id}</button><button className="secondary" disabled={busy || dirty} onClick={() => run(() => api(`/projects/${id}/releases/rollback`, {destinationId: item.id}), `Earlier artifact restored for ${item.id}.`)}>Roll back {item.id}</button></>}</span>)}
    </div>
    {releases?.lastObserved && <p className="note">Last observed live version {releases.lastObserved.tree.slice(0, 12)} at {releases.lastObserved.observedAt}.</p>}
  </section>;
}
