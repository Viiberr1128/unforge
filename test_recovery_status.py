from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from engine import Engine, Problem
from recovery_status import project_recovery_status


class RecoveryStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'workspace')
        self.project = self.engine.create('Recovery metadata fixture')
        self.root = self.engine.root(self.project['id'])
        self.now = datetime(2026,10,7,12,tzinfo=timezone.utc)

    def declare(self, path='data/status.json'):
        declaration = {'schemaVersion':1,'adapters':[{'id':'hosted-data','label':'Hosted data', 'statusPath':path,'staleAfterSeconds':129600}]}
        (self.root / '.unforge/recovery-status.json').write_text(json.dumps(declaration))
        return declaration

    def test_unconfigured_projects_do_not_claim_hosted_data_coverage(self):
        self.assertFalse(project_recovery_status(self.engine,self.project['id'])['declared'])

    def test_fresh_upload_of_old_data_remains_stale_despite_old_saved_false_flag(self):
        self.declare(); (self.root/'data').mkdir()
        (self.root/'data/status.json').write_text(json.dumps({'state':'cloud-uploaded','cloudSnapshotAt':(self.now-timedelta(days=4)).isoformat(),
            'cloudUploadedAt':self.now.isoformat(),'stale':False,'ageSeconds':0}))
        status = project_recovery_status(self.engine,self.project['id'],now=self.now)['adapters'][0]
        self.assertTrue(status['stale']); self.assertEqual(status['ageSeconds'],4*86400)
        self.assertEqual(status['freshnessBasis'],'cloudSnapshotAt')

    def test_missing_metadata_is_explicitly_unavailable(self):
        self.declare(); result=project_recovery_status(self.engine,self.project['id'],now=self.now)['adapters'][0]
        self.assertEqual(result['state'],'unavailable'); self.assertTrue(result['stale'])

    def test_outside_path_and_links_are_rejected_without_reading_external_data(self):
        self.declare('../external.json')
        with self.assertRaises(Problem): project_recovery_status(self.engine,self.project['id'])
        outside=Path(self.temp.name)/'outside';outside.write_text('private outside data')
        (self.root/'data').mkdir();(self.root/'data/status.json').symlink_to(outside)
        self.declare(); result=project_recovery_status(self.engine,self.project['id'])['adapters'][0]
        self.assertEqual(result['state'],'unavailable')
        self.assertEqual(outside.read_text(),'private outside data')
