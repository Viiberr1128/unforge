import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import backups
from backups import Backups, restic_path
from engine import Engine, Problem

@unittest.skipUnless(restic_path(), 'Install pinned restic using scripts/fetch_restic.py')
class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.engine=Engine(self.root/'home');self.project=self.engine.create('Recovery fixture')
        self.service=Backups(self.engine)
        self.password='a unique recovery passphrase for testing'
        self.dest=self.root/'cloud'
        self.service.configure([{'kind':'icloud','path':str(self.dest)}],self.password)
    def tearDown(self): self.service.close();self.tmp.cleanup()
    def backup(self):
        j=self.service.start(standalone=True);self.service.worker.join(60)
        j=self.service.jobs[j['id']]
        self.assertEqual(j['state'],'completed',j)
        return j,Path(j['destinations'][0]['backupPath'])
    def test_lost_workspace_restores_uncommitted_code_sqlite_and_history_with_external_key(self):
        project=self.engine.root(self.project['id'])
        (project/'new.txt').write_text('unsaved but written work')
        with sqlite3.connect(project/'data.sqlite') as db:
            db.execute('CREATE TABLE sample(value TEXT)');db.execute("INSERT INTO sample VALUES('kept')")
        job,backup=self.backup()
        self.assertEqual(job['destinations'][0]['state'],'upload_pending')
        self.assertNotIn(self.password,(backup/'CONTENTS.json').read_text())
        # A distinct engine and supplied key: original workspace/key are not needed.
        other=Backups(Engine(self.root/'replacement'))
        try:
            result=other.restore(str(backup),self.password,str(self.root/'recovered'))
            recovered=Path(result['path'])/self.project['id']
            self.assertEqual((recovered/'new.txt').read_text(),'unsaved but written work')
            with sqlite3.connect(recovered/'data.sqlite') as db:self.assertEqual(db.execute('SELECT value FROM sample').fetchone()[0],'kept')
            self.engine.git(recovered,'fsck','--full')
            self.assertFalse(result['applicationsStarted'])
            self.assertFalse((Path(result['path'])/'.backups/recovery-key').exists())
        finally:other.close()
    def test_corruption_wrong_key_and_existing_restore_are_rejected(self):
        _,backup=self.backup()
        with self.assertRaises(Problem):self.service.restore(str(backup),'wrong-key',str(self.root/'wrong'))
        self.assertFalse((self.root/'wrong').exists())
        existing=self.root/'existing';existing.mkdir();(existing/'keep').write_text('original')
        with self.assertRaises(Problem):self.service.restore(str(backup),self.password,str(existing))
        self.assertEqual((existing/'keep').read_text(),'original')
        pack=next(p for p in (backup/'repository/data').rglob('*') if p.is_file())
        pack.chmod(0o600)
        pack.write_bytes(b'corruption')
        with self.assertRaises(Problem):self.service.restore(str(backup),self.password,str(self.root/'damaged'))
    def test_no_recursive_backup_or_key_change(self):
        with self.assertRaises(Problem):self.service.configure([{'kind':'folder','path':str(self.engine.home/'bad')}],self.password)
        with self.assertRaises(Problem):self.service.configure([{'kind':'folder','path':str(self.dest)}],'different password here')
    def test_interrupted_record_survives_restart_and_is_not_restarted(self):
        job={'id':'a'*32,'state':'running','createdAt':'2026-09-09','kind':'backup'}
        self.service._record(job)
        other=Backups(self.engine)
        self.assertEqual(other.jobs[job['id']]['state'],'interrupted');self.assertIsNone(other.worker)

    def test_only_real_sqlite_sidecars_are_excluded(self):
        project=self.engine.root(self.project['id'])
        (project/'notes-wal').write_text('ordinary user work')
        db=sqlite3.connect(project/'sample.sqlite')
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE items(value TEXT)')
            db.execute("INSERT INTO items VALUES('committed in WAL')");db.commit()
            target=self.root.resolve()/'snapshot';target.mkdir()
            manifest=self.service._copy_workspace(target)
            self.assertEqual((target/self.project['id']/'notes-wal').read_text(),'ordinary user work')
            excluded={entry['path'] for entry in manifest['excluded']}
            self.assertIn(self.project['id']+'/sample.sqlite-wal',excluded)
            # Context managers alone leave SQLite connections open. A WAL
            # source must become a closed, self-contained output before hashing.
            copied=target/self.project['id']/'sample.sqlite'
            self.assertFalse(any(Path(str(copied)+suffix).exists() for suffix in ('-wal','-shm','-journal')))
            self.service.verify_workspace(target)
            with sqlite3.connect(target/self.project['id']/'sample.sqlite') as recovered:
                self.assertEqual(recovered.execute('SELECT value FROM items').fetchone()[0],'committed in WAL')
        finally:db.close()

    def test_open_wal_database_survives_encrypted_backup_and_independent_restore(self):
        project=self.engine.root(self.project['id'])
        db=sqlite3.connect(project/'live.sqlite')
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE work(value TEXT)')
            db.execute("INSERT INTO work VALUES('committed while app remains open')")
            db.commit()
            self.assertTrue((project/'live.sqlite-wal').exists())
            _,backup=self.backup()
            result=self.service.restore(str(backup),self.password,str(self.root/'wal-recovered'))
            restored=Path(result['path'])/self.project['id']/'live.sqlite'
            self.assertFalse(any(Path(str(restored)+suffix).exists() for suffix in ('-wal','-shm','-journal')))
            self.service.verify_workspace(Path(result['path']))
            recovered=sqlite3.connect(restored)
            try:
                self.assertEqual(recovered.execute('SELECT value FROM work').fetchone()[0],
                                 'committed while app remains open')
                self.assertEqual(recovered.execute('PRAGMA journal_mode').fetchone()[0],'delete')
            finally:recovered.close()
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'wal')
            self.assertEqual(db.execute('SELECT count(*) FROM work').fetchone()[0],1)
        finally:db.close()

    def test_recovered_workspace_can_back_up_again_without_losing_nested_inventory_name(self):
        project=self.engine.root(self.project['id'])
        (project/'UNFORGE-RECOVERY.json').write_text('A project file with the same name must survive')
        _,backup=self.backup()
        first=self.service.restore(str(backup),self.password,str(self.root/'first-recovery'))
        recovered=Backups(Engine(Path(first['path'])))
        try:
            recovered.configure([{'kind':'folder','path':str(self.root/'second-backups')}],self.password)
            job=recovered.start();recovered.worker.join(60)
            receipt=recovered.jobs[job['id']]
            self.assertEqual(receipt['state'],'completed',receipt)
            second=recovered.restore(receipt['destinations'][0]['backupPath'],self.password,str(self.root/'second-recovery'))
            final=Path(second['path'])
            self.assertEqual((final/self.project['id']/'UNFORGE-RECOVERY.json').read_text(),
                             'A project file with the same name must survive')
            manifest=recovered.verify_workspace(final)
            self.assertNotIn('UNFORGE-RECOVERY.json',{entry['path'] for entry in manifest['files']})
            self.assertIn({'path':'UNFORGE-RECOVERY.json','reason':'previous recovery inventory'},manifest['excluded'])
        finally:recovered.close()

    def test_late_external_edit_prevents_complete_snapshot(self):
        project=self.engine.root(self.project['id'])
        target=self.root.resolve()/'snapshot';target.mkdir()
        original_digest=backups.digest
        changed=False
        def edit_after_capture(path, cancel=None):
            nonlocal changed
            result=original_digest(path)
            if Path(path).name == 'README.md' and not changed:
                changed=True
                (project/'README.md').write_text('an external editor changed this after capture')
            return result
        with patch('backups.digest',side_effect=edit_after_capture):
            with self.assertRaisesRegex(Problem,'changed during the snapshot'):
                self.service._copy_workspace(target)

    def test_restore_manifest_rejects_undeclared_files_and_links(self):
        target=self.root.resolve()/'snapshot';target.mkdir()
        self.service._copy_workspace(target)
        self.service.verify_workspace(target)
        extra=target/'not-declared.txt';extra.write_text('unexpected')
        with self.assertRaisesRegex(Problem,'Unexpected or missing'):
            self.service.verify_workspace(target)
        extra.unlink()
        (target/'external').symlink_to(self.root.resolve(),target_is_directory=True)
        with self.assertRaisesRegex(Problem,'links'):
            self.service.verify_workspace(target)

    def test_destination_ancestor_replacement_is_rejected(self):
        actual=self.root.resolve()/'actual';actual.mkdir()
        holder=self.root.resolve()/'holder';holder.mkdir()
        (holder/'destination').mkdir()
        configured=holder/'destination'
        self.service.configure([{'kind':'folder','path':str(configured)}])
        configured.rmdir();holder.rmdir();holder.symlink_to(actual,target_is_directory=True)
        (actual/'destination').mkdir()
        job=self.service.start();self.service.worker.join(60)
        self.assertEqual(self.service.jobs[job['id']]['state'],'failed')
        self.assertIn('symbolic link',self.service.jobs[job['id']]['error'])
        self.assertEqual(list((actual/'destination').iterdir()),[])
