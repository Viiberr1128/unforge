import { useEffect, useState } from 'react';
import { api } from './api.js';
import { Modal, Icon } from './ui.jsx';
import { agentExplanation } from './agentOutput.js';
import { useLeaveGuard } from './useLeaveGuard.js';
import { operationKey, finishOperation } from './operationKey.js';
import Consequences from './Consequences.jsx';

const labels = {running:'Codex is working in a separate copy',completed:'A proposal is ready to review',failed:'This attempt needs attention',cancelled:'Work stopped',timed_out:'The time limit was reached',applied:'Proposal brought into your project',archived:'Receipt from an earlier session',unknown:'This attempt has an uncertain outcome'};

export default function AgentWork({ id, dirty, onApplied, initialRequest, onBlocked }) {
  const [status, setStatus] = useState(null);
  const [job, setJob] = useState(null);
  const [prompt, setPrompt] = useState('');
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [reviewApply, setReviewApply] = useState(false);
  const [impact, setImpact] = useState(null);
  useLeaveGuard(busy || open || reviewApply, onBlocked);
  useEffect(() => {if (initialRequest) {setPrompt(initialRequest.text);setOpen(true);}}, [initialRequest?.id]);
  useEffect(() => {
    setImpact(null);
    if (job?.status !== 'completed') return;
    let active = true;
    api(`/agent/jobs/${job.id}/consequences`).then(value => {if (active) setImpact(value);}).catch(e => {if (active) setError(`Could not inspect proposal consequences: ${e.message}`);});
    return () => {active = false;};
  }, [job?.id, job?.status]);
  useEffect(() => {
    let active = true;
    api('/agent/status').then(async value => {
      if (!active) return;
      setStatus(value);
      let saved = value.recentJobs?.find(item => item.projectId === id)?.id;
      if (!saved) { try {saved = sessionStorage.getItem(`unforge-job-${id}`);} catch {} }
      if (saved) {
        try { const result = await api(`/agent/jobs/${saved}`); if (active && result.projectId === id) setJob(result); }
        catch { try {sessionStorage.removeItem(`unforge-job-${id}`);} catch {} }
      }
    }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [id]);
  useEffect(() => {
    if (!job || job.status !== 'running') return;
    let active = true, timer;
    async function update() {
      if (!active || document.hidden) return;
      try { const value = await api(`/agent/jobs/${job.id}`); if (active) {setJob(value); setError(''); if (value.status === 'running') timer = setTimeout(update, 1800);} }
      catch(e) { if (active) {setError(e.message); timer = setTimeout(update, 5000);} }
    }
    const visibility = () => { clearTimeout(timer); if (!document.hidden) update(); };
    timer = setTimeout(update, 1800);
    document.addEventListener('visibilitychange', visibility);
    return () => {active = false; clearTimeout(timer); document.removeEventListener('visibilitychange', visibility);};
  }, [job?.id, job?.status]);
  async function start(event) {
    event.preventDefault(); setBusy(true); setError('');
    try { const value = await api('/agent/jobs',{projectId:id,request:prompt,operationId:operationKey(`agent-${id}`,{request:prompt})}); setJob(value); setOpen(false); finishOperation(`agent-${id}`); try {sessionStorage.setItem(`unforge-job-${id}`,value.id);} catch {} }
    catch(e) { setError(e.message); } finally {setBusy(false);}
  }
  async function cancel() {setBusy(true); try {setJob(await api(`/agent/jobs/${job.id}/cancel`,{}));} catch(e) {setError(e.message);} finally {setBusy(false);}}
  async function apply() {setBusy(true); try {await api(`/agent/jobs/${job.id}/apply`,{}); setJob({...job,status:'applied'}); setReviewApply(false); try {await onApplied();} catch {setError('Proposal applied. Refresh the project to see the updated files.');}} catch(e) {setError(e.message);} finally {setBusy(false);}}
  return <section className="agent-work">
    <div className="section-heading"><div><h2>A coding partner. Your decision.</h2><p>Codex works in a separate copy. Review the changes before bringing them into your project.</p></div><button disabled={!status?.available || dirty || job?.status === 'running' || busy} onClick={() => {setPrompt('');setOpen(true);setError('');}}>Ask Codex<Icon name="arrow"/></button></div>
    {!status?.available && <p className="note">An installed Codex CLI is needed for this optional feature. You can still write a request below and use your preferred coding tool.</p>}
    {status?.available && <p className="note">Uses your configured Codex account; its usage limits or charges apply. One job at a time, with a {status.timeoutSeconds < 60 ? `${status.timeoutSeconds}-second` : `${Math.round(status.timeoutSeconds / 60)}-minute`} time limit. This is not a dollar spending cap.</p>}
    {dirty && <p className="note">Save your current changes before starting a new proposal.</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {job && <article className="job"><div className="section-heading"><div><h3>{job.status === 'completed' && !job.changedFiles?.length ? 'Codex finished without file changes' : labels[job.status] || job.status}</h3><p>{job.request}</p></div>{job.status === 'running' && <button className="secondary" disabled={busy} onClick={cancel}>Stop work</button>}</div>
      {job.error && <p className="error">{job.error}</p>}
      {job.archived && <p className="note">{job.note || 'Only the durable receipt remains. Proposals and logs from previous sessions are not retained.'}</p>}
      {job.changedFiles?.length > 0 && <p>{job.changedFiles.length} changed file{job.changedFiles.length === 1 ? '' : 's'} in this proposal.</p>}
      {agentExplanation(job.output) && <div className="agent-explanation"><h3>From Codex</h3><p>{agentExplanation(job.output)}</p></div>}
      {job.diff && <details open className="diff"><summary>Review proposed changes</summary><pre>{job.diff}</pre></details>}
      <details><summary>Agent output{job.outputTruncated ? ' (truncated)' : ''}</summary><pre className="agent-output">{job.output || 'Waiting for output…'}</pre></details>
      {job.status === 'completed' && <><Consequences compact report={impact}/>{!impact && <p className="note">Proposal consequences have not loaded. <button className="text-button" onClick={() => api(`/agent/jobs/${job.id}/consequences`).then(setImpact).catch(e => setError(e.message))}>Retry inspection</button></p>}<p className="note">Review the consequences, changes, and any checks Codex reports before applying. Applying updates your local files; publishing is a separate step.</p><button disabled={dirty || busy || !impact || !job.changedFiles?.length} onClick={() => setReviewApply(true)}>Bring these changes into my project<Icon name="check"/></button></>}
      {job.status === 'applied' && <p className="notice">Changes are in your working files. Try them, then save a version when you want to keep them.</p>}
    </article>}
    {open && <Modal title="What should Codex work on?" busy={busy} onClose={() => setOpen(false)}><form onSubmit={start}><label>Describe the outcome<textarea autoFocus rows={9} required maxLength={8000} value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="Make this page useful for planning my week. Keep it simple, readable, and local."/></label><p className="note">Your request and relevant project context go through your configured Codex account. This uses one shared local attempt and your account’s usage allowance.</p>{dirty && <p className="notice">Save a version before starting. You can write this as a portable request instead.</p>}{error && <p role="alert" className="error">{error}</p>}<div className="form-actions"><button type="button" className="secondary" onClick={() => setOpen(false)} disabled={busy}>Cancel</button><button type="button" className="secondary" disabled={busy || !prompt.trim()} onClick={async () => {setBusy(true);setError('');let done = false;try {await api(`/projects/${id}/request`,{message:prompt});done = true;setOpen(false);await onApplied();} catch(e) {setError(done ? `Request written. Refresh the project: ${e.message}` : e.message);} finally {setBusy(false);}}}>Write request only</button><button disabled={busy || dirty || !status?.available || !prompt.trim()}>{busy ? 'Starting…' : 'Start Codex'}</button></div></form></Modal>}
    {reviewApply && <Modal title="Bring this proposal into your project?" busy={busy} onClose={() => setReviewApply(false)}><p>This updates your working files with the reviewed proposal. Your saved history remains available. Nothing is deployed.</p>{error && <p role="alert" className="error">{error}</p>}<div className="form-actions"><button className="secondary" disabled={busy} onClick={() => setReviewApply(false)}>Cancel</button><button disabled={busy} onClick={apply}>{busy ? 'Applying…' : 'Apply proposal'}</button></div></Modal>}
  </section>;
}
