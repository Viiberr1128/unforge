import { useEffect, useState } from 'react';
import { api } from './api.js';
import { Icon, Modal, relativeDate } from './ui.jsx';
import { previewDocument } from './preview.js';
import Footprint from './Footprint.jsx';
import AgentWork from './AgentWork.jsx';

export default function Project({ id, back, onUpdated, onNavigationBlocked }) {
  const [project, setProject] = useState(null);
  const [tab, setTab] = useState('Overview');
  const [file, setFile] = useState('README.md');
  const [content, setContent] = useState('');
  const [draft, setDraft] = useState(false);
  const [dialog, setDialog] = useState(null);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  async function load() {
    const value = await api(`/projects/${id}`);
    setProject(value); return value;
  }
  useEffect(() => { load().catch(e => setError(e.message)); }, [id]);
  useEffect(() => { onNavigationBlocked(draft || busy); return () => onNavigationBlocked(false); }, [draft, busy, onNavigationBlocked]);
  useEffect(() => {
    const warn = event => { if (draft) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [draft]);
  useEffect(() => {
    if (project && !draft) setContent(project.files.find(entry => entry.path === file)?.content || '');
  }, [project, file, draft]);
  async function act(action, success) {
    setBusy(true); setError(''); setNotice('');
    let completed = false;
    try { const result = await action(); completed = true; setDialog(null); setNotice(success); if (result?.files) setProject(result); else await load(); onUpdated().catch(() => {}); }
    catch(e) { setError(completed ? `Action completed, but the view could not refresh: ${e.message}` : e.message); }
    finally { setBusy(false); }
  }
  function show(kind, value = '') { setInput(value); setDialog(kind); setError(''); }
  function chooseFile(next) { if (draft || busy) return; setFile(next); }
  async function writeFile() {
    await act(async () => { const result = await api(`/projects/${id}/file`, {path: file, content, expectedContent: project.files.find(entry => entry.path === file)?.content ?? null}); setDraft(false); return result; }, 'File written to this computer. Save a version to add it to history.');
  }
  async function submit(event) {
    event.preventDefault();
    if (dialog === 'discard') { setBusy(true); try { await load(); setDraft(false); setDialog(null); setError(''); setNotice('Editor reloaded from the file on this computer.'); } catch(e) {setError(e.message);} finally {setBusy(false);} return; }
    if (dialog === 'save') await act(() => api(`/projects/${id}/save`, {message: input}), 'Version saved. Your history is up to date.');
    if (dialog === 'request') await act(() => api(`/projects/${id}/request`, {message: input}), 'Request written to your project. Save a version to include it in your history.');
    if (dialog === 'file') await act(async () => { const result = await api(`/projects/${id}/file`, {path: input, content: '', expectedContent: null}); setFile(input); return result; }, 'File created. You can edit it below.');
    if (dialog?.revision) await act(() => api(`/projects/${id}/restore`, {revision: dialog.revision}), 'Earlier files restored as a new version. Your previous versions remain available.');
  }
  if (!project) return <><button className="text-button" onClick={back}><Icon name="back"/>Your projects</button><p role={error ? 'alert' : 'status'}>{error || 'Opening project…'}</p></>;
  const html = project.files.find(f => f.path === 'index.html')?.content;
  const readonly = project.files.find(f => f.path === file)?.readonly;
  const requests = project.files.filter(f => f.path.startsWith('.unforge/requests/'));
  return <>
    <header className="page-top"><button className="text-button" onClick={back} disabled={draft || busy}><Icon name="back"/>Your projects</button><span className="local-state"><span className={`dot ${project.dirty || draft ? 'amber' : ''}`}/>{draft ? 'Editor has unwritten changes' : project.dirty ? 'Changes ready to save' : 'All changes saved'}</span></header>
    <section className="project-heading"><div><h1>{project.name}</h1><p>{project.description || 'A place to start something useful.'}</p></div><button onClick={() => show('save')} disabled={busy || draft || !project.dirty}><Icon name="check"/>Save a version</button></section>
    <nav className="tabs" aria-label="Project views">{['Overview', 'Files', 'History', 'Dependencies', 'Take it with you'].map(label => <button key={label} disabled={busy || (draft && label !== tab)} className={tab === label ? 'active' : ''} aria-current={tab === label ? 'page' : undefined} onClick={() => {setTab(label); setNotice('');}}>{label}</button>)}</nav>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="notice">{notice}</p>}
    {tab === 'Overview' && <div className="overview">
      <div className="section-heading"><div><h2>A little room to experiment.</h2><p>A static preview of your project’s index.html. Scripts and external resources are disabled.</p></div><button className="secondary" onClick={() => {chooseFile('index.html'); setTab('Files');}}>Edit this page<Icon name="arrow"/></button></div>
      {html ? <iframe title="Project static preview" className="preview" sandbox="" referrerPolicy="no-referrer" srcDoc={previewDocument(html)}/> : <div className="empty"><p>Add index.html in Files to preview a page here.</p></div>}
      <AgentWork id={id} dirty={project.dirty} onApplied={async () => {await load(); await onUpdated();}}/>
      <section className="request-band"><div><h2>What would make this more useful?</h2><p>Write a clear request for your coding agent. It stays in the project as an ordinary file.</p></div><button className="secondary" onClick={() => show('request')}>Write a request<Icon name="arrow"/></button></section>
      {requests.length > 0 && <details><summary>{requests.length} saved request{requests.length !== 1 ? 's' : ''}</summary>{requests.map(r => <article className="request" key={r.path}><small>{r.path}</small><pre>{r.content}</pre></article>)}</details>}
      <p className="note">Request files are portable handoffs. They do not start an agent automatically.</p>
    </div>}
    {tab === 'Files' && <>
      <div className="section-heading"><div><h2>Everything is a file.</h2><p>{draft ? 'Write your changes before switching files or views.' : 'Edit text here, or use your own editor.'}</p></div><button className="secondary" disabled={draft || busy} onClick={() => show('file')}>New file<Icon name="plus"/></button></div>
      <div className="editor-layout"><nav className="file-list" aria-label="Project files">{project.files.map(f => <button key={f.path} className={f.path === file ? 'selected' : ''} disabled={busy || (draft && f.path !== file)} onClick={() => chooseFile(f.path)}><Icon name="file" size={16}/><span>{f.path}</span></button>)}</nav>
        <div className="editor"><label htmlFor="file-content">{file}{readonly ? ' · Read only' : ''}</label><textarea id="file-content" spellCheck={false} value={content} readOnly={readonly} disabled={busy} onChange={e => {setContent(e.target.value); setDraft(true);}}/><div className="editor-actions"><span className="note">Writing updates the file. Saving a version records its history.</span>{draft && <button className="text-button" disabled={busy} onClick={() => show('discard')}>Reload file</button>}<button disabled={!draft || busy || readonly} onClick={writeFile}>{busy ? 'Writing…' : 'Write file'}</button></div></div>
      </div>
      <details className="diff"><summary>Changes since your last saved version</summary><pre>{project.diff || (project.dirty ? 'New files have not been saved in a version yet.' : 'No changes since the last saved version.')}</pre></details>
    </>}
    {tab === 'History' && <>
      <div className="section-heading"><div><h2>A history you can return to.</h2><p>Restoring adds a new version. It never erases the versions that came after.</p></div></div>
      {project.dirty && <p className="notice">Save your current changes before restoring an earlier version.</p>}
      <ol className="history">{project.history.map((version, index) => <li key={version.id}><span className="timeline-dot"/><div><h3>{version.message}</h3><p>{relativeDate(version.date)}<span className="technical"> · <code>{version.id.slice(0, 8)}</code></span>{index === 0 ? ' · Current version' : ''}</p></div>{index > 0 && <button className="secondary" disabled={busy || project.dirty} onClick={() => show({revision: version.id, message: version.message})}>Restore</button>}</li>)}</ol>
    </>}
    {tab === 'Dependencies' && <Footprint id={id}/>}
    {tab === 'Take it with you' && <div className="export-view"><h2>No permission needed to leave.</h2><p>Download your committed files, requests, and complete Git history in a portable bundle.</p>{project.dirty && <p className="notice">Your latest file changes are not in history yet. Save a version before exporting.</p>}<a className={`button ${project.dirty ? 'disabled' : ''}`} aria-disabled={project.dirty} href={project.dirty ? undefined : `/api/projects/${id}/export`} download><Icon name="export"/>Download project history</a><h3>Open it without Unforge</h3><code className="command">git clone your-project.bundle my-project</code><p>Or use “Open a project bundle” on another Unforge workspace.</p><h3>Your working folder</h3><code className="command">{project.path}</code><p className="note">Keep the bundle on another device for an independent copy. It excludes unwritten editor changes, uncommitted files, external databases, separately stored credentials, and external large-file objects. Any secrets already committed remain in history; export is not a secret scanner.</p></div>}
    <footer>On this computer<span className="technical"> · {project.path}</span></footer>
    {dialog && <Modal title={dialog === 'save' ? 'Keep this version.' : dialog === 'request' ? 'Describe the next useful change.' : dialog === 'file' ? 'Add a file.' : dialog === 'discard' ? 'Reload this file?' : 'Return to these files?'} busy={busy} onClose={() => setDialog(null)}>
      <form onSubmit={submit}>
        {dialog === 'discard' ? <p>This discards the unwritten text in this editor and loads the current file. Copy any draft text you want to keep before continuing.</p> : dialog?.revision ? <><p>Restore “{dialog.message}” as a new saved version. All existing versions remain in history.</p><p className="note">This changes this project’s tracked files. It does not reverse database changes, deployments, or messages sent elsewhere.</p></> : <label>{dialog === 'save' ? 'What changed?' : dialog === 'request' ? 'What do you want to improve?' : 'File path'}{dialog === 'request' ? <textarea rows={5} autoFocus required value={input} maxLength={8000} onChange={e => setInput(e.target.value)} placeholder="Describe the outcome, an example, and how you will know it works."/> : <input autoFocus required value={input} maxLength={dialog === 'file' ? 200 : 500} onChange={e => setInput(e.target.value)} placeholder={dialog === 'file' ? 'notes.md' : 'Make the page easier to read'}/>}</label>}
        {dialog === 'request' && <p className="note">This writes a request file only. No model is called and no AI credits are spent.</p>}
        {error && <p className="error" role="alert">{error}</p>}
        <div className="form-actions"><button type="button" className="secondary" disabled={busy} onClick={() => setDialog(null)}>Cancel</button><button disabled={busy || (dialog !== 'discard' && !dialog?.revision && !input.trim())}>{busy ? 'Working…' : dialog === 'save' ? 'Save version' : dialog === 'request' ? 'Save request' : dialog === 'file' ? 'Create file' : dialog === 'discard' ? 'Discard draft and reload' : 'Restore as new version'}</button></div>
      </form>
    </Modal>}
  </>;
}
