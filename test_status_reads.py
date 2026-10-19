"""Status reads must not write project indexes or multiply private inventory scans."""
from contextlib import contextmanager
import errno
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_manifest import AppManifest
from backups import Backups, RETENTION_REVIEW_TTL_SECONDS, sync_tree, tree_storage
from engine import Engine, Problem
from incremental_backups import IncrementalBackups
from operating import Operating


def index_identity(path):
    info = path.stat()
    return path.read_bytes(), (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def denied_scandir(folder, *, after=1):
    """Exercise os.walk's real error path without relying on Unix user identity."""
    original = os.scandir
    visits = []
    def scan(path):
        if not isinstance(path, int) and Path(path) == folder:
            visits.append(path)
            if len(visits) >= after:
                raise PermissionError(errno.EACCES, 'Unreadable regression subtree', str(folder))
        return original(path)
    with patch('os.scandir', side_effect=scan):
        yield visits


class ReadOnlyGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name).resolve() / 'workspace')
        self.pid = self.engine.create('Read status fixture')['id']
        self.root = self.engine.root(self.pid)
        self.index = self.root / '.git/index'
        readme = self.root / 'README.md'
        info = readme.stat()
        # The contents stay unchanged, but the cached Git stat is stale.
        os.utime(readme, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))

    def test_status_diff_and_operating_reads_preserve_index_bytes_and_identity(self):
        before = index_identity(self.index)
        self.assertEqual(self.engine.git(self.root, 'status', '--porcelain'), b'')
        self.assertEqual(index_identity(self.index), before, 'status changed the index')
        self.assertEqual(self.engine.git(self.root, 'diff', '--no-ext-diff', '--no-textconv', 'HEAD', '--'), b'')
        self.assertEqual(index_identity(self.index), before, 'diff changed the index')
        self.assertFalse(self.engine.detail(self.pid)['dirty'])
        self.assertEqual(index_identity(self.index), before, 'project detail changed the index')
        self.assertFalse(Operating(self.engine).get(self.pid)['sourceDirty'])
        self.assertEqual(index_identity(self.index), before, 'operating status changed the index')
        self.assertFalse(AppManifest(self.engine).get(self.pid)['sourceDirty'])
        self.assertEqual(index_identity(self.index), before)

    def test_explicit_add_and_commit_still_write_index_and_history(self):
        before = index_identity(self.index)
        head = self.engine.git(self.root, 'rev-parse', 'HEAD')
        self.engine.edit(self.pid, 'new.txt', 'Saved new contents')
        self.engine.save(self.pid, 'Save explicit source')
        self.assertNotEqual(index_identity(self.index), before)
        self.assertNotEqual(self.engine.git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.engine.git(self.root, 'show', 'HEAD:new.txt'), b'Saved new contents')
        self.assertEqual(self.engine.git(self.root, 'status', '--porcelain'), b'')

    def test_app_status_during_capture_does_not_invalidate_consistent_snapshot(self):
        service = Backups(self.engine, executable='/unused-restic')
        self.addCleanup(service.close)
        destination = Path(self.temp.name).resolve() / 'capture'
        destination.mkdir()
        original = service._workspace_listing
        calls = 0
        def listing():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.assertFalse(AppManifest(self.engine).get(self.pid)['sourceDirty'])
            return original()
        before = index_identity(self.index)
        with patch.object(service, '_workspace_listing', side_effect=listing):
            manifest = service._copy_workspace(destination, capacity_check=lambda _: None)
        self.assertEqual(calls, 2)
        self.assertEqual(index_identity(self.index), before)
        self.assertTrue(service.verify_workspace(destination))
        self.assertIn(self.pid + '/.git/index', {entry['path'] for entry in manifest['files']})


class BackupStatusReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name).resolve() / 'workspace')
        self.service = Backups(self.engine, executable='/unused-restic')
        self.addCleanup(self.service.close)
        self.review = {'automaticPruning':False, 'generations':[], 'privateRepositoryBytes':123}

    def retention(self):
        return self.service.state()['retentionReview']

    def test_parallel_requests_share_one_scan_and_do_not_block_on_it(self):
        entered, release = threading.Event(), threading.Event()
        result = []
        def scan(*_, **__):
            entered.set()
            if not release.wait(3): raise AssertionError('Inventory fixture timed out')
            return self.review
        with patch.object(IncrementalBackups, 'retention_review', side_effect=scan) as scanner:
            thread = threading.Thread(target=lambda: result.append(self.retention()))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                for _ in range(12):
                    pending = self.retention()
                    self.assertEqual(pending['cacheStatus'], 'not-yet-available')
                    self.assertTrue(pending['refreshPending'])
                self.assertEqual(scanner.call_count, 1)
            finally:
                release.set(); thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0]['cacheStatus'], 'current')
            cached = self.retention()
            self.assertEqual(cached['privateRepositoryBytes'], 123)
            self.assertEqual(scanner.call_count, 1)
            cached['privateRepositoryBytes'] = 999
            self.assertEqual(self.retention()['privateRepositoryBytes'], 123)

    def test_cache_ttl_expires_and_terminal_receipt_invalidates_it(self):
        with patch('backups.time.monotonic', return_value=100) as clock, patch.object(IncrementalBackups, 'retention_review', return_value=self.review) as scan:
            first = self.retention()
            clock.return_value = 100 + RETENTION_REVIEW_TTL_SECONDS - 1
            self.assertEqual(self.retention()['checkedAt'], first['checkedAt'])
            self.assertEqual(scan.call_count, 1)
            clock.return_value += 2
            self.retention(); self.assertEqual(scan.call_count, 2)
            self.service._record({'id':'a' * 32, 'state':'completed'})
            self.retention(); self.assertEqual(scan.call_count, 3)

    def test_active_backup_defers_cold_and_cached_inventory_then_refreshes(self):
        alive = True
        self.service.worker = SimpleNamespace(is_alive=lambda: alive)
        with patch.object(IncrementalBackups, 'retention_review', return_value=self.review) as scan:
            cold = self.retention()
            self.assertEqual(cold['cacheStatus'], 'not-yet-available')
            self.assertTrue(cold['refreshDeferred']); scan.assert_not_called()
            alive = False
            checked = self.retention()
            alive = True
            stale = self.retention()
            self.assertEqual(stale['cacheStatus'], 'stale')
            self.assertEqual(stale['checkedAt'], checked['checkedAt'])
            self.assertEqual(stale['privateRepositoryBytes'], 123)
            self.assertEqual(scan.call_count, 1)
            self.service._record({'id':'a' * 32, 'state':'failed'})
            alive = False
            self.assertEqual(self.retention()['cacheStatus'], 'current')
            self.assertEqual(scan.call_count, 2)
        self.service.worker = None

    def test_scan_errors_are_bounded_and_never_marked_current(self):
        with patch.object(IncrementalBackups, 'retention_review', return_value=self.review) as scan:
            checked = self.retention()
            self.service._record({'id':'a' * 32, 'state':'completed'})
            scan.side_effect = Problem('Broken inventory fixture')
            failed = self.retention()
            self.assertEqual(failed['cacheStatus'], 'stale')
            self.assertEqual(failed['checkedAt'], checked['checkedAt'])
            self.assertIn('error', failed)
            self.assertEqual(self.retention()['cacheStatus'], 'stale')
            self.assertEqual(scan.call_count, 2)

    def test_terminal_receipt_during_scan_keeps_result_historical_and_refreshes_next_read(self):
        calls = 0
        def scan(*_, **__):
            nonlocal calls
            calls += 1
            if calls == 1: self.service._record({'id':'a' * 32, 'state':'completed'})
            return self.review
        with patch.object(IncrementalBackups, 'retention_review', side_effect=scan):
            historical = self.retention()
            self.assertEqual(historical['cacheStatus'], 'stale')
            self.assertTrue(historical['refreshDeferred'])
            self.assertEqual(self.retention()['cacheStatus'], 'current')
            self.assertEqual(calls, 2)

    def test_unexpected_scan_error_releases_singleflight_slot(self):
        with patch.object(IncrementalBackups, 'retention_review', side_effect=RuntimeError('fixture')):
            with self.assertRaisesRegex(RuntimeError, 'fixture'): self.retention()
        with patch.object(IncrementalBackups, 'retention_review', return_value=self.review):
            self.assertEqual(self.retention()['cacheStatus'], 'current')

    def test_inventory_never_reads_pack_contents_and_rejects_links(self):
        repository = self.service.root / 'incremental/repository/data/ab'
        repository.mkdir(parents=True)
        (repository / 'one').write_bytes(b'opaque encrypted fixture')
        import incremental_backups
        original_reader = incremental_backups.regular_reader
        @contextmanager
        def metadata_reader(path):
            with original_reader(path) as reader:
                if Path(path).parent == repository:
                    yield SimpleNamespace(fileno=reader.fileno, read=lambda *_: self.fail('Read encrypted object contents'))
                else:
                    yield reader
        with patch('incremental_backups.regular_reader', metadata_reader), patch('incremental_backups.digest', side_effect=AssertionError('Content hash in status')):
            self.assertEqual(self.retention()['privateRepositoryBytes'], len(b'opaque encrypted fixture'))
            (repository / 'linked').symlink_to(repository / 'one')
            self.service._record({'id':'a' * 32, 'state':'completed'})
            rejected = self.retention()
            self.assertEqual(rejected['cacheStatus'], 'stale')
            self.assertIn('error', rejected)

    def test_inventory_stops_between_entries_when_backup_starts(self):
        repository = self.service.root / 'incremental/repository/data/ab'
        repository.mkdir(parents=True)
        for name in ('one', 'two', 'three'): (repository / name).write_bytes(b'opaque')
        import incremental_backups
        original_reader = incremental_backups.regular_reader
        opened = []
        @contextmanager
        def start_backup_after_first(path):
            with original_reader(path) as reader:
                opened.append(Path(path))
                self.service.worker = SimpleNamespace(is_alive=lambda: True)
                yield reader
        try:
            with patch('incremental_backups.regular_reader', start_backup_after_first):
                deferred = self.retention()
            self.assertEqual(len(opened), 1)
            self.assertEqual(deferred['cacheStatus'], 'not-yet-available')
            self.assertTrue(deferred['refreshDeferred'])
            self.assertNotIn('error', deferred)
        finally: self.service.worker = None


class BackupTraversalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.engine = Engine(self.root / 'workspace')
        self.service = Backups(self.engine, executable='/unused-restic')
        self.addCleanup(self.service.close)
        self.source = self.engine.home / 'source/required'
        self.source.mkdir(parents=True)
        (self.source / 'work.txt').write_text('Required user contents')

    def snapshot(self, name='capture'):
        destination = self.root / name
        destination.mkdir()
        return destination

    def repository(self):
        incremental = IncrementalBackups(self.service)
        required = incremental.repository / 'data/ab'
        required.mkdir(parents=True)
        (incremental.repository / 'config').write_bytes(b'opaque config')
        (required / ('a' * 64)).write_bytes(b'opaque required encrypted pack')
        snapshot = 'b' * 64
        (incremental.repository / 'snapshots').mkdir()
        (incremental.repository / 'snapshots' / snapshot).write_bytes(b'opaque snapshot')
        return incremental, required, snapshot

    def test_unreadable_source_baseline_refuses_capture(self):
        destination = self.snapshot()
        with denied_scandir(self.source) as visits:
            with self.assertRaisesRegex(Problem, 'Cannot completely read backup directory'):
                self.service._workspace_listing()
            with self.assertRaisesRegex(Problem, 'Cannot completely read backup directory'):
                self.service._copy_workspace(destination, capacity_check=lambda _: None)
        self.assertTrue(visits)
        self.assertFalse((destination / 'UNFORGE-RECOVERY.json').exists())
        self.assertEqual((self.source / 'work.txt').read_text(), 'Required user contents')

    def test_unreadable_source_during_copy_or_final_listing_refuses_completion(self):
        for failure_visit in (2, 3):
            with self.subTest(failure_visit=failure_visit):
                destination = self.snapshot('capture-' + str(failure_visit))
                with denied_scandir(self.source, after=failure_visit) as visits:
                    with self.assertRaisesRegex(Problem, 'Cannot completely read backup directory'):
                        self.service._copy_workspace(destination, capacity_check=lambda _: None)
                self.assertEqual(len(visits), failure_visit)
                self.assertFalse((destination / 'UNFORGE-RECOVERY.json').exists())

    def test_explicitly_excluded_unreadable_cache_still_is_not_visited(self):
        excluded = self.engine.home / 'source/node_modules'
        excluded.mkdir(); (excluded / 'rebuildable.txt').write_text('Ignored cache')
        destination = self.snapshot()
        with denied_scandir(excluded) as visits:
            manifest = self.service._copy_workspace(destination, capacity_check=lambda _: None)
        self.assertEqual(visits, [])
        self.assertIn('source/node_modules', {entry['path'] for entry in manifest['excluded']})
        self.assertFalse((destination / 'source/node_modules').exists())
        self.assertEqual((destination / 'source/required/work.txt').read_text(), 'Required user contents')
        self.assertTrue(self.service.verify_workspace(destination))

    def test_encrypted_inventory_refuses_to_omit_required_pack_directory(self):
        incremental, required, snapshot = self.repository()
        valid = incremental._inventory(snapshot)
        self.assertIn('data/ab/' + 'a' * 64, {entry['path'] for entry in valid})
        cache = incremental.root / 'object-hashes.json'
        original = cache.read_bytes()
        with denied_scandir(required):
            with self.assertRaisesRegex(Problem, 'Cannot completely read backup directory'):
                incremental._inventory(snapshot)
        self.assertEqual(cache.read_bytes(), original)

    def test_failed_retention_walk_never_marks_partial_totals_current(self):
        _, required, _ = self.repository()
        previous = self.service.state()['retentionReview']
        self.assertEqual(previous['cacheStatus'], 'current')
        (required / ('c' * 64)).write_bytes(b'New required pack after earlier inventory')
        self.service._record({'id':'a' * 32, 'state':'completed'})
        with denied_scandir(required):
            failed = self.service.state()['retentionReview']
            cached_failure = self.service.state()['retentionReview']
        self.assertEqual(failed['cacheStatus'], 'stale')
        self.assertEqual(cached_failure['cacheStatus'], 'stale')
        self.assertEqual(failed['checkedAt'], previous['checkedAt'])
        self.assertEqual(failed['privateRepositoryBytes'], previous['privateRepositoryBytes'])
        self.assertIn('error', failed)

    def test_restore_verification_cannot_hide_unlisted_contents_in_unreadable_subtree(self):
        for marker, verify in (('CONTENTS.json', Backups.verify_container),
                               ('UNFORGE-RECOVERY.json', Backups.verify_workspace)):
            with self.subTest(marker=marker):
                restored = self.snapshot(marker + '-fixture')
                (restored / marker).write_text(json.dumps({'schema':1, 'files':[], 'fileCount':0}))
                hidden = restored / 'unlisted'
                hidden.mkdir(); (hidden / 'unexpected.txt').write_text('Must not be silently accepted')
                with denied_scandir(hidden):
                    with self.assertRaisesRegex(Problem, 'Cannot completely read backup directory'):
                        verify(restored)

    def test_capacity_durability_and_rollover_refuse_incomplete_tree_reads(self):
        incremental, required, _ = self.repository()
        consumers = {
            'capacity':lambda: tree_storage(incremental.repository),
            'durability':lambda: sync_tree(incremental.repository),
            'entry-limit':lambda: incremental._entry_paths(incremental.repository),
            'rollover':incremental._rollover_needed,
        }
        for label, operation in consumers.items():
            with self.subTest(consumer=label), denied_scandir(required):
                with self.assertRaisesRegex(Problem, 'Cannot completely read backup directory'):
                    operation()


class BackupChildCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.engine = Engine(self.base / 'workspace')
        self.helper = self.base / 'fake-restic'
        self.service = Backups(self.engine, executable=str(self.helper))
        self.addCleanup(self.service.close)
        self.processes = []
        self.real_popen = subprocess.Popen

    def script(self, body):
        self.helper.write_text('#!/bin/sh\n' + body)
        self.helper.chmod(0o700)

    def launch(self, *args, **kwargs):
        process = self.real_popen(*args, **kwargs)
        self.processes.append(process)
        return process

    def assert_reaped(self):
        self.assertTrue(self.processes)
        for process in self.processes:
            self.assertIsNotNone(process.returncode, 'Owned child was discarded without waiting')
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None: self.assertTrue(stream.closed)
        self.assertEqual(self.service.children, set())

    @staticmethod
    def reject_stream(reader):
        reader.read()  # EOF means the fixture child exited before cancellation.
        raise Problem('Original invalid verification stream')

    def test_exited_stream_child_checks_owned_group_without_masking_original_error(self):
        self.script('printf invalid\nexit 3\n')
        with patch('backups.subprocess.Popen', side_effect=self.launch), patch('backups.os.killpg', side_effect=ProcessLookupError) as kill:
            with self.assertRaisesRegex(Problem, 'Original invalid verification stream'):
                self.service._stream_command(self.base, self.base / 'key', self.reject_stream, 'dump')
        kill.assert_called_once_with(self.processes[0].pid, signal.SIGKILL)
        self.assert_reaped()

    def test_stream_exit_between_poll_and_signal_preserves_original_error_and_reaps(self):
        self.script('printf invalid\nexit 3\n')
        def launch(*args, **kwargs):
            if args[0][0] != str(self.helper): return self.real_popen(*args, **kwargs)
            process = self.launch(*args, **kwargs)
            real_poll = process.poll
            pending = 2
            def poll():
                nonlocal pending
                if pending:
                    pending -= 1
                    return None  # The group exits before its leader becomes waitable.
                return real_poll()
            process.poll = poll
            process.wait = Mock(wraps=process.wait)
            return process
        with patch('backups.subprocess.Popen', side_effect=launch), patch('backups.os.killpg', side_effect=PermissionError(errno.EPERM, 'Exited group')) as kill:
            with self.assertRaisesRegex(Problem, 'Original invalid verification stream'):
                self.service._stream_command(self.base, self.base / 'key', self.reject_stream, 'dump')
        self.assertEqual(kill.call_count, 2)
        kill.assert_called_with(self.processes[0].pid, signal.SIGKILL)
        self.processes[0].wait.assert_called()
        self.assert_reaped()

    def test_stream_reader_start_failure_stops_and_reaps_owned_child(self):
        self.script('exec sleep 60\n')
        with patch('backups.subprocess.Popen', side_effect=self.launch), patch('backups.threading.Thread.start', side_effect=RuntimeError('Cannot start stream reader')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot start stream reader'):
                self.service._stream_command(self.base, self.base / 'key', lambda reader: reader.read(), 'dump')
        self.assert_reaped()

    def test_stream_deadline_start_failure_stops_child_and_joins_started_reader(self):
        self.script('exec sleep 60\n')
        with patch('backups.subprocess.Popen', side_effect=self.launch), patch('backups.threading.Timer.start', side_effect=RuntimeError('Cannot start stream deadline')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot start stream deadline'):
                self.service._stream_command(self.base, self.base / 'key', lambda reader: reader.read(), 'dump')
        self.assert_reaped()

    def test_stream_deadline_stops_and_reaps_owned_child(self):
        self.script('exec sleep 60\n')
        timer = threading.Timer
        with patch('backups.subprocess.Popen', side_effect=self.launch), patch('backups.threading.Timer', side_effect=lambda _, callback: timer(0.02, callback)):
            with self.assertRaisesRegex(Problem, 'Encrypted verification timed out'):
                self.service._stream_command(self.base, self.base / 'key', lambda reader: reader.read(), 'dump')
        self.assert_reaped()

    def test_command_exception_stops_and_reaps_owned_child(self):
        self.script('exec sleep 60\n')
        def launch(*args, **kwargs):
            process = self.launch(*args, **kwargs)
            process.communicate = Mock(side_effect=subprocess.TimeoutExpired('fixture', 1800))
            return process
        with patch('backups.subprocess.Popen', side_effect=launch):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.service._command(self.base, self.base / 'key', 'backup')
        self.assert_reaped()

    def test_unstoppable_owned_child_remains_tracked_for_later_shutdown(self):
        # No real process is launched or signalled in this denial fixture.
        process = Mock(pid=2**31 - 1, returncode=None, stdout=io.BytesIO(), stderr=io.BytesIO())
        process.poll.return_value = None
        process.communicate.side_effect = RuntimeError('Fixture command interrupted')
        process.wait.side_effect = subprocess.TimeoutExpired('fixture', 10)
        try:
            with patch('backups.subprocess.Popen', return_value=process), patch('backups.os.killpg', side_effect=PermissionError(errno.EPERM, 'Still live owned child')):
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.service._command(self.base, self.base / 'key', 'backup')
            self.assertIn(process, self.service.children)
            process.wait.assert_called_once_with(timeout=10)
            self.assertTrue(process.stdout.closed); self.assertTrue(process.stderr.closed)
        finally:
            self.service.children.discard(process)

    def test_exited_leader_with_denied_live_group_remains_tracked(self):
        process = Mock(pid=2**31 - 1, returncode=0, stdout=io.BytesIO(), stderr=io.BytesIO())
        process.poll.return_value = 0
        process.wait.return_value = 0
        process.communicate.side_effect = RuntimeError('Fixture command interrupted')
        inspection = SimpleNamespace(returncode=0, stderr='', stdout='S\n')
        try:
            with patch('backups.subprocess.Popen', return_value=process), patch('backups.os.killpg', side_effect=PermissionError(errno.EPERM, 'Live denied group')), patch('backups.subprocess.run', return_value=inspection) as inspect:
                with self.assertRaises(PermissionError):
                    self.service._command(self.base, self.base / 'key', 'backup')
            self.assertIn(process, self.service.children)
            self.assertFalse(process._unforge_group_stopped)
            self.assertEqual(inspect.call_count, 5)
            process.wait.assert_called_once_with(timeout=10)
            # The worker is gone, but ownership must still block new work.
            self.assertIsNone(self.service.worker)
            operations = {
                'backup':self.service.start,
                'configure':lambda:self.service.configure([]),
                'standalone-restore':lambda:self.service.restore('/unused.ufbackup', 'key', '/unused-target'),
                'incremental-restore':lambda:self.service.restore('/unused.ufpoint', 'key', '/unused-target'),
                'cloud-status':lambda:self.service.refresh_cloud('unused'),
                'cloud-rehearsal':lambda:self.service.cloud_rehearse('unused', '/unused', 'key', '/unused-target'),
            }
            for name, operation in operations.items():
                with self.subTest(operation=name), self.assertRaisesRegex(Problem, 'current backup operation|already running'):
                    operation()
            with patch.object(IncrementalBackups, 'retention_review') as scan:
                review = self.service.state()['retentionReview']
            self.assertTrue(review['refreshDeferred'])
            self.assertEqual(review['cacheStatus'], 'not-yet-available')
            scan.assert_not_called()
        finally:
            with patch('backups.os.killpg', side_effect=ProcessLookupError): self.service.close()
        self.assertEqual(self.service.children, set())

    def test_exited_leader_descendant_holding_stderr_is_stopped_and_pipe_closed(self):
        self.script('sleep 60 >/dev/null &\nprintf "%s\\n" "$!"\nexit 0\n')
        descendant = []
        def reject(reader):
            child_pid = int(reader.read())
            descendant.append(child_pid)
            self.assertEqual(os.getpgid(child_pid), self.processes[0].pid)
            raise Problem('Original invalid verification stream')
        with patch('backups.subprocess.Popen', side_effect=self.launch):
            with self.assertRaisesRegex(Problem, 'Original invalid verification stream'):
                self.service._stream_command(self.base, self.base / 'key', reject, 'dump')
        self.assert_reaped()
        self.assertEqual(len(descendant), 1)
        inspection = subprocess.run(['ps', '-o', 'stat=', '-p', str(descendant[0])], capture_output=True, text=True, timeout=1)
        self.assertIn(inspection.returncode, (0, 1))
        self.assertEqual(inspection.stderr, '')
        self.assertTrue(all(state.startswith('Z') for state in inspection.stdout.split()), 'Owned descendant survived cleanup')

    def test_denied_descendant_cleanup_returns_without_closing_locked_reader(self):
        self.script('sleep 60 >/dev/null &\nprintf "%s\\n" "$!"\nexit 0\n')
        readers = []
        join = threading.Thread.join
        def defer_join(reader, timeout=None):
            readers.append(reader)
        try:
            with patch('backups.subprocess.Popen', side_effect=self.launch), patch('backups.os.killpg', side_effect=PermissionError(errno.EPERM, 'Owned descendant cancellation denied')), patch('backups.threading.Thread.join', autospec=True, side_effect=defer_join):
                with self.assertRaises(PermissionError):
                    self.service._stream_command(self.base, self.base / 'key', self.reject_stream, 'dump')
            process = self.processes[0]
            self.assertEqual(process.returncode, 0)
            self.assertIn(process, self.service.children)
            self.assertFalse(process._unforge_group_stopped)
            self.assertTrue(readers[0].is_alive())
            self.assertFalse(process.stderr.closed, 'Closing a live reader would block behind the descendant')
        finally:
            # Only this fixture's verified owned group is stopped after lifting
            # the simulated denial. The reader closes its own deferred pipe.
            self.service.close()
            for reader in readers: join(reader, timeout=2)
        self.assertTrue(all(not reader.is_alive() for reader in readers))
        self.assert_reaped()

    def test_completed_cloud_helper_group_exit_does_not_invalidate_its_result(self):
        self.script('printf \'{"state":"cloud_uploaded"}\'\n')
        with patch('backups.subprocess.Popen', side_effect=self.launch), patch('backups.os.killpg', side_effect=PermissionError(errno.EPERM, 'Exited group')) as kill:
            result = self.service._cloud_report(self.helper, self.base)
        self.assertEqual(result, {'state':'cloud_uploaded'})
        kill.assert_called_once_with(self.processes[0].pid, signal.SIGKILL)
        self.assert_reaped()

    def test_close_tolerates_owned_child_exit_during_term_and_reaps(self):
        self.script('exit 0\n')
        process = self.launch([str(self.helper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        process.stdout.read()  # Child has exited; its status is deliberately unreaped.
        self.service.children.add(process)
        real_poll = process.poll
        first = True
        def poll():
            nonlocal first
            if first:
                first = False
                return None
            return real_poll()
        process.poll = poll
        with patch('backups.os.killpg', side_effect=PermissionError(errno.EPERM, 'Exited group')) as kill:
            self.service.close()
        kill.assert_called_once_with(process.pid, signal.SIGTERM)
        process.stdout.close(); process.stderr.close()
        self.assert_reaped()


if __name__ == '__main__': unittest.main()
