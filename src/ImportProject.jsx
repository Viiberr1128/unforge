import { useState } from 'react';
import { Modal, Icon } from './ui.jsx';

export default function ImportProject({ onImport, onClose }) {
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      if (!file || file.size > 20 * 1024 * 1024) throw new Error('Choose a Git bundle smaller than 20 MiB.');
      await onImport(file);
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  return <Modal title="Bring your project home." busy={busy} onClose={onClose}>
    <p>Open a Git bundle from another Unforge workspace or ordinary Git. We make a new local copy. Your original stays where it is.</p>
    <form onSubmit={submit}>
      <label>Project bundle<input autoFocus type="file" accept=".bundle" required onChange={e => setFile(e.target.files[0] || null)}/></label>
      <p className="note">Up to 20 MiB. Bundles carry committed history, which may include past private information. No project scripts are run during import. Submodules, symbolic links, and some reserved paths are not supported in this alpha.</p>
      {error && <p role="alert" className="error">{error}</p>}
      <div className="form-actions"><button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button><button disabled={busy || !file}>{busy ? 'Opening your project…' : 'Import local copy'}<Icon name="arrow"/></button></div>
    </form>
  </Modal>;
}
