import { useState } from 'react';
import { api, canPickFolder, pickFolder } from './api.js';
import { Modal, Icon } from './ui.jsx';

export default function ImportProject({ onImport, onImported, onClose }) {
  const [mode, setMode] = useState('folder');
  const [file, setFile] = useState(null);
  const [path, setPath] = useState(''), [name, setName] = useState('');
  const [inventory, setInventory] = useState(null), [accepted, setAccepted] = useState(false);
  const [shown, setShown] = useState(50);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const capsule = !!file?.name.endsWith('.tar.gz');
  async function inspect(event) {
    event.preventDefault(); setBusy(true); setError(''); setInventory(null); setAccepted(false);
    try {setInventory(await api('/folders/inventory', {path: path.trim()}));setShown(50);} catch (e) {setError(e.message);} finally {setBusy(false);}
  }
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      if (mode === 'folder') {
        const result = await api('/folders/import', {path:inventory.sourcePath,name:name.trim() || undefined,revision:inventory.revision,allowPartial:accepted});
        await onImported(result.project);
      } else {
        if (!file || file.size > (capsule ? 256 : 20) * 1024 * 1024) throw new Error('Choose a Git bundle up to 20 MiB or a recovery capsule up to 256 MiB.');
        await onImport(file, capsule);
      }
    } catch (e) {setError(e.message);} finally {setBusy(false);}
  }
  return <Modal title="Bring your project home." busy={busy} onClose={onClose}>
    <nav className="subtabs" aria-label="Import method">{[['folder','A folder on this Mac'],['bundle','A saved bundle']].map(([value,label]) => <button type="button" key={value} className={mode === value ? 'selected' : 'secondary'} onClick={() => {setMode(value);setError('');}}>{label}</button>)}</nav>
    {mode === 'folder' ? <>
      <p>Make an independent working copy of an existing app. First, see exactly what will come with it.</p>
      {canPickFolder()&&<button type="button" className="secondary" disabled={busy} onClick={()=>pickFolder().then(value=>{if(value){setPath(value);setInventory(null);setAccepted(false);}}).catch(e=>setError(e.message))}>Choose project folder…</button>}
      <form onSubmit={inspect}><label>Folder location<input autoFocus required value={path} onChange={e => {setPath(e.target.value);setInventory(null);setAccepted(false);}} placeholder="/Users/you/projects/my-app"/></label><p className="note">In Finder, select your app folder and press Option–Command–C to copy its location, then paste it here.</p><button className="secondary" disabled={busy || !path.trim()}>{busy ? 'Checking folder…' : 'Review this folder'}</button></form>
      {inventory && <form onSubmit={submit} style={{marginTop:24}}>
        <h3>{inventory.fileCount.toLocaleString()} source files · {(inventory.totalBytes / 1048576).toFixed(1)} MiB</h3>
        <p>{inventory.git.history === 'preserved' ? 'Supported Git branches, tags and history will be kept. Your current files become a new version in the copy.' : inventory.git.history === 'none' ? 'This folder has no Git history. Its current source becomes the first saved version.' : `History cannot be copied: ${inventory.git.reason}. The listed current files can still be imported.`}</p>
        <label>Name in Unforge<input value={name} maxLength={120} placeholder={inventory.sourcePath.split('/').pop()} onChange={e => setName(e.target.value)}/></label>
        <details><summary>Files coming with you ({inventory.fileCount})</summary><ul style={{maxHeight:220,overflow:'auto',overflowWrap:'anywhere'}}>{inventory.files.slice(0,shown).map(item => <li key={item.path}>{item.path}</li>)}</ul>{shown < inventory.fileCount && <button type="button" className="text-button" onClick={() => setShown(value => value + 100)}>Show 100 more</button>}</details>
        {!!inventory.skipped.length && <details open={inventory.partial}><summary>Items left out ({inventory.skipped.length})</summary><ul style={{maxHeight:220,overflow:'auto',overflowWrap:'anywhere'}}>{inventory.skipped.map(item => <li key={item.path}><strong>{item.path}</strong> — {item.reason}</li>)}</ul></details>}
        {inventory.git.note && <p className="note">{inventory.git.note}</p>}
        <p className="notice">Your original stays where it is. Changes in the original and this copy do not sync automatically. App data and credentials need separate setup.</p>
        <p className="note">Filename checks are not a secret scan. Private values can remain inside ordinary source files and existing history. Nothing is uploaded or executed during import.</p>
        {inventory.partial && <label className="check-label"><input type="checkbox" checked={accepted} onChange={e => setAccepted(e.target.checked)}/>I reviewed the omitted items and want this partial source copy.</label>}
        <div className="form-actions"><button type="button" className="secondary" onClick={onClose}>Cancel</button><button disabled={busy || (inventory.partial && !accepted)}>{busy ? 'Making your copy…' : 'Import independent copy'}<Icon name="arrow"/></button></div>
      </form>}
    </> : <>
      <p>Open a Git bundle or an Unforge recovery capsule. We reconstruct a new local copy and check its contents.</p>
      <form onSubmit={submit}>
        <label>Project bundle or recovery capsule<input type="file" accept=".bundle,.gz" required onChange={e => setFile(e.target.files[0] || null)}/></label>
        <p className="note">Bundles: 20 MiB. Recovery capsules (.tar.gz): 256 MiB. These files can contain private history and declared personal data. Capsules are unencrypted. No project scripts run during import. Submodules and symbolic links are not supported.</p>
        <div className="form-actions"><button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button><button disabled={busy || !file}>{busy ? 'Opening your project…' : 'Import local copy'}<Icon name="arrow"/></button></div>
      </form>
    </>}
    {error && <p role="alert" className="error">{error}</p>}
  </Modal>;
}
