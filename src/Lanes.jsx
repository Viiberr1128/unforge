import { useEffect, useState } from 'react';
import { api } from './api.js';
import { useLeaveGuard } from './useLeaveGuard.js';

export default function Lanes({id, dirty, onBlocked, onUpdated}) {
  const [state, setState] = useState(null);
  const [app, setApp] = useState(null);
  const [releases, setReleases] = useState(null);
  const [name, setName] = useState('');
  const [parent, setParent] = useState('');
  const [selected, setSelected] = useState(null);
  const [path, setPath] = useState('README.md');
  const [content, setContent] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [editing, setEditing] = useState(false);
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
  }
  useEffect(() => { refresh().catch(e => setError(e.message)); }, [id]);
  async function run(action, success) {
    setBusy(true); setError(''); setNotice('');
    try { await action(); await refresh(); await onUpdated(); setNotice(success); setEditing(false); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  if (!state || !app) return <p role={error ? 'alert' : 'status'}>{error || 'Opening lanes…'}</p>;
  const open = state.lanes.filter(item => item.status !== 'closed');
  return <section className="project-care">
    <div className="section-heading"><div><h2>Lanes, merge, and live ship.</h2><p>Each lane is its own Git worktree. Merge lands on live source. Publishing is not done until the bound site serves that version.</p></div></div>
    {error && <p role="alert" className="error">{error}</p>}
    {notice && <p role="status" className="notice">{notice}</p>}
    <div className={app.githubAbsent ? 'notice' : 'error'} role="status">
      <strong>{app.githubAbsent ? 'This app can work without GitHub.' : 'GitHub is still required for this app.'}</strong>
      {app.blockers.map(item => <p key={item}>{item}</p>)}
    </div>
    {dirty && <p className="notice">Save the live version before opening or merging a lane.</p>}
    <form className="brief-form" onSubmit={event => { event.preventDefault(); run(() => api(`/projects/${id}/lanes`, {name, parent: parent || null}), 'Lane opened. Work here; live files stay untouched.'); setName(''); }}>
      <label>New lane<input value={name} disabled={busy || dirty} onChange={event => setName(event.target.value)} placeholder="Fix the save button"/></label>
      <label>Stack on (optional)<select value={parent} disabled={busy} onChange={event => setParent(event.target.value)}>
        <option value="">Live version</option>
        {open.filter(item => item.status !== 'merged').map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label>
      <div className="form-actions"><button disabled={busy || dirty || !name.trim()}>Open lane</button></div>
    </form>
    <div className="care-rows">{open.map(item => <article key={item.id}>
      <div className="section-heading"><div><h3>{item.name} · {item.status.replace('_', ' ')}</h3><p>{item.changedFiles?.length || 0} changed files{item.parent ? ' · stacked' : ''}</p></div>
        <span className={`status-label ${item.status === 'merged' ? 'checked' : ''}`}>{item.status.replace('_', ' ')}</span></div>
      {item.conflictFiles?.length ? <p className="error">Overlaps: {item.conflictFiles.join(', ')}</p> : null}
      <div className="inline-actions">
        <button className="secondary" disabled={busy} onClick={() => { setSelected(item); setError(''); setNotice(''); }}>Open</button>
        {item.status === 'needs_restack' || item.status === 'conflict' ? <button className="secondary" disabled={busy || dirty} onClick={() => run(() => api(`/projects/${id}/lanes/${item.id}/restack`, {}), 'Lane restacked onto the current live version.')}>Restack</button> : null}
        {item.status !== 'merged' ? <button disabled={busy || dirty} onClick={() => run(() => api(`/projects/${id}/lanes/${item.id}/merge`, {}), 'Lane merged. Run checks and publish to put it on the live site.')}>Merge</button> : null}
      </div>
    </article>)}</div>
    {selected && selected.status !== 'merged' && <form className="brief-form" onSubmit={event => { event.preventDefault(); run(async () => { await api(`/projects/${id}/lanes/${selected.id}/file`, {path, content}); await api(`/projects/${id}/lanes/${selected.id}/save`, {message: message || 'Update lane'}); }, 'Lane saved. Live source is unchanged until you merge.'); }}>
      <h3>Edit {selected.name}</h3>
      <label>File path<input value={path} disabled={busy} onChange={event => { setPath(event.target.value); setEditing(true); }}/></label>
      <label>File text<textarea rows={8} value={content} disabled={busy} onChange={event => { setContent(event.target.value); setEditing(true); }}/></label>
      <label>What changed?<input value={message} disabled={busy} onChange={event => setMessage(event.target.value)} placeholder="Describe the change"/></label>
      <div className="form-actions"><button disabled={busy || !path.trim()}>Write and save in this lane</button></div>
    </form>}
    <div className="inline-actions">
      <button className="secondary" disabled={busy} onClick={() => run(() => api(`/projects/${id}/github/import`, {}), 'GitHub run steps are now local checks. Deploy steps stay on your own host.')}>Import GitHub Actions</button>
      <button className="secondary" disabled={busy} onClick={() => run(() => api(`/projects/${id}/github/archive`, {}), 'GitHub remotes removed and workflow files parked. Save a version.')}>Leave GitHub for this app</button>
    </div>
    <form className="brief-form" onSubmit={event => { event.preventDefault(); run(() => api(`/projects/${id}/exchange/export`, {laneId: selected?.id, title: handoff.title, note: handoff.note, path: handoff.path}), 'Change file written. Send it any way you send a file.'); }}>
      <h3>Send a change</h3>
      <p className="note">No GitHub. Save a lane, write a file, give it to a friend. They import it as a lane on their Mac.</p>
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
      <p className="note">Unforge does not provide a shared Cloudflare or database. Connect the account you already own. Any domain works.</p>
      <label>Kind<select value={host.type} disabled={busy} onChange={event => setHost(current => ({...current, type: event.target.value}))}>
        <option value="cloudflare-pages">Cloudflare Pages (your account)</option>
        <option value="supabase-functions">Supabase functions (your project)</option>
        <option value="local">Folder on this computer</option>
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
      <div className="form-actions"><button disabled={busy || !host.id.trim()}>Save destination</button></div>
    </form>
    <div className="inline-actions">
      <button className="secondary" disabled={busy} onClick={() => run(() => api(`/projects/${id}/checks`, {}), 'Checks finished. A passing receipt is required before publish.')}>Run checks</button>
      {app.destinations?.map(item => <button key={item.id} disabled={busy} onClick={() => run(() => api(`/projects/${id}/releases/publish`, {destinationId: item.id}), `Observed ${item.id}. Confirm the live flow yourself.`)}>Publish {item.id}</button>)}
    </div>
    {releases?.lastObserved && <p className="note">Last observed live version {releases.lastObserved.tree.slice(0, 12)} at {releases.lastObserved.observedAt}.</p>}
  </section>;
}
