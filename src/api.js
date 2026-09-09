let token;
export async function importBundle(file) {
  if (!token) token = (await api('/session')).token;
  const response = await fetch('/api/import-bundle', {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Unforge-Token': token }, body: file,
  });
  const result = await response.json();
  if (!response.ok) { if (response.status === 403) token = undefined; throw new Error(result.error || 'Could not open this project bundle.'); }
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
  return result;
}
