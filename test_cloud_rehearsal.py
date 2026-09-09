"""Fake-helper iCloud rehearsal tests. No iCloud or Restic operation is performed."""
import http.client
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from backups import Backups, atomic, digest
from engine import Engine, Problem, Server


class CloudRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='unforge-cloud-rehearsal-test-')
        self.base = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.engine = Engine(self.base / 'workspace')
        self.service = Backups(self.engine, executable='unused-restic-fixture')
        self.addCleanup(self.service.close)
        self.cloud = self.base / 'fake-cloud'
        self.password = 'private fixture passphrase'
        self.service.configure([{'kind':'icloud', 'path':str(self.cloud)}], self.password)
        self.job_id = 'a' * 32
        self.source = self.cloud / ('fixture-' + self.job_id + '.ufbackup')
        self.source.mkdir()
        (self.source / 'repository').mkdir()
        sample = self.source / 'repository/config'
        sample.write_text('fake encrypted repository; never passed to Restic')
        atomic(self.source / 'CONTENTS.json', {'schema':1, 'files':[
            {'path':'repository/config', 'bytes':sample.stat().st_size, 'sha256':digest(sample)}]})
        self.service._record({'id':self.job_id,'state':'completed','createdAt':'2026-09-09',
                              'destinations':[{'kind':'icloud','path':str(self.cloud),
                                               'backupPath':str(self.source),'state':'cloud_uploaded'}]})
        self.target = self.base / 'recovered'
        self.report = dict(state='rehydrated',files=2,uploaded=2,evicted=2,downloaded=2,
                           evictionVerified=True,contentVerified=False,secondDeviceVerified=False)
        self.result = {'path':str(self.target),'files':3,'applicationsStarted':False}
        self.helper = self.base / 'fake-helper'
        self.helper.write_text('#!' + sys.executable + '\nimport json,sys,select\n'
                               'assert len(sys.argv)==2\n'
                               'assert not select.select([sys.stdin],[],[],0)[0], "parent stdin closed"\n'
                               'print(' + repr(json.dumps(self.report)) + ')\n')
        self.helper.chmod(0o700)

    def rehearse(self, **arguments):
        values = dict(job_id=self.job_id, path=str(self.source), password=self.password, destination=str(self.target))
        values.update(arguments)
        return self.service.cloud_rehearse(**values)

    def stats(self):
        return patch.object(self.service, '_command', return_value=json.dumps({'total_size':100,'total_file_count':3}))

    def evidence(self):
        return self.service.jobs[self.job_id]['destinations'][0].get('cloudRecoveryEvidence', {})

    def incremental_fixture(self):
        repository_id, writer_id = 'b'*64, 'c'*32
        self.vault = self.cloud / ('unforge-' + repository_id + '.ufvault')
        self.vault.mkdir()
        repository = self.vault / 'repository'; repository.mkdir()
        atomic(self.vault/'VAULT.json',{'schema':2,'repositoryId':repository_id,'writerId':writer_id})
        files = []
        for folder, content in [('',b'config'),('keys',b'key'),('snapshots',b'first snapshot')]:
            checksum = hashlib.sha256(content).hexdigest()
            relative = folder+'/'+checksum if folder else 'config'
            item=repository/relative;item.parent.mkdir(exist_ok=True);item.write_bytes(content)
            files.append({'path':relative,'bytes':len(content),'sha256':checksum})
            if folder == 'snapshots': snapshot = checksum
        self.source = self.vault / 'points' / (self.job_id+'.ufpoint');self.source.mkdir(parents=True)
        descriptor = {'schema':2,'format':'unforge-incremental-restic','pointId':self.job_id,
                      'writerId':writer_id,'repositoryId':repository_id,'snapshotId':snapshot,
                      'workspaceManifestSha256':'e'*64,'vaultIdentitySha256':digest(self.vault/'VAULT.json'),
                      'sourceBytes':100,'fileCount':3,'files':files}
        atomic(self.source/'POINT.json',descriptor)
        # A later point contributes extra objects and descriptors to the same
        # vault. A helper count for the old point's requiredFiles is insufficient.
        later_snapshot = hashlib.sha256(b'later snapshot').hexdigest()
        (repository/'snapshots'/later_snapshot).write_bytes(b'later snapshot')
        later=self.vault/'points'/('d'*32+'.ufpoint');later.mkdir()
        atomic(later/'POINT.json',{**descriptor,'pointId':'d'*32,'snapshotId':later_snapshot})
        self.service._record({'id':self.job_id,'state':'completed','createdAt':'2026-09-09',
                              'format':'incremental-v2','snapshotId':snapshot,'repositoryId':repository_id,
                              'destinations':[{'kind':'icloud','path':str(self.cloud),'backupPath':str(self.source),
                                               'vaultPath':str(self.vault),'format':'incremental-v2','state':'cloud_uploaded'}]})
        self.report = {**self.report,**dict.fromkeys(('files','uploaded','evicted','downloaded'),7)}
        return snapshot

    def test_valid_download_then_content_restore_persists_bounded_claim(self):
        with patch('backups.cloud_helper', return_value=self.helper), self.stats() as command, \
                patch.object(self.service, 'restore', return_value=self.result) as restore:
            result = self.rehearse()
        evidence = result['cloudRecoveryEvidence']
        self.assertEqual(evidence['state'],'verified')
        self.assertTrue(evidence['contentVerified'])
        self.assertFalse(evidence['secondDeviceVerified'])
        self.assertEqual(evidence['restoredPath'],str(self.target))
        restore.assert_called_once_with(str(self.source),self.password,str(self.target))
        self.assertNotIn(self.password,str(command.call_args))
        self.assertNotIn(self.password,json.dumps(self.service.state()))
        reopened = Backups(self.engine, executable='unused-restic-fixture')
        try:
            self.assertTrue(reopened.jobs[self.job_id]['destinations'][0]['cloudRecoveryEvidence']['contentVerified'])
        finally: reopened.close()
        self.assertFalse(self.service.children)

    def test_incomplete_metadata_never_reaches_restore(self):
        for changes in [{'evictionVerified':False}, {'downloaded':1}, {'files':True},
                        {'files':3,'uploaded':3,'evicted':3,'downloaded':3},
                        {'state':'uploaded'}, {'secondDeviceVerified':True},
                        {'contentVerified':True}, {'error':'Cloud download failed'}]:
            with self.subTest(changes=changes), patch('backups.cloud_helper', return_value=self.helper), \
                    self.stats(), patch.object(self.service, '_cloud_report', return_value={**self.report,**changes}), \
                    patch.object(self.service, 'restore') as restore:
                with self.assertRaises(Problem): self.rehearse()
                restore.assert_not_called()
                self.assertFalse(self.evidence()['contentVerified'])
                self.assertEqual(self.evidence()['state'],'failed')

    def test_wrong_key_and_invalid_destination_precede_eviction(self):
        with patch('backups.cloud_helper',return_value=self.helper), \
                patch.object(self.service,'_cloud_report') as helper, \
                patch.object(self.service,'_command',side_effect=Problem('Wrong recovery key')):
            with self.assertRaisesRegex(Problem,'Wrong recovery key'): self.rehearse(password='wrong')
            helper.assert_not_called()
        existing = self.base / 'existing'; existing.mkdir()
        link = self.base / 'link'; link.symlink_to(existing,target_is_directory=True)
        for path in [str(existing),str(self.engine.home/'bad'),str(link/'new'),str(self.source/'new'),'relative']:
            with self.subTest(path=path), patch.object(self.service,'_cloud_report') as helper:
                with self.assertRaises(Problem): self.rehearse(destination=path)
                helper.assert_not_called()
        self.assertEqual(list(existing.iterdir()),[])

    def test_only_completed_registered_icloud_point_can_be_evicted(self):
        for changes in [{'job_id':'unknown'}, {'path':str(self.cloud)}, {'password':''}]:
            with self.subTest(changes=changes), patch.object(self.service,'_cloud_report') as helper:
                with self.assertRaises(Problem): self.rehearse(**changes)
                helper.assert_not_called()
        for field, value in [('state','failed'), ('destinations',[])] :
            original = self.service.jobs[self.job_id][field]
            self.service.jobs[self.job_id][field] = value
            try:
                with patch.object(self.service,'_cloud_report') as helper:
                    with self.assertRaises(Problem): self.rehearse()
                    helper.assert_not_called()
            finally: self.service.jobs[self.job_id][field] = original
        with patch.object(self.service,'settings',return_value={'destinations':[]}), \
                patch.object(self.service,'_cloud_report') as helper:
            with self.assertRaises(Problem): self.rehearse()
            helper.assert_not_called()
        with patch.object(self.service,'worker') as worker:
            worker.is_alive.return_value = True
            with self.assertRaisesRegex(Problem,'current backup'): self.rehearse()

    def test_download_alone_does_not_claim_decrypted_content(self):
        with patch('backups.cloud_helper',return_value=self.helper), self.stats(), \
                patch.object(self.service,'_cloud_report',return_value=self.report), \
                patch.object(self.service,'restore',side_effect=Problem('Restored checksum mismatch')):
            with self.assertRaisesRegex(Problem,'checksum'): self.rehearse()
        evidence = self.evidence()
        self.assertEqual(evidence['rehydration']['state'],'rehydrated')
        self.assertFalse(evidence['contentVerified'])
        self.assertFalse(evidence['secondDeviceVerified'])
        self.assertEqual(evidence['state'],'failed')

    def test_failed_receipt_write_does_not_publish_unpersisted_success(self):
        def fail_final_receipts(path, value):
            evidence = value.get('destinations',[{}])[0].get('cloudRecoveryEvidence',{})
            if evidence.get('state') in ('verified','failed'):
                raise OSError('Fixture disk full')
            atomic(path,value)
        with patch('backups.cloud_helper',return_value=self.helper), self.stats(), \
                patch.object(self.service,'_cloud_report',return_value=self.report), \
                patch.object(self.service,'restore',return_value=self.result), \
                patch('backups.atomic',side_effect=fail_final_receipts):
            with self.assertRaisesRegex(OSError,'disk full'): self.rehearse()
        self.assertEqual(self.evidence()['state'],'verifying')
        self.assertFalse(self.evidence()['contentVerified'])
        on_disk = json.loads((self.service.root/('job-'+self.job_id+'.json')).read_text())
        self.assertEqual(self.service.jobs[self.job_id],on_disk)

    def test_incremental_rehearsal_covers_entire_vault_and_exact_old_snapshot(self):
        snapshot = self.incremental_fixture()
        with patch('backups.cloud_helper',return_value=self.helper), self.stats() as command, \
                patch.object(self.service,'_cloud_report',return_value=self.report) as helper, \
                patch.object(self.service,'restore',return_value=self.result) as restore:
            result=self.rehearse()
        helper.assert_called_once_with(self.helper,self.vault,timeout=130)
        self.assertEqual(command.call_args.args[0],self.vault/'repository')
        self.assertEqual(command.call_args.args[2:],('--no-lock','stats',snapshot,'--mode','restore-size'))
        restore.assert_called_once_with(str(self.source),self.password,str(self.target))
        self.assertEqual(result['cloudRecoveryEvidence']['vaultFilesVerified'],7)
        self.assertEqual(result['cloudRecoveryEvidence']['snapshotId'],snapshot)
        self.assertFalse(result['cloudRecoveryEvidence']['secondDeviceVerified'])

    def test_incremental_point_only_counts_and_any_vault_content_change_reject_proof(self):
        self.incremental_fixture()
        with patch('backups.cloud_helper',return_value=self.helper), self.stats(), \
                patch.object(self.service,'_cloud_report',return_value={**self.report,**dict.fromkeys(('files','uploaded','evicted','downloaded'),5)}), \
                patch.object(self.service,'restore') as restore:
            with self.assertRaisesRegex(Problem,'counts'): self.rehearse()
            restore.assert_not_called()
        # Corrupt a later point that is not in the selected point's requiredFiles.
        def changed_vault(*args,**kwargs):
            (self.vault/'points'/('d'*32+'.ufpoint')/'POINT.json').write_text('changed during cloud download')
            return self.report
        with patch('backups.cloud_helper',return_value=self.helper), self.stats(), \
                patch.object(self.service,'_cloud_report',side_effect=changed_vault), \
                patch.object(self.service,'restore') as restore:
            with self.assertRaisesRegex(Problem,'complete pre-eviction inventory'): self.rehearse()
            restore.assert_not_called()
        self.assertFalse(self.evidence()['contentVerified'])

    def test_incremental_registration_and_vault_links_are_rejected_before_helper(self):
        self.incremental_fixture()
        entry=self.service.jobs[self.job_id]['destinations'][0]
        for field,value in [('vaultPath',str(self.cloud)),('path',str(self.base))]:
            old=entry[field];entry[field]=value
            try:
                with patch.object(self.service,'_cloud_report') as helper:
                    with self.assertRaises(Problem): self.rehearse()
                    helper.assert_not_called()
            finally:entry[field]=old
        (self.vault/'outside-link').symlink_to(self.base,target_is_directory=True)
        with patch('backups.cloud_helper',return_value=self.helper),patch.object(self.service,'_cloud_report') as helper:
            with self.assertRaisesRegex(Problem,'links'): self.rehearse()
            helper.assert_not_called()

    def test_incremental_upload_metadata_checks_full_vault_not_descriptor_folder(self):
        snapshot=self.incremental_fixture()
        with patch('backups.cloud_helper',return_value=self.helper), \
                patch.object(self.service,'_cloud_report',return_value={'state':'uploaded','files':7,'uploaded':7}) as helper:
            destination=self.service.refresh_cloud(self.job_id)['destinations'][0]
        helper.assert_called_once_with(self.helper,str(self.vault))
        self.assertEqual(destination['state'],'cloud_uploaded')
        self.assertEqual(destination['uploadEvidence']['scope'],'entire shared vault')
        self.assertEqual(destination['uploadEvidence']['selectedSnapshotId'],snapshot)
        with patch('backups.cloud_helper',return_value=self.helper), \
                patch.object(self.service,'_cloud_report',return_value={'state':'uploaded','files':1,'uploaded':1}):
            destination=self.service.refresh_cloud(self.job_id)['destinations'][0]
        self.assertEqual(destination['state'],'upload_pending')
        self.assertIn('Incomplete',destination['uploadEvidence'])

    def test_helper_output_and_time_are_bounded_and_processes_reaped(self):
        for code, timeout, message in [('print("x"*70000)',2,'exceeded'),
                                      ('import time; time.sleep(30)',.15,'did not finish')]:
            with self.subTest(message=message):
                pid_file = self.base / 'helper.pid'
                self.helper.write_text('#!' + sys.executable + '\nimport os,pathlib\n'
                                       f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n' + code + '\n')
                with self.assertRaisesRegex(Problem,message): self.service._cloud_report(self.helper,self.source,timeout=timeout)
                pid = int(pid_file.read_text())
                self.assertEqual(subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True).stdout.strip(),'')
                self.assertFalse(self.service.children)

    def test_http_route_authentication_and_exact_payload(self):
        server = Server(('127.0.0.1',0),self.engine,self.base/'static')
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            origin = 'http://127.0.0.1:' + str(server.server_port)
            body = {'jobId':self.job_id,'path':str(self.source),'password':self.password,'destination':str(self.target)}
            with patch.object(server.backups,'cloud_rehearse',return_value=self.result) as rehearse:
                for token, expected in [('',403),(server.token,200)]:
                    connection = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                    try:
                        connection.request('POST','/api/backups/cloud-rehearse',json.dumps(body),
                                           {'Content-Type':'application/json','Origin':origin,'X-Unforge-Token':token})
                        response = connection.getresponse(); value = json.loads(response.read())
                        self.assertEqual(response.status,expected,value)
                    finally: connection.close()
                rehearse.assert_called_once_with(self.job_id,str(self.source),self.password,str(self.target))
        finally:
            server.shutdown(); server.server_close(); thread.join(5)
            self.assertFalse(thread.is_alive())


if __name__ == '__main__': unittest.main()
