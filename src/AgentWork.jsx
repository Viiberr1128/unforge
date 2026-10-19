import { useEffect, useRef, useState } from 'react';
import { api } from './api.js';
import { Modal, Icon } from './ui.jsx';
import { agentExplanation } from './agentOutput.js';
import { agentWorkView, attentionMessage, providerSummary } from './agentWorkState.js';
import { useLeaveGuard } from './useLeaveGuard.js';
import { operationKey, finishOperation } from './operationKey.js';
import Consequences from './Consequences.jsx';

function commandText(check) {
  return check?.argv?.map(value => /\s/.test(value) ? JSON.stringify(value) : value).join(' ') || '';
}

function PlanView({ plan }) {
  return <div className="outcome-plan" aria-labelledby="outcome-plan-heading">
    <h3 id="outcome-plan-heading">Plan</h3>
    <dl>
      <div><dt>Outcome</dt><dd>{plan.outcome}</dd></div>
      <div><dt>Change</dt><dd>{plan.allowPaths?.length ? <ul className="path-list">{plan.allowPaths.map(path => <li key={path}><code>{path}</code></li>)}</ul> : plan.question}</dd></div>
      <div><dt>Check</dt><dd>{plan.focusedCheck ? <code className="command-line">{commandText(plan.focusedCheck)}</code> : 'Unforge will run the project checks before integration.'}</dd></div>
    </dl>
    <details><summary>Lane</summary><p>Subscription Fleet · {plan.size} · {plan.taskClass}</p></details>
  </div>;
}

function AdvancedFields({ status, automaticFleet, provider, setProvider, acceptance, setAcceptance, allowPaths, setAllowPaths, size, setSize }) {
  return <fieldset className="advanced-agent-fields"><legend>Advanced</legend>
    <label>Provider<select value={provider} onChange={event => setProvider(event.target.value)}>
      <option value="automatic">Automatic</option>
      {(status?.providers || []).filter(item => item.available).map(item => <option key={item.id} value={item.id}>{item.label}{item.state === 'ready' ? '' : ` — ${providerSummary(item)}`}</option>)}
    </select></label>
    {(provider === 'subscription-fleet' || (provider === 'automatic' && automaticFleet)) && <>
      <label>Acceptance criteria<textarea rows={4} required maxLength={8000} value={acceptance} onChange={event => setAcceptance(event.target.value)}/></label>
      <label>Exact files<textarea rows={5} required value={allowPaths} onChange={event => setAllowPaths(event.target.value)}/></label>
      <label>Task size<select value={size} onChange={event => setSize(event.target.value)}><option value="small">Small</option><option value="medium">Medium</option><option value="large">Large</option></select></label>
    </>}
  </fieldset>;
}

