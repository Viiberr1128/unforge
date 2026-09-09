import { useState } from 'react';
import { Icon, Modal, relativeDate } from './ui.jsx';

export function CreateProject({ onCreate, onClose }) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError('');
    try { await onCreate({ name: name.trim(), description: description.trim() }); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  return <Modal title="Start something useful." onClose={onClose} busy={busy}>
    <p>Your project starts as a folder on this computer, with a simple page you can edit and a first saved version.</p>
    <form onSubmit={submit}>
      <label>Project name<input autoFocus value={name} maxLength={80} onChange={e => setName(e.target.value)} placeholder="My first idea" required/></label>
      <label>What would you like it to do?<textarea rows={3} value={description} maxLength={1000} onChange={e => setDescription(e.target.value)} placeholder="A place to start something useful."/></label>
      <p className="note">This creates a starter, not an AI-generated application. No cloud account or connected service is needed.</p>
      {error && <p role="alert" className="error">{error}</p>}
      <div className="form-actions"><button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button><button disabled={busy || !name.trim()}>{busy ? 'Creating…' : 'Create project'}<Icon name="arrow"/></button></div>
    </form>
  </Modal>;
}

export default function Projects({ projects, openProject, openOwnership, create, loading, importProject }) {
  return <>
    <header className="page-top"><span>Your projects</span><button onClick={create}><Icon name="plus"/>New project</button></header>
    <section className="intro"><h1>A home for what you build.</h1><p>Your files. Your history. Your next idea.</p></section>
    <section aria-label="Projects" className="project-list">
      <div className="list-labels"><span>Project</span><span>Last saved</span><span/></div>
      {loading ? <p className="empty">Opening your workspace…</p> : projects.length ? projects.map(project => <button className="project-row" key={project.id} onClick={() => openProject(project.id)}>
        <span className="project-identity"><span className="initial">{project.name.slice(0, 1).toUpperCase()}</span><span><strong>{project.name}</strong><small>{project.description || 'A place to start something useful.'}</small></span></span>
        <span className="saved-at">{relativeDate(project.updatedAt || project.updated_at || project.lastSaved)}</span><Icon name="arrow"/>
      </button>) : <div className="empty"><h2>Something useful starts here.</h2><p>Create your first project. Everything stays on this computer.</p><button className="text-button" onClick={create}>Start your first project<Icon name="arrow"/></button></div>}
    </section>
    <div className="import-line"><span>Already have a project?</span><button className="text-button" onClick={importProject}>Bring an existing app<Icon name="arrow" size={16}/></button></div>
    <section className="ownership-banner"><div><h2>Yours, all the way down.</h2><p>Projects are ordinary folders with Git history. Keep working without an account or a cloud service.</p></div><button className="text-button" onClick={openOwnership}>Explore ownership<Icon name="arrow"/></button></section>
    <footer>Local workspace · Optional Codex assistance</footer>
  </>;
}
