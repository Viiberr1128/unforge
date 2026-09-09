import { useEffect, useState } from 'react';
import { api, importBundle } from './api.js';
import { Icon } from './ui.jsx';
import Projects, { CreateProject } from './Projects.jsx';
import Project from './Project.jsx';
import Ownership from './Ownership.jsx';
import ImportProject from './ImportProject.jsx';
import Preferences from './Preferences.jsx';
import Allowance from './Allowance.jsx';
import Backups from './Backups.jsx';
import BackupHealth from './BackupHealth.jsx';

export default function App() {
  const [projects, setProjects] = useState([]);
  const [projectProblems,setProjectProblems] = useState([]);
  const [view, setView] = useState('projects');
  const [selected, setSelected] = useState(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [navigationBlocked, setNavigationBlocked] = useState(false);
  useEffect(()=>{const show=event=>setError(event.detail);window.addEventListener('unforge-backup-warning',show);return()=>window.removeEventListener('unforge-backup-warning',show);},[]);
  // Read by the native window before reload or quit.
  useEffect(() => {
    window.unforgeDesktopHasDraft = navigationBlocked || creating || importing;
    return () => { window.unforgeDesktopHasDraft = false; };
  }, [navigationBlocked, creating, importing]);
  const [options, setOptions] = useState(() => {try {const saved = JSON.parse(localStorage.getItem('unforge-display') || '{}'); return {largeText:!!saved.largeText,technical:!!saved.technical};} catch {return {largeText:false,technical:false};}});
  function updateOptions(value) {setOptions(value); try {localStorage.setItem('unforge-display',JSON.stringify(value));} catch {setError('This browser could not save your display preferences. They apply for this session.');}}
  async function refresh() { const result = await api('/projects'); setProjects(Array.isArray(result) ? result : result.projects); setProjectProblems(result.problems || []); }
  useEffect(() => { refresh().catch(e => setError(e.message)).finally(() => setLoading(false)); }, []);
  function home() { setSelected(null); setView('projects'); refresh().catch(e => setError(e.message)); }
  async function create(body) { const result = await api('/projects', body); setCreating(false); setSelected(result.id); setView('projects'); refresh().catch(() => setError('Project created. The project list could not refresh yet.')); }
  async function importProject(file, capsule) { const result = await importBundle(file, capsule); setImporting(false); setSelected(result.id); setView('projects'); refresh().catch(() => setError('Project imported. The project list could not refresh yet.')); }
  return <div className={`app-shell ${options.largeText ? 'comfortable' : ''} ${options.technical ? 'show-technical' : ''}`}>
    <a className="skip-link" href="#main">Skip to content</a>
    <aside className="sidebar"><div className="brand"><svg viewBox="0 0 42 48" width="36" height="42" aria-hidden="true"><path d="M5 4v25a16 16 0 0 0 32 0V4H27v25a6 6 0 0 1-12 0V4Z" fill="none" stroke="currentColor" strokeWidth="2.4"/></svg><span>unforge</span></div>
      <nav aria-label="Workspace"><button className={view === 'projects' ? 'selected' : ''} disabled={navigationBlocked} onClick={home}><Icon name="folder"/>Your projects</button><button className={view === 'ownership' ? 'selected' : ''} disabled={navigationBlocked} onClick={() => {setSelected(null); setView('ownership');}}><Icon name="book"/>How ownership works</button><button className={view === 'preferences' ? 'selected' : ''} disabled={navigationBlocked} onClick={() => {setSelected(null); setView('preferences');}}><Icon name="preferences"/>Make it comfortable</button></nav>
      <nav aria-label="Workspace usage"><button className={view === 'backups' ? 'selected' : ''} disabled={navigationBlocked} onClick={() => {setSelected(null); setView('backups');}}><Icon name="folder"/>Backups & recovery</button><button className={view === 'allowance' ? 'selected' : ''} disabled={navigationBlocked} onClick={() => {setSelected(null); setView('allowance');}}><Icon name="clock"/>Usage & practice</button></nav>
      <div className="sidebar-foot"><span className="dot"/>On this computer</div>
    </aside>
    <main id="main" className={selected ? 'workspace' : ''}>
      {(selected || view !== 'backups') && <BackupHealth disabled={navigationBlocked || creating || importing} onOpen={() => {if (!navigationBlocked && !creating && !importing) {setSelected(null);setView('backups');}}}/>}
      {projectProblems.length > 0 && <div className="notice" role="alert"><strong>{projectProblems.length} project{projectProblems.length === 1 ? '' : 's'} need attention.</strong><p>Their folders are still here. Other projects remain available.</p><details><summary>Folders to inspect or recover</summary>{projectProblems.map(item=><p key={item.id}><code>{item.path}</code><br/>{item.error}</p>)}</details></div>}
      {error && <div className="error" role="alert">{error} <button className="text-button" onClick={() => {setError(''); refresh().catch(e => setError(e.message));}}>Try again</button></div>}
      {selected ? <Project key={selected} id={selected} back={home} onUpdated={refresh} onNavigationBlocked={setNavigationBlocked} onOpenProject={setSelected}/> : view === 'backups' ? <Backups back={home} onBlocked={setNavigationBlocked}/> : view === 'ownership' ? <Ownership back={home}/> : view === 'preferences' ? <Preferences options={options} update={updateOptions} back={home}/> : view === 'allowance' ? <Allowance projects={projects} back={home} onBlocked={setNavigationBlocked}/> : <Projects projects={projects} loading={loading} openProject={setSelected} openOwnership={() => setView('ownership')} create={() => setCreating(true)} importProject={() => setImporting(true)}/>}
    </main>
    {creating && <CreateProject onCreate={create} onClose={() => setCreating(false)}/>}
    {importing && <ImportProject onImport={importProject} onImported={project=>{setImporting(false);setSelected(project.id);refresh().catch(e=>setError(e.message));}} onClose={() => setImporting(false)}/>}
  </div>;
}
