import { useEffect, useState } from 'react';
import { api } from './api.js';

function timestamp(value) {
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}
function completedLabel(job) {
  const time = timestamp(job?.finishedAt || job?.createdAt);
  return time ? `Last checked copy ${new Date(time).toLocaleString()}.` : 'No completed backup recorded.';
}

// Retain only this small summary in React state, not backup paths or settings.
export function backupHealth(state) {
  const schedule = state.schedule || {};
  const jobs = [...(state.jobs || [])].sort((a,b) => timestamp(b.createdAt)-timestamp(a.createdAt));
  const running = jobs.some(job => job.state === 'running') || !!schedule.runningJobId;
  const latest = jobs.find(job => job.state === 'completed');
  const shouldPoll = !!schedule.enabled || running || (!!state.configured && !!schedule.pending);
  const lastCopy = latest ? completedLabel(latest) : '';
  function result(message, attention = false, detail = lastCopy) {
    return {message,detail,attention,shouldPoll};
  }
  if (state.settingsRecoveryRequired) return result('Backup settings need repair.',true);
  if (schedule.recoveryRequired || schedule.persistenceError) return result('Automatic backup settings need repair.',true);
  if (!state.configured) return result('Backups not set up.',false,'');
  if (!state.available) return result('The backup tool is unavailable.',true);
  if (schedule.error || schedule.lastError || (schedule.enabled && schedule.blockedReason)) {
    return result('Automatic backups need attention.',true);
  }
  const newest = jobs[0];
  if (newest && ['failed','interrupted'].includes(newest.state)) {
    return result(newest.state === 'failed' ? 'The latest backup failed.' : 'The latest backup was interrupted.',true);
  }
  if (running) return result('Backup in progress.');
  if (!latest) return result('No completed backup yet.',false,'');
  const destinations = latest.destinations || [];
  if (destinations.some(item => item.cloudRecoveryEvidence?.state === 'failed')) {
    return result('The latest cloud recovery test needs attention.',true);
  }
  const cloud = destinations.filter(item => ['icloud','drive'].includes(item.kind));
  if (cloud.some(item => !['cloud_uploaded','cloud_verified'].includes(item.state)
      && item.cloudRecoveryEvidence?.contentVerified !== true)) {
    return result('Cloud upload unconfirmed.');
  }
  if (cloud.some(item => item.cloudRecoveryEvidence?.contentVerified === true)) {
    return result('Cloud recovery tested on this Mac.');
  }
  if (cloud.length) return result('Upload reported; cloud recovery untested.');
  return result('Copy checked locally.');
}

export default function BackupHealth({onOpen,disabled}) {
  const [summary,setSummary] = useState(null);
  const [unavailable,setUnavailable] = useState(false);
  useEffect(() => {
    let disposed = false, inFlight = false, refreshAgain = false, timer = null;
    const stopTimer = () => { if (timer) clearInterval(timer); timer = null; };
    const refresh = async () => {
      if (disposed || document.hidden) return;
      if (inFlight) { refreshAgain = true; return; }
      inFlight = true;
      try {
        const value = backupHealth(await api('/backups'));
        if (!disposed) {
          setSummary(value); setUnavailable(false);
          if (value.shouldPoll && !document.hidden && !timer) timer = setInterval(refresh,30000);
          if (!value.shouldPoll) stopTimer();
        }
      } catch {
        if (!disposed) {
          setUnavailable(true);
          if (!document.hidden && !timer) timer = setInterval(refresh,30000);
        }
      } finally {
        inFlight = false;
        if (refreshAgain) { refreshAgain = false; refresh(); }
      }
    };
    refresh();
    const visible = () => { if (document.hidden) stopTimer(); else refresh(); };
    document.addEventListener('visibilitychange',visible);
    window.addEventListener('unforge-backups-updated',refresh);
    return () => {
      disposed = true;
      stopTimer();
      document.removeEventListener('visibilitychange',visible);
      window.removeEventListener('unforge-backups-updated',refresh);
    };
  }, []);
  const attention = unavailable || summary?.attention;
  return <aside aria-label="Backup health" style={{display:'flex',alignItems:'baseline',justifyContent:'space-between',gap:'1rem',flexWrap:'wrap',marginBottom:'1rem',fontSize:'.85rem'}}>
    <span role="status" aria-live="polite">
      {attention ? <strong>{unavailable ? 'Backup status unavailable.' : summary.message}</strong> : summary?.message || 'Checking backup status…'}
      {!unavailable && summary?.detail && <> <span style={{opacity:.7}}>{summary.detail}</span></>}
    </span>
    <button type="button" className="text-button" disabled={disabled} onClick={onOpen}>{attention ? 'Review backups' : 'View backup status'}</button>
  </aside>;
}
