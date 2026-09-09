import { useState } from 'react';
import { Modal, Icon } from './ui.jsx';

export default function ImportProject({ onImport, onClose }) {
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const capsule = !!file?.name.endsWith('.tar.gz');
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      if (!file || file.size > (capsule ? 256 : 20) * 1024 * 1024) throw new Error('Choose a Git bundle up to 20 MiB or a recovery capsule up to 256 MiB.');
      await onImport(file, capsule);
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  return <Modal title="Bring your project home." busy={busy} onClose={onClose}>
    <p>Open a Git bundle or an Unforge recovery capsule. We reconstruct a new local copy and check its contents.</p>
    <form onSubmit={submit}>
      <label>Project bundle or recovery capsule<input autoFocus type="file" accept=".bundle,.gz" required onChange={e => setFile(e.target.files[0] || null)}/></label>
      <p className="note">Bundles: 20 MiB. Recovery capsules (.tar.gz): 256 MiB. These files can contain private history and declared personal data. Capsules are unencrypted. No project scripts run during import. Submodules and symbolic links are not supported.</p>
      {error && <p role="alert" className="error">{error}</p>}
      <div className="form-actions"><button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button><button disabled={busy || !file}>{busy ? 'Opening your project…' : 'Import local copy'}<Icon name="arrow"/></button></div>
    </form>
  </Modal>;
}