export default function AgentWork({ id, dirty, onApplied, initialRequest, onBlocked }) {
  const [status, setStatus] = useState(null), [job, setJob] = useState(null);
  const [outcome, setOutcome] = useState(''), [plan, setPlan] = useState(null), [mode, setMode] = useState('outcome');
  const [provider, setProvider] = useState('automatic'), [acceptance, setAcceptance] = useState('');
  const [allowPaths, setAllowPaths] = useState(''), [size, setSize] = useState('medium');
  const [open, setOpen] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [reviewApply, setReviewApply] = useState(false), [reviewAcknowledged, setReviewAcknowledged] = useState(false);
  const [impact, setImpact] = useState(null);
  const planHeading = useRef(null);
  const anyAvailable = status?.anyAvailable ?? status?.available;
  const fleetInstalled = status?.providers?.some(item => item.id === 'subscription-fleet' && item.available);
  const fleetAvailable = status?.providers?.some(item => item.id === 'subscription-fleet' && item.available && item.state === 'ready');
  const codexAvailable = status?.providers?.some(item => item.id === 'codex' && item.available);
  const automaticProvider = fleetAvailable ? 'subscription-fleet' : codexAvailable ? 'codex' : fleetInstalled ? 'subscription-fleet' : 'codex';
  const automaticFleet = automaticProvider === 'subscription-fleet';
  const view = agentWorkView(job);
  useLeaveGuard(busy || open || reviewApply, onBlocked);

  useEffect(() => { if (initialRequest) { setOutcome(initialRequest.text); setMode('outcome'); setOpen(true); } }, [initialRequest?.id]);
  useEffect(() => {
    setImpact(null);
    if (job?.status !== 'completed') return;
    let active = true;
    api(`/agent/jobs/${job.id}/consequences`).then(value => { if (active) setImpact(value); })
      .catch(problem => { if (active) setError(`Could not inspect proposal consequences: ${problem.message}`); });
    return () => { active = false; };
  }, [job?.id, job?.status]);
  useEffect(() => {
    let active = true;
    api('/agent/status').then(async value => {
      if (!active) return;
      setStatus(value);
      let saved = value.recentJobs?.find(item => item.projectId === id)?.id;
      if (!saved) { try { saved = sessionStorage.getItem(`unforge-job-${id}`); } catch {} }
      if (saved) {
        try { const result = await api(`/agent/jobs/${saved}`); if (active && result.projectId === id) setJob(result); }
        catch { try { sessionStorage.removeItem(`unforge-job-${id}`); } catch {} }
      }
    }).catch(problem => { if (active) setError(problem.message); });
    return () => { active = false; };
  }, [id]);
  useEffect(() => {
    if (!job || (job.status !== 'running' && !job.bridgeActive)) return;
    let active = true, timer;
    async function update() {
      if (!active || document.hidden) return;
      try {
        const value = await api(`/agent/jobs/${job.id}`);
        if (active) { setJob(value); setError(''); if (value.status === 'running' || value.bridgeActive) timer = setTimeout(update, 1800); }
      } catch (problem) { if (active) { setError(problem.message); timer = setTimeout(update, 5000); } }
    }
    const visibility = () => { clearTimeout(timer); if (!document.hidden) update(); };
    timer = setTimeout(update, 1800);
    document.addEventListener('visibilitychange', visibility);
    return () => { active = false; clearTimeout(timer); document.removeEventListener('visibilitychange', visibility); };
  }, [job?.id, job?.status, job?.bridgeActive]);
  useEffect(() => { if (mode === 'plan') requestAnimationFrame(() => planHeading.current?.focus()); }, [mode]);

  function remember(value) {
    setJob(value); setOpen(false); finishOperation(`agent-${id}`);
    try { sessionStorage.setItem(`unforge-job-${id}`, value.id); } catch {}
  }
  async function makePlan(event) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const value = await api('/agent/plans', {projectId:id, outcome});
      setPlan(value); setAcceptance(value.acceptance || outcome); setAllowPaths((value.allowPaths || []).join('\n'));
      setSize(value.size || 'medium'); setMode('plan');
    } catch (problem) { setError(problem.message); } finally { setBusy(false); }
  }
  async function startPlan() {
    setBusy(true); setError('');
    const intent = {planId:plan.id};
    try { remember(await api('/agent/jobs', {projectId:id, planId:plan.id, operationId:operationKey(`agent-${id}`, intent)})); }
    catch (problem) { setError(problem.message); } finally { setBusy(false); }
  }
  async function startAdvanced(event) {
    event.preventDefault(); setBusy(true); setError('');
    const selected = provider === 'automatic' ? automaticProvider : provider;
    const paths = allowPaths.split('\n').map(value => value.trim()).filter(Boolean);
    const fleet = selected === 'subscription-fleet' ? {acceptance, allowPaths:paths, size} : undefined;
    const intent = {request:outcome, provider:selected, ...(fleet ? {fleet} : {})};
    try { remember(await api('/agent/jobs', {projectId:id, request:outcome, provider:selected, fleet, operationId:operationKey(`agent-${id}`, intent)})); }
    catch (problem) { setError(problem.message); } finally { setBusy(false); }
  }
  async function cancel() { setBusy(true); try { setJob(await api(`/agent/jobs/${job.id}/cancel`, {})); } catch (problem) { setError(problem.message); } finally { setBusy(false); } }
  async function recover() { setBusy(true); setError(''); try { setJob(await api(`/agent/jobs/${job.id}/recover`, {})); } catch (problem) { setError(problem.message); } finally { setBusy(false); } }
  async function reject() { setBusy(true); setError(''); try { setJob(await api(`/agent/jobs/${job.id}/reject`, {})); } catch (problem) { setError(problem.message); } finally { setBusy(false); } }
  async function apply() {
    setBusy(true);
    try {
      await api(`/agent/jobs/${job.id}/apply`, {reviewAcknowledged});
      setJob({...job, status:'applied'}); setReviewApply(false);
      try { await onApplied(); } catch { setError('Proposal applied. Refresh the project to see the updated files.'); }
    } catch (problem) { setError(problem.message); } finally { setBusy(false); }
  }
  function openComposer() {
    setOutcome(''); setPlan(null); setAcceptance(''); setAllowPaths(''); setProvider('automatic');
    setMode('outcome'); setOpen(true); setError('');
  }

  return <section className="agent-work">
    <div className="section-heading"><div><h2>AI work</h2></div><button disabled={!anyAvailable || dirty || ['running','completed','unknown'].includes(job?.status) || job?.bridgeActive || busy} onClick={openComposer}>Start work<Icon name="arrow"/></button></div>
    {!anyAvailable && <p className="note">Install and sign in to Codex or Subscription Fleet.</p>}
    {dirty && <p className="notice">Save your current changes before starting.</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {job && <article className="job job-cockpit">
      <div className="section-heading"><div><h3>{view.title}</h3><p>{job.request}</p></div>{view.action === 'stop' && <button className="secondary" disabled={busy} onClick={cancel}>Stop</button>}</div>
      {(view.phase || job.phaseNote) && <div role="status" aria-live="polite" className="job-phase"><strong>{view.phase}</strong>{job.phaseNote && <span>{job.phaseNote}</span>}</div>}
      {attentionMessage(view.attention) && <p className="notice">{attentionMessage(view.attention)}</p>}
      {job.error && <p className="error">{job.error}</p>}
      {job.fleet?.reviewer?.attention?.length > 0 && <div className="review-attention"><h3>Reviewer concerns</h3><ul>{job.fleet.reviewer.attention.map(item => <li key={item}>{item}</li>)}</ul></div>}
      {view.action === 'apply' && <button disabled={dirty || busy || !impact || !job.changedFiles?.length} onClick={() => {setReviewAcknowledged(false);setReviewApply(true);}}>Bring into project<Icon name="check"/></button>}
      {view.action === 'recover' && job.provider === 'subscription-fleet' && <button className="secondary" disabled={busy} onClick={recover}>{busy ? 'Checking…' : 'Check for saved result'}</button>}
      {job.status === 'applied' && <p className="notice">Changes are in your working files.</p>}
      <details className="job-details"><summary>Details</summary>
        {['failed','unavailable'].includes(job.fleetFinalization?.status) && <p className="notice">The proposal decision was saved, but Fleet calibration was not recorded.</p>}
        {job.status === 'rejected' && job.laneClosed === false && <p className="notice">The isolated proposal copy was retained because it could not be closed.</p>}
        {job.changedFiles?.length > 0 && <ul className="path-list">{job.changedFiles.map(change => <li key={change.path}><code>{change.path}</code> · {change.status}</li>)}</ul>}
        {job.fleet?.reviewer && <section><h3>Fleet reviewer report</h3><pre className="agent-output">{job.fleet.reviewer.report || 'No reviewer narrative was returned.'}</pre></section>}
        {agentExplanation(job.output) && <section className="agent-explanation"><h3>Delegate summary</h3><p>{agentExplanation(job.output)}</p></section>}
        {job.diff && <details className="diff"><summary>Exact diff</summary><pre>{job.diff}</pre></details>}
        <details><summary>Raw output{job.outputTruncated ? ' (truncated)' : ''}</summary><pre className="agent-output">{job.output || 'No output recorded.'}</pre></details>
        {job.status === 'completed' && job.provider === 'subscription-fleet' && <button className="text-button" disabled={busy} onClick={reject}>Discard proposal</button>}
      </details>
    </article>}
    {open && <Modal title={mode === 'plan' ? 'Review the plan' : mode === 'advanced' ? 'Advanced AI work' : 'What do you want changed?'} busy={busy} onClose={() => setOpen(false)}>
      {mode === 'outcome' && <form onSubmit={makePlan}>
        <label>Outcome<textarea autoFocus rows={7} required maxLength={8000} value={outcome} onChange={event => setOutcome(event.target.value)}/></label>
        {error && <p role="alert" className="error">{error}</p>}
        <div className="form-actions"><button type="button" className="secondary" onClick={() => setOpen(false)}>Cancel</button><button type="button" className="secondary" disabled={!outcome.trim()} onClick={() => {setAcceptance(outcome);setMode('advanced');}}>Advanced</button><button disabled={dirty || !fleetAvailable || !outcome.trim()}>{busy ? 'Planning…' : 'Plan the work'}</button></div>
      </form>}
      {mode === 'plan' && <div ref={planHeading} tabIndex="-1">
        <PlanView plan={plan}/>
        {error && <p role="alert" className="error">{error}</p>}
        <div className="form-actions"><button className="secondary" onClick={() => setMode('outcome')}>Back</button><button className="secondary" onClick={() => setMode('advanced')}>Adjust</button><button disabled={dirty || busy || plan.status !== 'ready'} onClick={startPlan}>{busy ? 'Starting…' : 'Start work'}</button></div>
      </div>}
      {mode === 'advanced' && <form onSubmit={startAdvanced}>
        <label>Outcome<textarea autoFocus rows={6} required maxLength={8000} value={outcome} onChange={event => setOutcome(event.target.value)}/></label>
        <AdvancedFields {...{status,automaticFleet,provider,setProvider,acceptance,setAcceptance,allowPaths,setAllowPaths,size,setSize}}/>
        {error && <p role="alert" className="error">{error}</p>}
        <div className="form-actions"><button type="button" className="secondary" onClick={() => setMode(plan ? 'plan' : 'outcome')}>Back</button><button disabled={dirty || !outcome.trim() || ((provider === 'subscription-fleet' || (provider === 'automatic' && automaticFleet)) && (!acceptance.trim() || !allowPaths.trim()))}>{busy ? 'Starting…' : 'Start work'}</button></div>
      </form>}
    </Modal>}
    {reviewApply && <Modal title="Bring this proposal into your project?" busy={busy} onClose={() => setReviewApply(false)}>
      {job?.fleet?.reviewer?.attention?.length > 0 && <div className="review-attention"><h3>Reviewer concerns</h3><ul>{job.fleet.reviewer.attention.map(item => <li key={item}>{item}</li>)}</ul></div>}
      {impact && <Consequences compact report={impact}/>}<h3>Changed files</h3>
      <ul className="path-list">{job?.changedFiles?.map(change => <li key={change.path}><code>{change.path}</code></li>)}</ul>
      {job?.diff && <details className="diff"><summary>Exact diff</summary><pre>{job.diff}</pre></details>}
      {job?.fleet?.reviewer && <label className="acknowledgment"><input type="checkbox" checked={reviewAcknowledged} onChange={event => setReviewAcknowledged(event.target.checked)}/> I reviewed the Fleet report and exact diff.</label>}
      {error && <p role="alert" className="error">{error}</p>}
      <div className="form-actions"><button className="secondary" onClick={() => setReviewApply(false)}>Cancel</button><button disabled={job?.fleet?.reviewer && !reviewAcknowledged} onClick={apply}>{busy ? 'Applying…' : 'Bring into project'}</button></div>
    </Modal>}
  </section>;
}
