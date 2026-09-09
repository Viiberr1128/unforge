import { useEffect, useState } from 'react';
import { api } from './api.js';
import { Icon, Modal } from './ui.jsx';
import { useLeaveGuard } from './useLeaveGuard.js';
import { operationKey } from './operationKey.js';

export default function Allowance({ projects, back, onBlocked }) {
  const [data, setData] = useState(null), [limit, setLimit] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [projectId, setProjectId] = useState(projects[0]?.id || ''), [action, setAction] = useState('email');
  const [practiceId, setPracticeId] = useState(''), [receipt, setReceipt] = useState(null);
  const [reconcile, setReconcile] = useState(null);
  const edited = !!data && limit !== String(data.settings.dailyLimit);
  useLeaveGuard(busy || edited || !!reconcile, onBlocked);
  async function load() {const value = await api('/operations');setData(value);setLimit(String(value.settings.dailyLimit));}
  useEffect(() => {load().catch(e => setError(e.message));}, []);
  useEffect(() => {if (!projectId && projects.length) setProjectId(projects[0].id);}, [projectId, projects]);
  async function run(fn, message) {setBusy(true);setError('');setNotice('');let completed = false;try {await fn();completed = true;setNotice(message);setReconcile(null);await load();} catch(e) {setError(completed ? `${message} The view could not refresh: ${e.message}` : e.message);} finally {setBusy(false);}}
  async function practice(repeat) {
    const operationId = repeat ? practiceId : operationKey(`practice-${projectId}`,{action,nonce:crypto.randomUUID()});
    setPracticeId(operationId);
    await run(async () => setReceipt(await api('/operations/practice',{projectId,operationId,action,payload:{example:'Local practice, no external destination'}})),repeat ? 'The same operation ID was checked again.' : 'Practice receipt recorded locally.');
  }
  const projectName = id => projects.find(project => project.id === id)?.name || id;
  const unknownIds = new Set((data?.unknownOperations || []).map(item => item.operationId));
  const history = data ? [...(data.unknownOperations || []), ...data.operations.filter(item => !unknownIds.has(item.operationId))] : [];
  return <section className="allowance">
    <header className="page-top"><button className="text-button" disabled={busy || edited} onClick={back}><Icon name="back"/>Your projects</button><span className="quiet">One shared local allowance</span></header>
    <div className="intro"><h1>Set your own limits.</h1><p>A limit across projects, with a record of each attempt.</p></div>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="notice">{notice}</p>}
    {!data ? <p role="status">Reading your local ledger…</p> : <>
      <div className="usage-tally"><strong>{data.usedToday}<span> / {data.settings.dailyLimit}</span></strong><div>attempts today<p>{data.remainingToday} remaining · resets at midnight UTC</p></div></div>
      <p>Unforge checks this allowance before starting Codex or a practice action. Repeating the same operation ID returns its receipt without consuming another attempt. Three failed, empty, or unknown attempts in a row pause that project.</p>
      <p className="note">This counts attempts, not dollars or tokens. It cannot limit direct provider calls, jobs started in other tools, or a provider’s bill. A single Codex attempt can use varying amounts of your account allowance.</p>
      <form className="limit-form" onSubmit={event => {event.preventDefault();run(() => api('/operations/settings',{dailyLimit:Number(limit),expectedRevision:data.settings.revision}),'Shared daily allowance updated.');}}><label>Maximum attempts per day<input type="number" min="0" max="100000" step="1" required value={limit} onChange={e => setLimit(e.target.value)} disabled={busy}/></label><div className="form-actions"><button type="button" className="secondary" disabled={!edited || busy} onClick={() => setLimit(String(data.settings.dailyLimit))}>Discard edit</button><button disabled={!edited || busy}>Set allowance</button></div></form>
      <p className="note">Set 0 to stop new routed attempts. Work already running continues until it finishes or you stop it.</p>
      {!!data.pausedProjects.length && <section className="care-rows"><h2>Projects that need attention</h2>{data.pausedProjects.map(item => <article key={item.projectId}><h3>{projectName(item.projectId)}</h3><p>{item.consecutiveFailures} unsuccessful outcomes. Check the history and reconcile uncertain results before continuing.</p><button className="secondary" disabled={busy || edited} onClick={() => run(() => api('/operations/resume',{projectId:item.projectId}),'Project resumed. Its history is retained.')}>Resume this project</button></article>)}</section>}
      <section className="practice-work"><h2>A place to try the workflow.</h2><p>Record a pretend email, payment, or webhook. Then replay it and see why the same action should keep the same ID. These built-in examples only create a local receipt.</p><p className="note">This practice area does not sandbox your app, clone a provider account, or intercept its external calls.</p>
        {!projects.length ? <p>Create a project first to try a practice action.</p> : <><div className="form-grid"><label>Practice project<select value={projectId} disabled={busy} onChange={e => {setProjectId(e.target.value);setReceipt(null);setPracticeId('');}}>{projects.map(project => <option value={project.id} key={project.id}>{project.name}</option>)}</select></label><label>Example action<select value={action} disabled={busy} onChange={e => {setAction(e.target.value);setReceipt(null);setPracticeId('');}}><option value="email">Pretend email</option><option value="payment">Pretend payment</option><option value="webhook">Pretend webhook</option></select></label></div><div className="inline-actions"><button disabled={busy || edited || !projectId} onClick={() => practice(false)}>Try a practice action</button><button className="secondary" disabled={busy || edited || !practiceId} onClick={() => practice(true)}>Replay the same action</button></div>{receipt && <div className="notice" role="status"><strong>{receipt.replayed ? 'Existing receipt returned. No new action.' : 'Practice completed.'}</strong><p>{receipt.result?.message}</p><code>{receipt.operationId}</code></div>}</>}
      </section>
      <section className="ledger"><div className="section-heading"><div><h2>What actually happened.</h2><p>Uncertain outcomes first, then the latest {data.historyLimit} receipts. Requests are hashed; the ledger does not store their full payload.</p>{!!data.unknownCount && <p className="notice">{data.unknownCount} uncertain outcome{data.unknownCount === 1 ? '' : 's'} need reconciliation. The oldest 100 are shown; resolving them reveals the next ones.</p>}</div><button className="secondary" disabled={busy || edited} onClick={() => run(async () => {},'Ledger refreshed.')}>Refresh</button></div>
        {!history.length && <p>No routed attempts yet.</p>}<div className="care-rows">{history.map(item => <article key={item.operationId}><div className="section-heading"><div><h3>{projectName(item.projectId)} · {item.kind === 'agent' ? 'Codex' : `Practice ${item.kind}`}</h3><p>{item.state} · {new Date(item.createdAt).toLocaleString()}</p></div>{item.state === 'unknown' && <button className="secondary" disabled={busy || edited} onClick={() => setReconcile({operationId:item.operationId,outcome:'failed',note:''})}>Record what you found</button>}</div><code>{item.operationId}</code>{item.result?.message && <p>{item.result.message}</p>}{item.result?.note && <p>{item.result.note}</p>}</article>)}</div>
      </section>
    </>}
    {reconcile && <Modal title="Resolve an uncertain outcome" busy={busy} onClose={() => setReconcile(null)}><form onSubmit={event => {event.preventDefault();run(() => api('/operations/reconcile',reconcile),'Outcome reconciled. No action was replayed.');}}><p>Check what happened before recording the result. This changes the ledger’s record; it never retries or reverses an action.</p><label>Verified outcome<select value={reconcile.outcome} onChange={e => setReconcile({...reconcile,outcome:e.target.value})}><option value="failed">It did not complete</option><option value="succeeded">It completed</option></select></label><label>What evidence did you find?<textarea autoFocus required maxLength={2000} rows={4} value={reconcile.note} onChange={e => setReconcile({...reconcile,note:e.target.value})}/></label>{error && <p className="error" role="alert">{error}</p>}<div className="form-actions"><button type="button" className="secondary" disabled={busy} onClick={() => setReconcile(null)}>Cancel</button><button disabled={busy}>Record outcome</button></div></form></Modal>}
  </section>;
}
