let token;
export function canPickFolder() { return !!window.webkit?.messageHandlers?.unforgeFolders; }
export function pickFolder() {
  if (!canPickFolder()) return Promise.reject(new Error('Folder selection is available in the Mac app. Paste a folder path in the browser.'));
  const id = crypto.randomUUID();
  return new Promise(resolve => {
    const receive = event => { if (event.detail?.id !== id) return; window.removeEventListener('unforge-folder-picked', receive); resolve(event.detail.path); };
    window.addEventListener('unforge-folder-picked', receive);
    window.webkit.messageHandlers.unforgeFolders.postMessage({action:'chooseFolder',id});
  });
}
export async function importBundle(file, capsule = false) {
  if (!token) token = (await api('/session')).token;
  const response = await fetch(capsule ? '/api/import-capsule' : '/api/import-bundle', {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Unforge-Token': token }, body: file,
  });
  const result = await response.json();
  if (!response.ok) { if (response.status === 403) token = undefined; throw new Error(result.error || 'Could not open this project bundle.'); }
  if (result.backupWarning) window.dispatchEvent(new CustomEvent('unforge-backup-warning',{detail:result.backupWarning}));
  return result;
}
export async function api(path, body) {
  if (body !== undefined && !token) {
    const session = await api('/session');
    token = session.token;
  }
  const response = await fetch(`/api${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json', 'X-Unforge-Token': token },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 403) token = undefined;
    throw new Error(result.error || 'This action could not be completed. Please try again.');
  }
  if (result.backupWarning) window.dispatchEvent(new CustomEvent('unforge-backup-warning',{detail:result.backupWarning}));
  if (body !== undefined && path.startsWith('/backups')) window.dispatchEvent(new Event('unforge-backups-updated'));
  return result;
}
