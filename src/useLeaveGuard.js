import { useEffect } from 'react';

export function useLeaveGuard(blocked, onBlocked) {
  useEffect(() => { onBlocked?.(blocked); return () => onBlocked?.(false); }, [blocked, onBlocked]);
  useEffect(() => {
    const warn = event => { if (blocked) {event.preventDefault(); event.returnValue = '';} };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [blocked]);
}
