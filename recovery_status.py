"""Source-owned declarations of external-data recovery status; never executes adapters."""
from datetime import datetime, timezone
import json
import re
from pathlib import Path

from backups import regular_reader
from engine import Problem


def _read(path):
    with regular_reader(path) as reader:
        raw = reader.read(65537)
    if len(raw) > 65536: raise Problem('Recovery status metadata exceeds its size limit.')
    value = json.loads(raw)
    if not isinstance(value, dict): raise Problem('Recovery status metadata must be an object.')
    return value


def project_recovery_status(engine, project_id, *, now=None):
    """Read only declared workspace-relative metadata; derive age at read time."""
    current = now or datetime.now(timezone.utc)
    with engine.lock:
        root = engine.root(project_id)
        declaration = engine.safe_path(root, '.unforge/recovery-status.json')
        if not declaration.exists(): return {'declared':False, 'adapters':[], 'scope':'No external-data recovery adapters declared.'}
        document = _read(declaration)
        adapters = document.get('adapters')
        if document.get('schemaVersion') != 1 or not isinstance(adapters, list) or len(adapters) > 10:
            raise Problem('Invalid recovery status declaration.')
        seen = set(); results = []
        for item in adapters:
            if not isinstance(item, dict): raise Problem('Invalid recovery status adapter.')
            identity, label, path = item.get('id'), item.get('label'), item.get('statusPath')
            bound = item.get('staleAfterSeconds', 129600)
            if (not isinstance(identity, str) or not re.fullmatch('[a-z0-9-]{1,64}', identity) or identity in seen
                    or not isinstance(label, str) or not 1 <= len(label) <= 120
                    or not isinstance(path, str) or not path or len(path) > 1024 or Path(path).is_absolute()
                    or any(part in ('', '.', '..') for part in path.split('/')) or '\\' in path or '\0' in path
                    or type(bound) is not int or not 3600 <= bound <= 30 * 86400
                    or not isinstance(item.get('scope', ''), str) or len(item.get('scope', '')) > 500):
                raise Problem('Invalid recovery status adapter declaration.')
            seen.add(identity)
            result = {'id':identity, 'label':label, 'statusPath':path, 'scope':item.get('scope',''),
                      'staleAfterSeconds':bound, 'freshnessBasis':'cloudSnapshotAt', 'stale':True,
                      'ageSeconds':None, 'state':'unavailable', 'error':None}
            try:
                target = engine.safe_path(root, path)
                status = _read(target)
                for field in ('snapshotAt','cloudSnapshotAt','cloudUploadedAt','lastAttemptAt','lastFailureAt','statusUpdatedAt'):
                    value = status.get(field)
                    if value is not None:
                        if not isinstance(value, str) or len(value) > 80: raise Problem('Invalid recovery timestamp.')
                        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
                        if parsed.tzinfo is None or (parsed-current).total_seconds() > 300: raise Problem('Recovery timestamp has no timezone or is in the future.')
                    result[field] = value
                state = status.get('state','unknown'); error = status.get('error')
                if not isinstance(state, str) or len(state) > 80 or (error is not None and (not isinstance(error, str) or len(error) > 2000)):
                    raise Problem('Invalid recovery result metadata.')
                result.update(state=state, error=error)
                if result['cloudSnapshotAt']:
                    age = max(0, (current-datetime.fromisoformat(result['cloudSnapshotAt'].replace('Z','+00:00'))).total_seconds())
                    result.update(ageSeconds=age, stale=age>bound)
                result['restoreTested'] = status.get('restoreTested') is True
                result['metadataOnly'] = True
            except (OSError, ValueError, TypeError, KeyError):
                result.update(state='unavailable', error='Declared recovery status could not be read. Existing backups were not changed.')
            results.append(result)
        return {'declared':True, 'adapters':results, 'scope':'Declared external-data capture and upload metadata; independent application recovery must be verified separately.'}
