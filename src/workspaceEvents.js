export function followWorkspaceChanges(refresh) {
  let source = null;
  let queued = false;
  let running = false;
  let pending = false;
  let disposed = false;
  const changed = () => {
    if (queued || disposed || document.hidden) return;
    if (running) { pending = true; return; }
    queued = true;
    queueMicrotask(async () => {
      queued = false;
      if (!disposed && !document.hidden) {
        running = true;
        try {
          await refresh();
          if (!disposed && !document.hidden) window.dispatchEvent(new Event('unforge-workspace-changed'));
        } finally {
          running = false;
          if (pending) { pending = false; changed(); }
        }
      }
    });
  };
  const visible = () => {
    source?.close();
    source = null;
    if (!document.hidden && !disposed) {
      changed();
      source = new EventSource('/api/events');
      source.onmessage = changed;
    }
  };
  visible();
  document.addEventListener('visibilitychange', visible);
  return () => {
    disposed = true;
    source?.close();
    document.removeEventListener('visibilitychange', visible);
  };
}
