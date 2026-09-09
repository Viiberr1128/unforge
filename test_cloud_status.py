"""Read-only cloud evidence tests; no cloud account or actual restic operation."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

import backups
from backups import Backups
from engine import Engine, Problem


class CloudStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.service = Backups(Engine(self.base / 'workspace'), executable='/unused-restic')
        module = self.base / 'module'
        (module / '.tools').mkdir(parents=True)
        self.helper = module / '.tools' / 'unforge-cloud-status'
        self.helper.write_text('#!' + sys.executable + '\nprint("{}")\n')
        self.helper.chmod(0o700)
        self.module_patch = patch('backups.__file__', str(module / 'backups.py'))
        self.module_patch.start()
        self.job_id = uuid.uuid4().hex
        self.destination = {'kind':'icloud','name':'Test cloud folder','path':str(self.base / 'cloud'),
                            'backupPath':str(self.base / 'cloud' / 'sample.ufbackup'),
                            'state':'cloud_uploaded','checkedAt':'2000-01-01T00:00:00Z',
                            'uploadEvidence':{'state':'uploaded','files':2,'uploaded':2}}
        self.service._record({'id':self.job_id,'state':'completed','kind':'backup',
                              'createdAt':'2026-01-01T00:00:00Z','destinations':[self.destination]})
    def tearDown(self):
        self.service.close()
        self.module_patch.stop()
        self.temp.cleanup()

    def refresh(self, report):
        with patch.object(self.service, '_cloud_report', return_value=report):
            return self.service.refresh_cloud(self.job_id)['destinations'][0]

    def test_complete_upload_metadata_is_not_promoted_to_remote_recovery_proof(self):
        destination = self.refresh({'state':'uploaded','files':4,'uploaded':4,
                                    'evidence':'macOS iCloud metadata; no remote readback'})
        self.assertEqual(destination['state'], 'cloud_uploaded')
        self.assertNotEqual(destination['state'], 'cloud_verified')
        self.assertNotEqual(destination['checkedAt'], '2000-01-01T00:00:00Z')
        self.assertIn('no remote readback', destination['uploadEvidence']['evidence'])
        reloaded = Backups(self.service.engine, executable='/unused-restic')
        try:
            self.assertEqual(reloaded.jobs[self.job_id]['destinations'][0], destination)
        finally:
            reloaded.close()

    def test_missing_or_failed_upload_evidence_clears_previous_uploaded_label(self):
        failures = [OSError('Helper cannot start'), Problem('Metadata response too large'),
                    subprocess.TimeoutExpired('fake-helper',15), ValueError('Malformed JSON')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.service.jobs[self.job_id]['destinations'][0] = copy.deepcopy(self.destination)
                with patch.object(self.service, '_cloud_report', side_effect=failure):
                    result = self.service.refresh_cloud(self.job_id)['destinations'][0]
                self.assertEqual(result['state'], 'upload_pending')
                self.assertNotEqual(result['checkedAt'], '2000-01-01T00:00:00Z')
                self.assertIn('unavailable', result['uploadEvidence'].lower())

    def test_absent_or_nonexecutable_helper_clears_prior_upload_claim(self):
        self.helper.chmod(0o600)
        # Ignore a separately installed helper: this fixture represents an
        # installation without a callable metadata helper.
        with patch('backups.os.access', return_value=False):
            result = self.service.refresh_cloud(self.job_id)['destinations'][0]
        self.assertEqual(result['state'], 'upload_pending')
        self.assertIn('helper unavailable', result['uploadEvidence'])
        self.assertNotEqual(result['checkedAt'], '2000-01-01T00:00:00Z')

    def test_partial_unknown_and_nonicloud_reports_are_not_uploaded(self):
        for report in ({'state':'upload_pending','files':4,'uploaded':2},
                       {'state':'unknown','files':4,'uploaded':4,'error':'Traversal incomplete'},
                       {'state':'not_icloud','files':4,'uploaded':0}):
            with self.subTest(report=report):
                result = self.refresh(report)
                self.assertEqual(result['state'], 'upload_pending')
                self.assertEqual(result['uploadEvidence'], report)

    def test_invalid_json_shapes_and_contradictory_counts_never_claim_uploaded(self):
        reports = [None, [], 'uploaded', {'state':'other'}, {'state':'uploaded'},
                   {'state':'uploaded','files':0,'uploaded':0},
                   {'state':'uploaded','files':3,'uploaded':2},
                   {'state':'uploaded','files':2,'uploaded':3},
                   {'state':'uploaded','files':True,'uploaded':True},
                   {'state':'uploaded','files':2.0,'uploaded':2.0},
                   {'state':'uploaded','files':2,'uploaded':2,'error':'Upload failed'}]
        for report in reports:
            with self.subTest(report=report):
                result = self.refresh(report)
                self.assertEqual(result['state'], 'upload_pending')
                self.assertIsInstance(result['uploadEvidence'], str)
                self.assertIn('unavailable', result['uploadEvidence'].lower())

    def test_other_destinations_are_not_mislabeled_as_icloud(self):
        local = {**self.destination,'kind':'folder','state':'copied_locally'}
        drive = {**self.destination,'kind':'drive','state':'upload_pending'}
        self.service.jobs[self.job_id]['destinations'].extend([local,drive])
        with patch.object(self.service, '_cloud_report', return_value={'state':'uploaded','files':1,'uploaded':1}) as helper:
            result = self.service.refresh_cloud(self.job_id)
        self.assertEqual(helper.call_count, 1)
        self.assertEqual(result['destinations'][1], local)
        self.assertEqual(result['destinations'][2], drive)

    def test_metadata_write_failure_does_not_mutate_live_receipt(self):
        original = copy.deepcopy(self.service.jobs[self.job_id])
        with patch.object(self.service, '_cloud_report', return_value={'state':'upload_pending','files':2,'uploaded':0}), \
             patch.object(self.service, '_record', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(OSError, 'Disk full'):
                self.service.refresh_cloud(self.job_id)
        self.assertEqual(self.service.jobs[self.job_id], original)

    def test_incomplete_or_unknown_backup_is_not_inspected(self):
        with self.assertRaises(Problem):
            self.service.refresh_cloud('not-a-known-job')
        self.service.jobs[self.job_id]['state'] = 'running'
        with patch.object(self.service, '_cloud_report', side_effect=AssertionError('Must not inspect')):
            with self.assertRaisesRegex(Problem, 'completed'):
                self.service.refresh_cloud(self.job_id)

    def script(self, body):
        self.helper.write_text('#!' + sys.executable + '\n' + body)
        self.helper.chmod(0o700)

    def test_fake_helper_receives_one_path_and_returns_bounded_metadata(self):
        self.script('import json,sys\nprint(json.dumps({"state":"uploaded","files":1,"uploaded":1,"path":sys.argv[1],"argc":len(sys.argv)}))\n')
        path = str(self.base / 'folder with spaces' / 'sample.ufbackup')
        result = self.service._cloud_report(self.helper,path)
        self.assertEqual(result['path'], path)
        self.assertEqual(result['argc'], 2)
        self.assertEqual(self.service.children, set())

    def test_fake_helper_excess_output_is_stopped_and_reaped(self):
        self.script('import sys\nsys.stdout.write("x" * 200000)\nsys.stdout.flush()\n')
        with self.assertRaisesRegex(Problem, 'limit'):
            self.service._cloud_report(self.helper,self.destination['backupPath'])
        self.assertEqual(self.service.children, set())

    def test_fake_helper_nonzero_exit_and_malformed_json_are_not_evidence(self):
        for body in ('import sys\nsys.exit(3)\n', 'print("not JSON")\n'):
            with self.subTest(body=body):
                self.script(body)
                with self.assertRaises((Problem,ValueError)):
                    self.service._cloud_report(self.helper,self.destination['backupPath'])
                self.assertEqual(self.service.children, set())

    def test_closing_stops_and_reaps_helper_without_waiting_for_timeout(self):
        self.script('import time\ntime.sleep(60)\n')
        with patch.object(self.service.closed, 'is_set', side_effect=[False,True]):
            with self.assertRaisesRegex(Problem, 'did not finish'):
                self.service._cloud_report(self.helper,self.destination['backupPath'])
        self.assertEqual(self.service.children, set())


if __name__ == '__main__':
    unittest.main()
