// One in-flight read and one pending refresh, shared by every event source.
export function coalescedRefresh(refresh, {visible = () => true, onValue = () => {}} = {}) {
  let inFlight = null;
  let pending = false;
  let disposed = false;
  return {
    request() {
      if (disposed || !visible()) return Promise.resolve();
      if (inFlight) { pending = true; return inFlight; }
      inFlight = (async () => {
        let value;
        do {
          pending = false;
          if (disposed || !visible()) break;
          try { value = await refresh(); }
          catch (error) { if (disposed) return; throw error; }
          if (!disposed) onValue(value);
        } while (pending && !disposed && visible());
        return value;
      })().finally(() => { inFlight = null; });
      return inFlight;
    },
    dispose() { disposed = true; pending = false; },
  };
}
