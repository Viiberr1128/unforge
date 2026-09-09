import { useCallback, useEffect, useState } from 'react';
import { api, canPickFolder, pickFolder } from './api.js';

const jobLabels = {running:'Making a recovery copy',completed:'Recovery copy created',failed:'Copy needs attention',interrupted:'Copy interrupted'};
const fieldsetStyle = {border:0,padding:0,margin:0,minWidth:0};
function when(value) {
  if (!value) return 'Not recorded';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? 'Time unavailable' : date.toLocaleString();
}
function storageSize(value) {
  if (!Number.isFinite(value) || value < 0) return 'Not recorded';
  if (value < 1024) return `${value} B`;
  if (value < 1024**2) return `${(value/1024).toFixed(1)} KB`;
  if (value < 1024**3) return `${(value/1024**2).toFixed(1)} MB`;
  return `${(value/1024**3).toFixed(2)} GB`;
}
function destinationLabel(destination) {
  if (destination.uploadEvidence?.error) return 'Latest upload check needs attention';
  if (destination.cloudRecoveryEvidence?.contentVerified === true) return 'Recovery from iCloud tested on this Mac';
  if (destination.state === 'upload_pending') return 'Copied on this Mac · cloud upload not confirmed';
  if (destination.state === 'cloud_uploaded') return 'macOS reports uploaded · recovery from the cloud not tested';
  if (destination.state === 'cloud_verified') return 'Cloud verification recorded · review its evidence';
  if (destination.state === 'copied_locally') return 'Local copy checked';
  return 'Copy status needs checking';
}

