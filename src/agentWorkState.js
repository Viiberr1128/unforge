const statusViews = {
  running: ['Work is underway', 'stop'],
  completed: ['Ready to try', 'apply'],
  failed: ['This attempt needs attention', 'none'],
  cancelled: ['Work stopped', 'none'],
  timed_out: ['The time limit was reached', 'none'],
  applied: ['Changes are in your project', 'none'],
  rejected: ['Proposal discarded', 'none'],
  archived: ['Saved attempt', 'none'],
  unknown: ['Checking for saved work', 'recover'],
};

export function agentWorkView(job) {
  if (!job) return { title: 'No active work', action: 'start', phase: null, attention: null };
  const [fallbackTitle, configuredAction] = statusViews[job.status] || [job.status, 'none'];
  const action = job.status === 'unknown' && (job.bridgeActive || job.bridgeRecoverable === false)
    ? 'none' : configuredAction;
  const noChanges = job.status === 'completed' && !job.changedFiles?.length;
  const phase = ['running', 'unknown'].includes(job.status)
    ? (job.phase?.label || (job.status === 'running' ? 'Working in a separate copy' : null))
    : null;
  return {
    title: noChanges ? 'Finished without source changes' : fallbackTitle,
    action: noChanges ? 'none' : action,
    phase,
    attention: job.attention || null,
  };
}

export function providerSummary(provider) {
  if (!provider?.available) return 'Not installed';
  const labels = {
    ready: 'Ready', 'needs-refresh': 'Usage needs refreshing',
    'health-unavailable': 'Connection status unavailable', unavailable: 'Unavailable',
  };
  return labels[provider.state] || 'Ready';
}

export function attentionMessage(kind) {
  return {
    sign_in: 'The selected lane needs you to sign in.',
    quota: 'The selected lane is out of recorded usage.',
    failed_check: 'The focused check did not pass.',
    recovery: 'The saved Fleet outcome needs recovery inspection.',
    failed: 'Fleet stopped before a candidate was ready.',
  }[kind] || '';
}
