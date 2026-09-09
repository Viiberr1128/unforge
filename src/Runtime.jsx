import { useEffect, useState } from 'react';
import { api } from './api.js';
import { useLeaveGuard } from './useLeaveGuard.js';

const active = new Set(['starting','preparing','running']);
const preset = profile => ({schemaVersion:1,profile,directory:'.',run:profile === 'node' ? ['npm','run','dev','--','--host','127.0.0.1','--port','{port}'] : profile === 'python' ? ['python3','app.py'] : profile === 'swift' ? ['swift','run'] : [],check:profile === 'node' ? ['npm','test'] : profile === 'python' ? ['python3','-m','unittest'] : profile === 'swift' ? ['swift','test'] : [],prepare:[],healthPath:'/',startupTimeoutSeconds:30,checkTimeoutSeconds:300,runTimeoutSeconds:28800,dataPaths:[],dataEnvironment:{}});

export default function Runtime({id,dirty,onBlocked,onUpdated}) {
  const [state,setState] = useState(null), [config,setConfig] = useState(preset('static'));
  const [configRevision,setConfigRevision] = useState(null);
  const [editing,setEditing] = useState(false), [busy,setBusy] = useState(false), [trusted,setTrusted] = useState(false);
  const [error,setError] = useState(''), [notice,setNotice] = useState('');
  useLeaveGuard(editing || busy,onBlocked);
  useEffect(() => {
    let mounted = true, timer;
    async function refresh() {
      if (document.hidden) return;
      try {const value = await api(`/projects/${id}/runtime`);if (mounted) {setState(value);}}
      catch(e) {if (mounted) setError(e.message);}
    }
    api(`/projects/${id}/runtime`).then(value => {if (mounted) {setState(value);setConfig(value.config || value.suggestedConfig || preset('static'));setConfigRevision(value.revision);}}).catch(e => {if (mounted) setError(e.message);});
    timer = setInterval(refresh,2500);
    document.addEventListener('visibilitychange',refresh);
    return () => {mounted = false;clearInterval(timer);document.removeEventListener('visibilitychange',refresh);};
  },[id]);
  async function run(action,body,message) {
    setBusy(true);setError('');setNotice('');
    try {await api(`/projects/${id}/runtime/${action}`,body);const value = await api(`/projects/${id}/runtime`);setState(value);if (action === 'configure') {setConfig(value.config);setConfigRevision(value.revision);setEditing(false);await onUpdated();}setNotice(message);}
    catch(e) {setError(e.message);} finally {setBusy(false);}
  }
  function update(key,value) {setConfig(current => ({...current,[key]:value}));setEditing(true);}
  if (!state) return <p role={error ? 'alert' : 'status'}>{error || 'Opening local runtime…'}</p>;
  const running = state.runs.some(item => active.has(item.status));
  return <section className="project-care">
    <div className="section-heading"><div><h2>Use your app.</h2><p>Start the saved version in its own folder, run checks, and keep the results here.</p></div></div>
    <p className="notice">This runs trusted code on your Mac. It is not a security sandbox: project code can access files and the network. Previews and checks get disposable data. “Use app” keeps managed data between runs. External calls in the code can still happen.</p>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="notice">{notice}</p>}
    <details open={!state.config}><summary>How this app runs{!state.config ? ' — set up once' : ''}</summary>
      {!state.config&&state.suggestionReason&&<p>{state.suggestionReason}</p>}
      <form className="brief-form" onSubmit={event => {event.preventDefault();run('configure',{document:config,revision:configRevision},'Runtime settings written. Save a version before starting.');}}>
        <label>App type<select disabled={busy} value={config.profile} onChange={event => {setConfig(preset(event.target.value));setEditing(true);}}><option value="static">Static website</option><option value="node">Node web app</option><option value="python">Python app</option><option value="swift">Swift app</option></select></label>
        <label>App folder inside the project<input disabled={busy} value={config.directory} onChange={event => update('directory',event.target.value)}/><span className="field-hint">Use . for the project folder, or a subfolder such as web.</span></label>
        {config.profile !== 'static' && <><p className="note">These starting suggestions need to match your app. An agent can fill them in. Each line below is one command argument; {'{port}'} is replaced with an available local port. The app also receives PORT and UNFORGE_DATA_DIR.</p>{[['run','Start command'],['check','Check command'],['prepare','Preparation command (optional)']].map(([key,label]) => <label key={key}>{label}<textarea rows={key === 'run' ? 6 : 3} disabled={busy} value={config[key].join('\n')} onChange={event => update(key,event.target.value.split('\n').filter(Boolean))}/></label>)}</>}
        <details><summary>Where this app keeps its data</summary><p className="note">Apps using UNFORGE_DATA_DIR work automatically. For other apps, map their data folders or environment variables here. These paths must not contain source files. An agent can identify the correct settings.</p>
        <label>Data folders (one relative path per line)<textarea disabled={busy} rows={3} value={(config.dataPaths || []).join('\n')} onChange={event => update('dataPaths',event.target.value.split('\n').filter(Boolean))}/></label>
        <label>Data environment variables (NAME=relative/path)<textarea disabled={busy} rows={3} value={Object.entries(config.dataEnvironment || {}).map(([key,value]) => `${key}=${value}`).join('\n')} onChange={event => update('dataEnvironment',Object.fromEntries(event.target.value.split('\n').filter(Boolean).map(line => {const at=line.indexOf('=');return at<0?[line,'']:[line.slice(0,at),line.slice(at+1)];})))}/></label></details>
        <label>Local health path<input disabled={busy} value={config.healthPath} onChange={event => update('healthPath',event.target.value)}/></label>
        <p className="note">No tools or packages are installed automatically. A preparation command can install packages and execute their scripts; review it before running. Native apps may not expose an HTTP health endpoint.</p>
        <div className="form-actions">{editing && <button type="button" className="secondary" disabled={busy} onClick={() => {setConfig(state.config || preset('static'));setConfigRevision(state.revision);setEditing(false);}}>Discard settings edits</button>}<button disabled={busy || (!editing && !!state.config)}>Write runtime settings</button></div>
      </form>
    </details>
    {state.config && <>
      {Object.entries(state.availableTools).some(([,available]) => !available) && <p className="error">Tools missing on this Mac: {Object.entries(state.availableTools).filter(([,available]) => !available).map(([name]) => name).join(', ')}.</p>}
      {dirty && <p className="notice">Save a version first. Runs use saved files, not unsaved changes.</p>}
      <label className="check-label"><input type="checkbox" checked={trusted} disabled={busy} onChange={event => setTrusted(event.target.checked)}/>I trust this project's code and reviewed its external calls and preparation command.</label>
      <div className="form-actions"><button disabled={busy || editing || dirty || !trusted || running} onClick={() => run('start',{trusted:true,persistent:true},'Starting your app with its saved local data.')}>Use app</button><button className="secondary" disabled={busy || editing || dirty || !trusted || running} onClick={() => run('start',{trusted:true,persistent:false},'Starting a preview with disposable data.')}>Try a preview</button><button className="secondary" disabled={busy || editing || dirty || !trusted || running} onClick={() => run('check',{trusted:true},'Check started. Its actual result appears below.')}>Run checks</button>{running && <button className="secondary" disabled={busy} onClick={() => run('stop',{},'Stop requested. Waiting for the processes to exit.')}>Stop active run</button>}</div>
    </>}
    <h3>Recent attempts</h3>{!state.runs.length && <p>No runs yet. Results will include the saved version, process output and health observation.</p>}
    <div className="care-rows">{state.runs.map(item => <article key={item.id}><div className="section-heading"><div><h3>{item.kind === 'check' ? 'Check' : item.persistent ? 'Your app' : 'Preview'} · {item.status}</h3><p>{new Date(item.createdAt).toLocaleString()} · version {item.sourceHead?.slice(0,8)}</p></div>{item.url && item.status === 'running' && <a className="button secondary" href={item.url} target="_blank" rel="noreferrer">Open running copy</a>}</div>{item.error && <p className="error">{item.error}</p>}<p className="note">Health: {item.health || 'not checked'}. A responding page does not prove the app's behavior is correct.</p>{item.exitCode != null && <p>Process exit code: {item.exitCode}</p>}<details><summary>Output and working folder</summary><code className="command">{item.candidatePath}</code><p className="note">Data: {item.dataPath}. {item.persistent ? 'Managed app data is kept across runs and included in workspace backups.' : 'Disposable data belongs to this attempt.'}</p><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',maxHeight:300,overflow:'auto'}}>{item.output || 'No output captured yet.'}</pre>{item.outputTruncated && <p className="note">Earlier output was truncated.</p>}</details>{!active.has(item.status) && <button className="text-button" disabled={busy} onClick={() => run('remove',{runId:item.id},'Attempt removed. Persistent app data has been kept.')}>Remove attempt and disposable files</button>}</article>)}</div>
  </section>;
}