export default function Backups({back,onBlocked}) {
  const [state,setState] = useState(null), [error,setError] = useState(''), [notice,setNotice] = useState('');
  const [path,setPath] = useState(''), [kind,setKind] = useState('folder');
  const [password,setPassword] = useState(''), [confirmation,setConfirmation] = useState('');
  const [keyKeptElsewhere,setKeyKeptElsewhere] = useState(false), [showPassword,setShowPassword] = useState(false);
  const [restorePath,setRestorePath] = useState(''), [destination,setDestination] = useState(''), [restoreKey,setRestoreKey] = useState('');
  const [cloudRehearsal,setCloudRehearsal] = useState(null);
  const [recovered,setRecovered] = useState(null), [busy,setBusy] = useState(false);
  const [visiblePoints,setVisiblePoints] = useState(3);
  const needsNewKey = !!state && !state.configured && !state.keyConfigured;
  const hasPassphrase = !!(password || confirmation || restoreKey);
  const load = useCallback(async () => { const value = await api('/backups'); setState(value); return value; }, []);
  useEffect(() => { load().catch(e => setError(e.message)); }, [load]);
  useEffect(() => { onBlocked(busy || hasPassphrase); return () => onBlocked(false); }, [busy,hasPassphrase,onBlocked]);
  useEffect(() => {
    const warn = event => { if (busy || hasPassphrase) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload',warn);
    return () => window.removeEventListener('beforeunload',warn);
  }, [busy,hasPassphrase]);
  const running = !!state?.jobs?.some(job => job.state === 'running');
  const followProgress = running || !!state?.schedule?.enabled || state?.watcher?.status === 'starting';
  useEffect(() => {
    const refresh = () => { if (!document.hidden) load().catch(e => setError(e.message)); };
    const timer = followProgress ? setInterval(refresh,5000) : null;
    document.addEventListener('visibilitychange',refresh);
    return () => { if (timer) clearInterval(timer); document.removeEventListener('visibilitychange',refresh); };
  }, [followProgress,load]);
  async function act(fn,message) {
    setBusy(true); setError(''); setNotice('');
    let completed = false;
    try { const result = await fn(); completed = true; setNotice(typeof message === 'function' ? message(result) : message); await load(); }
    catch(e) { setError(completed ? `The action completed, but its status could not refresh: ${e.message}` : e.message); }
    finally { setBusy(false); }
  }
  function clearSetupKey() { setPassword(''); setConfirmation(''); setKeyKeptElsewhere(false); setShowPassword(false); }
  function chooseRecoveryPoint(job, point, cloud = false) {
    setRestorePath(point.backupPath); setRestoreKey(''); setRecovered(null);
    setCloudRehearsal(cloud ? {jobId:job.id,path:point.backupPath,vaultPath:point.vaultPath} : null);
    document.getElementById('recover-workspace')?.scrollIntoView({behavior:'smooth'});
  }
  function changeRecoveryPath(value) { setRestorePath(value); setCloudRehearsal(null); }
  async function recover(event) {
    event.preventDefault();
    await act(async () => {
      try {
        const result = await api(cloudRehearsal ? '/backups/cloud-rehearse' : '/backups/restore', {
          path:restorePath,password:restoreKey,destination,...(cloudRehearsal ? {jobId:cloudRehearsal.jobId} : {}),
        });
        setRecovered(result); return result;
      } finally { setRestoreKey(''); }
    }, result => result.cloudRecoveryEvidence?.contentVerified
      ? `Downloaded from iCloud and recovered ${result.files} files to ${result.path}. Recovery on a second device has not been tested.`
      : `Recovered ${result.files} files to ${result.path}. Applications have not been started.`);
  }
  async function chooseFolder(setter, recoveryParent = false) {
    try {
      const value = await pickFolder();
      if (value) setter(recoveryParent ? `${value.replace(/\/$/,'')}/Unforge Recovered ${new Date().toISOString().slice(0,19).replace(/[T:]/g,'-')}` : value);
    } catch(e) { setError(e.message); }
  }
  async function saveDestination(event) {
    event.preventDefault();
    if (needsNewKey && password !== confirmation) { setError('The two passphrases do not match. Re-enter them before saving.'); return; }
    if (needsNewKey && !keyKeptElsewhere) { setError('Keep your recovery passphrase somewhere you can reach without this Mac first.'); return; }
    await act(async () => {
      const result = await api('/backups/settings', {
        destinations:[...(state.destinations || []).filter(d => d.path !== path),{kind,path,name:kind === 'icloud' ? 'iCloud Drive' : kind === 'drive' ? 'Google Drive' : 'Backup folder'}],
        ...(needsNewKey ? {password} : {}),
      });
      clearSetupKey(); setPath('');
      return result;
    }, 'Destination saved. Choose Back up now to create your first recovery copy.');
  }
  if (!state) return <p role={error ? 'alert' : 'status'}>{error || 'Opening your backups…'}</p>;
  const schedule = state.schedule || {};
  const watcher = state.watcher || {};
  const destinations = state.destinations || [];
  const jobs = state.jobs || [];
  const latest = jobs.find(job => job.state === 'completed');
  const scheduleError = schedule.error || schedule.persistenceError || schedule.lastError;
  const interval = schedule.intervalSeconds || 300;
  const observing = watcher.observing === true && watcher.status === 'watching';
  return <>
    <button className="text-button" disabled={busy || hasPassphrase} onClick={back}>← Your projects</button>
    <header className="project-heading"><div><h1>Keep your work.</h1><p>A recovery copy you can open even if this Mac is gone.</p></div><button disabled={busy || running || !state.configured || !state.available} onClick={() => act(() => api('/backups/start',{}),'Backup started. You can continue using Unforge while it makes and checks the copy.')}>Back up now</button></header>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="notice">{notice}</p>}
    {state.settingsRecoveryRequired && <p role="alert" className="error">{state.settingsError} Save a destination below to repair backup settings.{state.keyConfigured ? ' Your existing local recovery key will be preserved.' : ''}</p>}
    {state.historyErrors?.length > 0 && <div role="alert" className="error"><p>Some recovery history needs attention. Available recovery points are still listed below.</p>{state.historyErrors.map((message,index) => <p key={index}>{message}</p>)}</div>}
    {hasPassphrase && <p className="note">Finish this step or clear the entered passphrase before leaving this page.</p>}
    {!state.available && <p role="alert" className="error">This installation is missing its backup tool. Install a complete Unforge Mac app. Your projects are still available.</p>}
    {latest && <p className="notice">Last checked recovery copy: <strong>{when(latest.finishedAt || latest.createdAt)}</strong>. Check its destination below to see whether cloud upload has been confirmed.</p>}

    <section className="request" aria-labelledby="backup-setup-heading">
      <h2 id="backup-setup-heading">{state.configured ? 'Where your copies go' : 'Set up your first recovery copy'}</h2>
      <p>Your projects stay where they are. Unforge makes an encrypted copy of their history, written changes, managed local data, saved drafts and proposals.</p>
      {destinations.map(d => <p key={d.path}><strong>{d.name}</strong><br/><code>{d.path}</code></p>)}
      <form onSubmit={saveDestination}>
        <fieldset disabled={busy || running} style={fieldsetStyle}>
          <label>Save a copy to<select value={kind} onChange={e => setKind(e.target.value)}><option value="folder">External drive or another folder</option><option value="icloud">iCloud Drive</option><option value="drive">Google Drive synced folder</option></select></label>
          {state.suggestions?.length > 0 && <div className="inline-actions">{state.suggestions.map(d => <button className="secondary" type="button" key={d.path} onClick={() => { setPath(d.path); setKind(d.kind); }}>Use {d.name}</button>)}</div>}
          <label>Backup folder<input required value={path} onChange={e => setPath(e.target.value)} placeholder="Choose a folder outside your active projects"/></label>
          {canPickFolder() && <button type="button" className="secondary" onClick={() => chooseFolder(setPath)}>Choose backup folder…</button>}
          <p className="note">{kind === 'folder' ? 'A folder on this same Mac cannot protect against losing the Mac. Use a separate drive for that protection.' : 'Choose a folder your cloud app actually syncs. Unforge does not sign in to the cloud or buy more storage; copies may wait for an upload.'}</p>
          {needsNewKey && <>
            <h3>Your way back in</h3>
            <p>You will need this passphrase to recover on another computer. Keep it in a password manager you can access from another device, or on paper stored separately.</p>
            <label>Create a recovery passphrase<input type={showPassword ? 'text' : 'password'} autoComplete="new-password" minLength={12} required value={password} onChange={e => setPassword(e.target.value)} placeholder="At least 12 characters"/></label>
            <label>Enter the passphrase again<input type={showPassword ? 'text' : 'password'} autoComplete="new-password" minLength={12} required value={confirmation} onChange={e => setConfirmation(e.target.value)}/></label>
            <button type="button" className="text-button" aria-pressed={showPassword} onClick={() => setShowPassword(value => !value)}>{showPassword ? 'Hide passphrase' : 'Show passphrase'}</button>
            <label className="check-label"><input type="checkbox" checked={keyKeptElsewhere} onChange={e => setKeyKeptElsewhere(e.target.checked)} required/>I can retrieve this passphrase without this Mac.</label>
            <p className="note">This Mac keeps a local key for automatic backups. That does not replace your separate copy. The key is excluded from recovery copies; do not put a plain-text key beside the encrypted backup.</p>
          </>}
          <div className="inline-actions"><button disabled={!path || !state.available || (needsNewKey && (!keyKeptElsewhere || !password || !confirmation))}>{state.settingsRecoveryRequired ? 'Repair backup settings' : state.configured ? 'Add destination' : 'Save backup setup'}</button>{(password || confirmation) && <button className="text-button" type="button" onClick={clearSetupKey}>Clear entered passphrase</button>}</div>
        </fieldset>
      </form>
      {state.configured && <details><summary>Could I recover after losing this Mac?</summary><p>You need both the complete recovery folder and your passphrase on another device. Access to a cloud account may also require its password and a second sign-in step. Keep that access independent of this Mac, then try recovering a separate copy below.</p><p className="note">Unforge cannot verify where you kept your passphrase. A successful local recovery does not prove that your cloud copy is available from another device.</p></details>}
    </section>

    {(state.configured || schedule.recoveryRequired) && <section className="request" aria-labelledby="automatic-backups-heading">
      <h2 id="automatic-backups-heading">Keep backing up as I work</h2>
      {schedule.recoveryRequired ? <>
        <p role="alert" className="error">{scheduleError || 'Automatic backup settings need repair. Automation is disabled and your existing recovery copies remain available.'}</p>
        <p>Repair preserves the damaged settings in a separate file. Pending work stays pending until a new recovery copy completes.</p>
        <div className="inline-actions"><button disabled={busy || !state.configured || !state.available} onClick={() => act(() => api('/backups/automatic',{enabled:true,intervalSeconds:interval}),'Settings repaired. Automatic backups are enabled; a new recovery copy is due.')}>Repair and enable automatic backups</button><button className="secondary" disabled={busy} onClick={() => act(() => api('/backups/automatic',{enabled:false,intervalSeconds:interval}),'Settings repaired. Automatic backups remain off; you can still choose Back up now.')}>Repair with automatic backups off</button></div>
      </> : <>
        <label className="check-label"><input type="checkbox" checked={!!schedule.enabled} disabled={busy || !state.configured || !state.available} onChange={e => act(() => api('/backups/automatic',{enabled:e.target.checked,intervalSeconds:interval}),'Automatic backup preference saved.')}/>Back up changes automatically while Unforge is open</label>
        <label>Time between recovery points<select value={interval} disabled={busy || !state.configured || !state.available} onChange={e => act(() => api('/backups/automatic',{enabled:!!schedule.enabled,intervalSeconds:Number(e.target.value)}),'Backup timing saved.')}>
          {[...new Set([300,900,1800,3600,interval])].sort((a,b)=>a-b).map(seconds => <option key={seconds} value={seconds}>{seconds / 60} minutes</option>)}
        </select></label>
        <p className="note">Shorter intervals protect recent changes sooner. Longer intervals use less backup history and verification work. Unchanged contents are reused; history still grows. Existing recovery points are never automatically deleted.</p>
        <p className="note">Changes are grouped into recovery points, normally no more than once every {Math.round(interval / 60)} minute{interval !== 60 ? 's' : ''}. Unforge must stay open. Continuous editing will not postpone every backup.</p>
        {scheduleError && <p role="alert" className="error">{scheduleError}</p>}
        {schedule.blockedReason && <p role="status">{schedule.blockedReason}</p>}
        {schedule.runningJobId ? <p role="status">An automatic recovery copy is being made. New changes stay queued for the following copy.</p> : schedule.pending ? <p role="status">{schedule.enabled ? `Changes are waiting for the next recovery copy${schedule.nextDueAt ? `, due no earlier than ${when(schedule.nextDueAt)}` : ''}.` : 'Automatic backups are off. Use Back up now whenever you want a recovery copy.'}</p> : <p>No reported changes are waiting for an automatic copy. This is not confirmation of cloud upload.</p>}
      </>}
      <h3>Changes from other apps</h3>
      <p>{observing ? 'This Mac is currently watching the managed workspace for changes from editors and running apps.' : watcher.status === 'starting' ? 'The workspace watcher is starting. External changes are not confirmed as observed yet.' : 'Workspace watching is not active. Changes reported through Unforge can still trigger backups; changes made elsewhere may need Back up now.'}</p>
      {watcher.error && <p role="alert" className="error">{watcher.error}</p>}
      <p className="note">{watcher.scope || 'Watching covers the managed workspace. Original project folders and hosted databases need their own backup arrangements.'}</p>
      <button className="text-button" disabled={busy} onClick={() => act(load,'Backup and watcher status refreshed.')}>Refresh backup status</button>
    </section>}

    <section aria-labelledby="recovery-points-heading"><h2 id="recovery-points-heading">Recovery points</h2>
      {!jobs.length && <p>No recovery points yet. Save your setup, then choose Back up now. After it finishes, try recovering a separate copy.</p>}
      {jobs.slice(0,visiblePoints).map((job,index) => {
        const content = <article className={index === 0 ? 'request' : undefined} key={job.id}>
        <h3>{jobLabels[job.state] || 'Recovery status needs checking'}</h3><p>{when(job.createdAt)}{job.files ? ` · ${job.files} files` : ''}<br/>{job.phase}</p>
        {job.error && <p role="alert" className="error">{job.error}</p>}
        {job.destinations?.map(d => <div key={d.path}>
          <p><strong>{d.name}: {destinationLabel(d)}</strong><br/><code>{d.backupPath}</code></p>
          {d.format === 'incremental-v2' && <><p className="note">{storageSize(d.addedBytes)} added · {storageSize(d.reusedBytes)} reused. Keep the entire shared vault; this point cannot restore on its own.</p><details><summary>Shared vault location</summary><p><code>{d.vaultPath}</code></p><p>Do not delete shared files to remove an old recovery point.</p></details></>}
          {d.checkedAt && <p className="note">Last upload check: {when(d.checkedAt)}</p>}
          {d.uploadEvidence && <details><summary>What this upload check established</summary><p>{typeof d.uploadEvidence === 'string' ? d.uploadEvidence : d.uploadEvidence.error || d.uploadEvidence.evidence || 'Read-only operating-system metadata; no independent cloud recovery was performed.'}</p>{typeof d.uploadEvidence === 'object' && Number.isInteger(d.uploadEvidence.files) && <p className="note">{d.uploadEvidence.uploaded ?? 0} of {d.uploadEvidence.files} inspected files{d.uploadEvidence.scope === 'entire shared vault' ? ' across the entire shared vault' : ''} reported uploaded. These flags do not prove the backup can be retrieved from another computer.</p>}</details>}
          {d.cloudRecoveryEvidence && <p className={d.cloudRecoveryEvidence.state === 'failed' ? 'error' : 'note'} role={d.cloudRecoveryEvidence.state === 'failed' ? 'alert' : undefined}>{d.cloudRecoveryEvidence.state === 'verified' ? `Cloud download and file recovery verified on this Mac at ${when(d.cloudRecoveryEvidence.finishedAt)}. Recovery on a second device has not been tested.` : d.cloudRecoveryEvidence.error || 'A cloud recovery check has not finished. No complete recovery proof is recorded.'}</p>}
          <button className="text-button" disabled={busy} onClick={() => chooseRecoveryPoint(job,d)}>Try recovering this copy</button>
          {job.state === 'completed' && d.kind === 'icloud' && <button className="secondary" disabled={busy || running || !state.cloudRehearsalAvailable} onClick={() => chooseRecoveryPoint(job,d,true)}>Test recovery from iCloud</button>}
        </div>)}
        {job.state === 'completed' && job.destinations?.some(d => d.kind === 'icloud') && <button className="secondary" disabled={busy} onClick={() => act(() => api('/backups/cloud-status',{jobId:job.id}),'iCloud upload metadata checked. Read the result below; this did not test recovery from another device.')}>Check iCloud upload</button>}
        {job.state === 'completed' && job.destinations?.some(d => d.kind === 'icloud') && !state.cloudRehearsalAvailable && <p className="note">Testing a fresh iCloud download requires the iCloud recovery helper in the current Unforge Mac app.</p>}
      </article>;
        return index === 0 ? content : <details className="request" key={job.id}><summary>{when(job.createdAt)} · {jobLabels[job.state] || 'Recovery status needs checking'}</summary>{content}</details>;
      })}
      {jobs.length > visiblePoints && <button type="button" className="text-button" disabled={busy} onClick={() => setVisiblePoints(count => count+10)}>Show older points ({Math.min(10,jobs.length-visiblePoints)} more)</button>}
    </section>

    <section className="request" id="recover-workspace" aria-labelledby="recover-workspace-heading">
      <h2 id="recover-workspace-heading">{cloudRehearsal ? 'Test recovery from iCloud' : 'Try recovering a separate copy'}</h2>
      <p>Use this to check your passphrase now, or to recover on a replacement Mac. Unforge verifies and restores into a new folder. Your existing workspace stays intact.</p>
      {cloudRehearsal && <p className="notice">{cloudRehearsal.vaultPath ? 'This test removes the local iCloud cache for the entire shared vault, including files used by its other recovery points. It downloads the whole vault again, verifies every encrypted file, then restores the selected point.' : 'This test removes only this encrypted recovery point’s local iCloud cache, downloads it again, then verifies every restored file.'} Your passphrase is checked before removing the cache. Your projects and the cloud copy stay in place. Keep Unforge open and connected; downloading may take up to two minutes before recovery begins. This tests cloud recovery on this Mac, not access from a second device.</p>}
      <form onSubmit={recover}>
        <fieldset disabled={busy || running} style={fieldsetStyle}>
          <label>Recovery point folder<input required readOnly={!!cloudRehearsal} value={restorePath} onChange={e => changeRecoveryPath(e.target.value)} placeholder="Choose a .ufpoint inside its vault, or an older .ufbackup"/></label>
          {canPickFolder() && !cloudRehearsal && <button type="button" className="secondary" onClick={() => chooseFolder(changeRecoveryPath)}>Choose recovery point…</button>}
          <label>Recovery passphrase<input required type="password" autoComplete="current-password" value={restoreKey} onChange={e => setRestoreKey(e.target.value)}/></label>
          <label>New folder for the recovered workspace<input required value={destination} onChange={e => setDestination(e.target.value)} placeholder="This folder must not already exist"/></label>
          {canPickFolder() && <button type="button" className="secondary" onClick={() => chooseFolder(setDestination,true)}>Choose where to put the new folder…</button>}
          <div className="inline-actions"><button disabled={!state.available}>{busy ? 'Checking recovery…' : cloudRehearsal ? 'Download and test recovery' : 'Recover a separate copy'}</button>{cloudRehearsal && <button className="text-button" type="button" onClick={() => setCloudRehearsal(null)}>Use ordinary recovery instead</button>}{restoreKey && <button className="text-button" type="button" onClick={() => setRestoreKey('')}>Clear entered passphrase</button>}</div>
        </fieldset>
      </form>
      {recovered && <div className="notice" role="status"><strong>Your recovered workspace is ready to inspect.</strong><p><code>{recovered.path}</code></p><p>In the Mac app, choose <strong>File → Open Workspace…</strong> and select this folder. Then open an app and check the records that matter to you. Applications and scheduled work do not start automatically.</p><p className="note">{recovered.cloudRecoveryEvidence?.contentVerified ? 'This proves cloud download and file reconstruction on this Mac. Recovery on a second device, every application workflow, and external services have not been verified.' : 'This proves local reconstruction with your passphrase. It does not prove cloud download, every application workflow, or recovery of external services.'}</p></div>}
    </section>
    <details><summary>What these recovery copies include</summary><p>{state.scope}</p><p>{state.cloudNotice}</p></details>
  </>;
}
