import { useEffect, useRef } from 'react';

export function Icon({ name = 'arrow', size = 20 }) {
  const paths = {
    arrow: 'M5 12h14m-6-6 6 6-6 6', plus: 'M12 5v14M5 12h14',
    folder: 'M3 7V5h6l2 2h10v13H3V7Z', book: 'M12 5v15M12 5C8 2 4 3 2 4v15c4-2 7-1 10 1 3-2 6-3 10-1V4c-4-2-7-1-10 1Z',
    back: 'M19 12H5m6-6-6 6 6 6', close: 'm6 6 12 12M6 18 18 6', check: 'm5 12 4 4L19 6',
    export: 'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5', clock: 'M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
    file: 'M5 3h9l5 5v13H5V3Zm9 0v6h5',
    preferences: 'M4 7h5m5 0h6M4 17h10m5 0h1M9 4v6m5 4v6',
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] || paths.arrow}/></svg>;
}

export function Modal({ title, children, onClose, busy }) {
  const ref = useRef(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog ref={ref} onCancel={event => { event.preventDefault(); if (!busy) onClose(); }} aria-labelledby="modal-title">
    <div className="modal-head"><h2 id="modal-title">{title}</h2><button className="icon-button" aria-label="Close dialog" onClick={onClose} disabled={busy}><Icon name="close"/></button></div>
    {children}
  </dialog>;
}

export function relativeDate(value) {
  if (!value) return 'Saved locally';
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return 'Saved locally';
  const elapsed = Math.max(0, Date.now() - time);
  if (elapsed < 60000) return 'Just now';
  if (elapsed < 3600000) return `${Math.floor(elapsed / 60000)} min ago`;
  if (elapsed < 86400000) return `${Math.floor(elapsed / 3600000)} hr ago`;
  return new Date(time).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
