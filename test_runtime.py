"""Real disposable process tests; no provider calls or production project access."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engine import Engine, Problem
from runtime import ACTIVE, MAX_LOG, RuntimeService, validate


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unforge-runtime-test-')
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(self.temp.name)
        self.runtime = RuntimeService(self.engine)
        self.addCleanup(self.runtime.close)

    def project(self, profile='python', *, run=None, check=None, prepare=None, files=None, **settings):
        project = self.engine.create('Runtime fixture')
        pid = project['id']
        for path, content in (files or {}).items():
            self.engine.edit(pid, path, content)
        config = dict(schemaVersion=1, profile=profile)
        if profile != 'static':
            config['run'] = run or ['python3', '-c', 'import time; time.sleep(20)']
        if check:
            config['check'] = check
        if prepare:
            config['prepare'] = prepare
        config.update(settings)
        self.runtime.configure(pid, config, None)
        self.engine.save(pid, 'Save runtime fixture')
        return pid

    def wait(self, run_id, statuses, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.runtime.get_run(run_id)
            if run['status'] in statuses:
                return run
            time.sleep(.04)
        self.fail(f'Runtime did not reach {statuses}: {self.runtime.get_run(run_id)}')

    def test_static_server_uses_candidate_and_stops(self):
        pid = self.project('static')
        started = self.runtime.start(pid, trusted=True)
        ready = self.wait(started['id'], {'running'})
        for _ in range(50):
            try:
                with urlopen(ready['url']) as response:
                    body = response.read()
                break
            except OSError:
                time.sleep(.04)
        else:
            self.fail('No static response')
        self.assertIn(b'Runtime fixture', body)
        self.assertNotEqual(Path(ready['candidatePath']), self.engine.root(pid))
        with self.assertRaises(HTTPError):
            urlopen(ready['url'] + '/.unforge/project.json')
        self.runtime.stop(pid, started['id'])
        self.wait(started['id'], {'stopped'})
        self.assertEqual(self.engine.git(self.engine.root(pid), 'status', '--porcelain'), b'')

    def test_requires_saved_explicit_trust_and_optimistic_config(self):
        pid = self.project('static')
        with self.assertRaisesRegex(Problem, 'trusted'):
            self.runtime.start(pid)
        with self.assertRaisesRegex(Problem, 'changed'):
            self.runtime.configure(pid, {'schemaVersion': 1, 'profile': 'static'}, 'stale')
        self.engine.edit(pid, 'new.txt', 'unsaved')
        with self.assertRaisesRegex(Problem, 'Save the project'):
            self.runtime.start(pid, trusted=True)
        self.assertEqual(self.runtime.overview()['runs'], [])

    def test_check_exit_status_logs_and_environment_are_real(self):
        code = 'import os; print("x"*160000); print("secret="+str(os.getenv("UNFORGE_TEST_PRODUCTION_SECRET"))); print("private="+str(os.getenv("HOME"))); raise SystemExit(7)'
        pid = self.project(check=['python3', '-c', code])
        old = os.environ.get('UNFORGE_TEST_PRODUCTION_SECRET')
        os.environ['UNFORGE_TEST_PRODUCTION_SECRET'] = 'should-never-reach-runtime'
        try:
            result = self.wait(self.runtime.check(pid, trusted=True)['id'], {'failed'})
        finally:
            if old is None:
                os.environ.pop('UNFORGE_TEST_PRODUCTION_SECRET', None)
            else:
                os.environ['UNFORGE_TEST_PRODUCTION_SECRET'] = old
        self.assertEqual(result['exitCode'], 7)
        self.assertTrue(result['outputTruncated'])
        self.assertLessEqual(len(result['output'].encode()), MAX_LOG)
        self.assertNotIn('should-never-reach-runtime', result['output'])
        pid2 = self.project(check=['python3', '-c', 'import os; print(os.getenv("UNFORGE_TEST_PRODUCTION_SECRET")); print(os.environ["HOME"]); print(os.environ["UNFORGE_DATA_DIR"])'])
        result2 = self.wait(self.runtime.check(pid2, trusted=True)['id'], {'passed'})
        self.assertIn('None\n', result2['output'])
        self.assertIn('/.runtime/', result2['output'])

    def test_check_scheduler_serializes_projects_and_releases(self):
        first = self.project(check=['python3', '-c', 'import time; print("ready",flush=True);time.sleep(20)'])
        second = self.project(check=['python3', '-c', 'print("done")'])
        one = self.runtime.check(first, trusted=True)
        self.wait(one['id'], {'running'})
        two = self.wait(self.runtime.check(second, trusted=True)['id'], {'failed'})
        self.assertIn('Another local check', two['error'])
        self.runtime.stop(first)
        self.wait(one['id'], {'stopped'})
        three = self.wait(self.runtime.check(second, trusted=True)['id'], {'passed'})
        self.assertEqual(three['exitCode'], 0)

    def test_timeout_kills_spawned_group_and_keeps_original(self):
        script = 'import pathlib,subprocess,sys,time\np=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\npathlib.Path("child.pid").write_text(str(p.pid))\nprint("child",p.pid,flush=True)\ntime.sleep(60)\n'
        pid = self.project(check=['python3', 'check.py'], files={'check.py': script}, checkTimeoutSeconds=1)
        result = self.wait(self.runtime.check(pid, trusted=True)['id'], {'timed-out'})
        child = int((Path(result['candidatePath']) / 'child.pid').read_text())
        for _ in range(50):
            status = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True).stdout.strip()
            if not status or status.startswith('Z'):
                break
            time.sleep(.05)
        else:
            self.fail('Runtime child remains alive')
        self.assertFalse((self.engine.root(pid) / 'child.pid').exists())

    def test_receipts_survive_restart_and_closed_parent_stops_worker(self):
        pid = self.project('static')
        one = self.runtime.start(pid, trusted=True)
        self.wait(one['id'], {'running'})
        process = self.runtime._workers[one['id']]
        process.stdin.close()
        result = self.wait(one['id'], {'interrupted'})
        process.wait(timeout=8)
        self.runtime.close()
        new = RuntimeService(self.engine)
        try:
            self.assertEqual(new.get_run(one['id'])['status'], 'interrupted')
            self.assertFalse(new.overview()['active'])
        finally:
            new.close()

    def test_preparation_and_checks_share_same_candidate(self):
        pid = self.project(prepare=['python3', '-c', 'from pathlib import Path;Path("prepared.txt").write_text("ready")'],
                           check=['python3', '-c', 'from pathlib import Path;assert Path("prepared.txt").read_text()=="ready";print("verified")'])
        result = self.wait(self.runtime.check(pid, trusted=True)['id'], {'passed', 'failed'})
        self.assertEqual(result['status'], 'passed', result)
        self.assertIn('verified', result['output'])
        self.assertFalse((self.engine.root(pid) / 'prepared.txt').exists())

    def test_invalid_profile_commands_are_rejected(self):
        for config in [dict(schemaVersion=1, profile='python', run=['sh', '-c', 'echo hi']),
                       dict(schemaVersion=1, profile='swift', run=['xcrun', 'sh']),
                       dict(schemaVersion=1, profile='static', directory='../private')]:
            with self.assertRaises(Problem):
                validate(config)

    def test_python_http_runtime_reports_observed_health(self):
        pid = self.project(run=['python3', '-m', 'http.server', '{port}', '--bind', '127.0.0.1'])
        attempt = self.runtime.start(pid, trusted=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.runtime.get_run(attempt['id'])
            if result['health'] == 'responding':
                break
            time.sleep(.05)
        else:
            self.fail(f'No observed health: {result}')
        self.assertEqual(result['status'], 'running')
        with urlopen(result['url']) as response:
            self.assertIn(b'Runtime fixture', response.read())
        self.runtime.stop(pid)
        self.wait(attempt['id'], {'stopped'})

    def test_lost_parent_stops_python_process_group(self):
        script = 'import pathlib,subprocess,sys,time\np=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\npathlib.Path("child.pid").write_text(str(p.pid))\ntime.sleep(60)\n'
        pid = self.project(run=['python3', 'app.py'], files={'app.py': script})
        attempt = self.runtime.start(pid, trusted=True)
        child_file = Path(attempt['candidatePath']) / 'child.pid'
        deadline = time.monotonic() + 5
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(.04)
        child = int(child_file.read_text())
        process = self.runtime._workers[attempt['id']]
        process.stdin.close()
        self.wait(attempt['id'], {'interrupted'})
        process.wait(timeout=5)
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not state or state.startswith('Z'), state)

    def test_removing_attempt_preserves_original_and_rejects_active(self):
        pid = self.project('static')
        attempt = self.runtime.start(pid, trusted=True)
        self.wait(attempt['id'], {'running'})
        with self.assertRaisesRegex(Problem, 'Stop'):
            self.runtime.remove(pid, attempt['id'])
        self.runtime.stop(pid)
        self.wait(attempt['id'], {'stopped'})
        self.runtime._workers[attempt['id']].wait(timeout=5)
        self.runtime.remove(pid, attempt['id'])
        self.assertTrue((self.engine.root(pid) / 'index.html').is_file())
        self.assertFalse(Path(attempt['candidatePath']).exists())
        self.assertEqual(self.runtime.overview()['runs'], [])

    def test_suggestions_choose_framework_specific_flags_without_execution(self):
        for script, flag in [('vite', '--host'), ('next dev', '--hostname'), ('node server.js', None)]:
            pid = self.engine.create('Suggestion fixture')['id']
            self.engine.edit(pid, 'package.json', json.dumps({'scripts': {'dev': script, 'check': 'node check.js'}}))
            self.engine.edit(pid, 'package-lock.json', '{}')
            suggested = self.runtime.get(pid)
            command = suggested['suggestedConfig']['run']
            if flag:
                self.assertIn(flag, command)
                self.assertIn('{port}', command)
            else:
                self.assertEqual(command, ['npm', 'run', 'dev'])
            self.assertIn('--ignore-scripts', suggested['suggestedConfig']['prepare'])
            self.assertIsNone(suggested['config'])
            self.assertEqual(suggested['runs'], [])

    def test_ambiguous_python_suggestion_declines_guess(self):
        pid = self.engine.create('Two entrypoints')['id']
        self.engine.edit(pid, 'app.py', 'print("app")')
        self.engine.edit(pid, 'main.py', 'print("main")')
        suggestion = self.runtime.suggested_config(pid)
        self.assertIsNone(suggestion['config'])
        self.assertIn('Both', suggestion['reason'])

    def test_persistent_data_survives_stop_new_version_and_attempt_removal(self):
        script = '''import os, sqlite3
from pathlib import Path
data = Path(os.environ['UNFORGE_DATA_DIR'])
store = Path(os.environ['APP_LOCAL_STORE'])
db = sqlite3.connect(store)
db.execute('create table if not exists visits (n integer)')
db.execute('insert into visits values (1)')
db.commit()
print('count', db.execute('select count(*) from visits').fetchone()[0], flush=True)
db.close()
path = Path('data/note.txt')
path.write_text(path.read_text() + 'x' if path.exists() else 'x')
print('ready', flush=True)
import time
time.sleep(60)
'''
        pid = self.project(run=['python3', 'app.py'], files={'app.py': script},
                           dataPaths=['data'], dataEnvironment={'APP_LOCAL_STORE': 'app.sqlite'})
        receipts = []
        for expected in (1, 2):
            attempt = self.runtime.start(pid, trusted=True, persistent=True)
            receipts.append(attempt)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = self.runtime.get_run(attempt['id'])
                if 'ready' in result['output']:
                    break
                time.sleep(.04)
            self.assertIn(f'count {expected}', result['output'])
            self.assertTrue(result['persistent'])
            self.runtime.stop(pid)
            self.wait(attempt['id'], {'stopped'})
            self.runtime._workers[attempt['id']].wait(timeout=5)
            self.runtime.remove(pid, attempt['id'])
            self.assertEqual((Path(attempt['dataPath']) / 'data/note.txt').read_text(), 'x' * expected)
            if expected == 1:
                self.engine.edit(pid, 'app.py', script + '\n# A new source version\n')
                self.engine.save(pid, 'Update source without moving local data')
                self.runtime.close()
                self.runtime = RuntimeService(self.engine)
                self.addCleanup(self.runtime.close)
        self.assertEqual(receipts[0]['dataPath'], receipts[1]['dataPath'])
        self.assertNotEqual(receipts[0]['sourceHead'], receipts[1]['sourceHead'])
        self.assertFalse((self.engine.root(pid) / 'data').exists())
        self.assertFalse((self.engine.root(pid) / 'app.sqlite').exists())
        preview = self.runtime.start(pid, trusted=True)
        self.assertFalse(preview['persistent'])
        self.assertNotEqual(preview['dataPath'], receipts[0]['dataPath'])
        self.runtime.stop(pid)

    def test_persistent_mounts_reject_source_collisions_and_outside_paths(self):
        pid = self.project(files={'data/source.txt': 'important'}, dataPaths=['data'])
        with self.assertRaisesRegex(Problem, 'collides with saved source'):
            self.runtime.start(pid, trusted=True, persistent=True)
        self.assertEqual((self.engine.root(pid) / 'data/source.txt').read_text(), 'important')
        for values in [{'dataPaths': ['../outside']}, {'dataPaths': ['data', 'Data/cache']},
                       {'dataEnvironment': {'HOME': '.'}}, {'dataEnvironment': {'DYLD_LIBRARY_PATH': '.'}},
                       {'dataEnvironment': {'APP_STORE': '/outside'}}, {'dataEnvironment': {'APP_STORE': '../outside'}}]:
            with self.assertRaises(Problem):
                validate(dict(schemaVersion=1, profile='static', **values))
        pid2 = self.project(dataPaths=['data'])
        storage = self.engine.home / '.app-data' / pid2
        storage.mkdir(parents=True)
        (storage / 'data').symlink_to(self.engine.root(pid))
        with self.assertRaisesRegex(Problem, 'symbolic links'):
            self.runtime.start(pid2, trusted=True, persistent=True)

    def test_checks_always_have_disposable_data(self):
        pid = self.project(check=['python3', '-c', 'import os; from pathlib import Path; Path(os.environ["UNFORGE_DATA_DIR"],"scratch.txt").write_text("check")'],
                           dataEnvironment={'APP_DIR': '.'})
        result = self.wait(self.runtime.check(pid, trusted=True)['id'], {'passed'})
        self.assertFalse(result['persistent'])
        self.assertTrue((Path(result['dataPath']) / 'scratch.txt').is_file())
        self.assertFalse((self.engine.home / '.app-data' / pid).exists())

    def test_runtime_settings_replace_is_atomic_and_flushes_directory(self):
        pid = self.project('static')
        current = self.runtime.get(pid)
        path = self.engine.root(pid) / '.unforge/runtime.json'
        original = path.read_bytes()
        updated = {**current['config'], 'runTimeoutSeconds': 123}
        with patch('runtime.os.replace', side_effect=OSError('simulated interrupted rename')):
            with self.assertRaisesRegex(OSError, 'interrupted rename'):
                self.runtime.configure(pid, updated, current['revision'])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob('runtime.json.*.tmp')), [])
        import stat
        modes = []
        original_sync = os.fsync
        def observed_sync(fd):
            modes.append(os.fstat(fd).st_mode)
            original_sync(fd)
        with patch('runtime.os.fsync', side_effect=observed_sync):
            result = self.runtime.configure(pid, updated, current['revision'])
        self.assertEqual(result['config']['runTimeoutSeconds'], 123)
        self.assertTrue(any(stat.S_ISREG(mode) for mode in modes))
        self.assertTrue(any(stat.S_ISDIR(mode) for mode in modes))

    def test_persistent_data_lease_survives_watchdog_crash(self):
        pid = self.project(run=['python3', '-c', 'import time; print("ready",flush=True);time.sleep(60)'])
        first = self.runtime.start(pid, trusted=True, persistent=True)
        child = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                record = self.runtime.get_run(first['id'])
                if 'ready' in record['output']:
                    child = record['processPid']
                    break
                time.sleep(.04)
            self.assertIsNotNone(child)
            worker = self.runtime._workers[first['id']]
            worker.kill()
            worker.wait(timeout=5)
            self.runtime.overview()  # Recover the interrupted receipt, without signaling a disk PID.
            self.assertEqual(self.runtime.get_run(first['id'])['status'], 'interrupted')
            second = self.runtime.start(pid, trusted=True, persistent=True)
            failed = self.wait(second['id'], {'failed'})
            self.assertIn('still in use', failed['error'])
            self.assertIsNone(failed.get('processPid'))
        finally:
            if child is not None:
                # This test deliberately kills the watchdog; reap only its proven fixture process group.
                try:
                    os.killpg(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for _ in range(50):
                    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True).stdout.strip()
                    if not state or state.startswith('Z'):
                        break
                    time.sleep(.04)


if __name__ == '__main__':
    unittest.main()
