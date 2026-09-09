const pending = new Map();
export function operationKey(scope, payload) {
  const serialized = JSON.stringify(payload);
  let prior = pending.get(scope);
  try { prior ||= JSON.parse(sessionStorage.getItem(`unforge-pending-${scope}`) || 'null'); } catch {}
  if (!prior || prior.payload !== serialized) prior = {payload:serialized,id:crypto.randomUUID()};
  pending.set(scope, prior);
  try {sessionStorage.setItem(`unforge-pending-${scope}`, JSON.stringify(prior));} catch {}
  return prior.id;
}
export function finishOperation(scope) {
  pending.delete(scope);
  try {sessionStorage.removeItem(`unforge-pending-${scope}`);} catch {}
}
