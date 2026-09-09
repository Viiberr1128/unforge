import { useEffect, useState } from 'react';
import { api, importBundle } from './api.js';
import { Icon } from './ui.jsx';
import Projects, { CreateProject } from './Projects.jsx';
import Project from './Project.jsx';
import Ownership from './Ownership.jsx';
import ImportProject from './ImportProject.jsx';
import Preferences from './Preferences.jsx';

export default function App() {
  const [projects, setProjects] = useState([]);
  const [view, setView] = useState('projects');
  const [selected, setSelected] = useState(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [navigationBlocked, setNavigationBlocked] = useState(false);
  const [options, setOptions] = useState(() => {try {const saved = JSON.parse(localStorage.getItem('unforge-display') || '{}'); return {largeText:!!saved.largeText,technical:!!saved.technical};} catch {return {largeText:false,technical:false};}});
  function updateOptions(value) {setOptions(value); try {localStorage.setItem('unforge-display',JSON.stringify(value));} catch {setError('This browser could not save your display preferences. They apply for this session.');}}
  async function refresh() { const result = await api('/projects'); setProjects(Array.isArray(result) ? result : result.projects); }
  useEffect(() => { refresh().catch(e => setError(e.message)).finally(() => setLoading(false)); }, []);
  function home() { setSelected(null); setView('projects'); refresh().catch(e => setError(e.message)); }
  async function create(body) { const result = await api('/projects', body); setCreating(false); setSelected(result.id); setView('projects'); refresh().catch(() => setError('Project created. The project list could not refresh yet.')); }
  async function importProject(file) { const result = await importBundle(file); setImporting(false); setSelected(result.id); setView('projects'); refresh().catch(() => setError('Project imported. The project list could not refresh yet.')); }
  return <div className={`app-shell ${options.largeText ? 'comfortable' : ''} ${options.technical ? 'show-technical' : ''}`}>
    <a className="skip-link" href="#main">Skip to content</a>
    <aside className="sidebar"><div className="brand"><svg viewBox="0 0 42 48" width="36" height="42" aria-hidden="true"><path d="M5 4v25a16 16 0 0 0 32 0V4H27v25a6 6 0 0 1-12 0V4Z" fill="none" stroke="currentColor" strokeWidth="2.4"/></svg><span>unforge</span></div>
      <nav aria-label="Workspace"><button className={view === 'projects' ? 'selected' : ''} disabled={navigationBlocked} onClick={home}><Icon name="folder"/>Your projects</button><button className={view === 'ownership' ? 'selected' : ''} disabled={navigationBlocked} onClick={() => {setSelected(null); setView('ownership');}}><Icon name="book"/>How ownership works</button><button className={view === 'preferences' ? 'selected' : ''} disabled={navigationBlocked} onClick={() => {setSelected(null); setView('preferences');}}><Icon name="preferences"/>Make it comfortable</button></nav>
      <div className="sidebar-foot"><span className="dot"/>On this computer</div>
    </aside>
    <main id="main" className={selected ? 'workspace' : ''}>
      {error && <div className="error" role="alert">{error} <button className="text-button" onClick={() => {setError(''); refresh().catch(e => setError(e.message));}}>Try again</button></div>}
      {selected ? <Project key={selected} id={selected} back={home} onUpdated={refresh} onNavigationBlocked={setNavigationBlocked}/> : view === 'ownership' ? <Ownership back={home}/> : view === 'preferences' ? <Preferences options={options} update={updateOptions} back={home}/> : <Projects projects={projects} loading={loading} openProject={setSelected} openOwnership={() => setView('ownership')} create={() => setCreating(true)} importProject={() => setImporting(true)}/>}
    </main>
    {creating && <CreateProject onCreate={create} onClose={() => setCreating(false)}/>}
    {importing && <ImportProject onImport={importProject} onClose={() => setImporting(false)}/>}
  </div>;
}
