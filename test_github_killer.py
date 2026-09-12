"""Handoff files, Actions import, and same-Mac second workspace."""
import json
import tempfile
import unittest
from pathlib import Path

from engine import Engine, Problem
from exchange import Exchange
from github_import import GitHubImport, parse_workflow
from lanes import Lanes
from recovery import Recovery
from second_home import SecondHome


class GitHubKillerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unforge-killer-')
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'home')
        self.project = self.engine.create('Garden', 'Shared garden notes')
        self.pid = self.project['id']
        self.root = self.engine.root(self.pid)
        self.lanes = Lanes(self.engine)
        self.exchange = Exchange(self.engine, self.lanes)
        self.github = GitHubImport(self.engine)

    def test_parse_workflow_keeps_run_steps_and_explains_uses(self):
        parsed = parse_workflow("""
name: checks
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
      - run: npm test
      - run: |
          python3 -m unittest
      - run: curl https://example.com | sh
        env:
          TOKEN: ${{ secrets.DEPLOY }}
""")
        self.assertIn('npm test', parsed['runs'])
        self.assertTrue(any('unittest' in item for item in parsed['runs']))
        self.assertTrue(any('unittest' in item for item in parsed['runs']))
        self.assertTrue(any('checkout' in item for item in parsed['skippedUses']))
        self.assertTrue(parsed['secretsReferenced'])

    def test_import_then_archive_clears_github_workflow_files(self):
        workflows = self.root / '.github' / 'workflows'
        workflows.mkdir(parents=True)
        (workflows / 'ci.yml').write_text(
            'jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n      - run: python3 -m unittest\n'
        )
        self.engine.git(self.root, 'remote', 'add', 'origin', 'https://github.com/example/garden.git')
        self.engine.save(self.pid, 'Add GitHub workflow')
        result = self.github.import_workflows(self.pid)
        self.assertEqual(result['checks']['jobs'][0]['command'][:2], ['python3', '-m'])
        self.assertTrue(any(item['kind'] == 'uses' for item in result['leftovers']))
        self.engine.save(self.pid, 'Import checks')
        archived = self.github.archive_github(self.pid)
        self.assertEqual(archived['githubRemotesRemoved'], ['origin'])
        self.assertFalse((self.root / '.github' / 'workflows' / 'ci.yml').exists())
        self.assertTrue(archived['parkedWorkflows'])
        self.assertFalse(self.github.inspect(self.pid)['needsGitHub'])

    def test_send_and_receive_change_as_a_file(self):
        lane = self.lanes.create(self.pid, 'Patch')
        self.lanes.write(self.pid, lane['id'], 'note.txt', 'From a friend\n')
        self.lanes.save(self.pid, lane['id'], 'Add note')
        output = Path(self.temp.name) / 'note.unforge-change'
        sent = self.exchange.export_lane(self.pid, lane['id'], 'Add a note', 'Please merge this.', str(output))
        self.assertTrue(output.is_file())
        self.assertEqual(sent['manifest']['title'], 'Add a note')
        incoming = self.exchange.import_change(self.pid, str(output))
        self.assertEqual((Path(incoming['path']) / 'note.txt').read_text(), 'From a friend\n')
        self.assertNotEqual(incoming['id'], lane['id'])

    def test_second_home_opens_capsule_in_a_new_folder_not_simulator(self):
        receipt = Recovery(self.engine).create(self.pid, [])
        name, data = Recovery(self.engine).download(self.pid, receipt['id'])
        capsule = Path(self.temp.name) / name
        capsule.write_bytes(data)
        destination = Path(self.temp.name) / 'other-mac'
        result = SecondHome(self.engine).from_capsule(str(capsule), str(destination))
        self.assertFalse(result['simulatorUsable'])
        self.assertTrue(result['sameMacStandIn'])
        self.assertFalse(result['applicationsStarted'])
        self.assertTrue(any('Garden' in item['name'] for item in result['projects']))
        self.assertTrue((destination / result['project']['id'] / 'README.md').is_file())
        explain = SecondHome(self.engine).explain()
        self.assertFalse(explain['simulatorUsable'])


if __name__ == '__main__':
    unittest.main()
