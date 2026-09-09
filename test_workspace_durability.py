import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from engine import Engine, Problem

class WorkspaceDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.engine=Engine(Path(self.tmp.name)/'workspace')

    def test_failed_creation_does_not_publish_partial_project_or_remove_existing_work(self):
        owned=self.engine.create('Keep this');before=self.engine.detail(owned['id'])['history'][0]['id']
        run=self.engine.git
        def fail_commit(root,*args,**kwargs):
            if args[0]=='commit':raise Problem('Simulated storage failure')
            return run(root,*args,**kwargs)
        with patch.object(self.engine,'git',side_effect=fail_commit):
            with self.assertRaisesRegex(Problem,'storage failure'):self.engine.create('Interrupted starter')
        self.assertEqual([x['id'] for x in self.engine.projects()],[owned['id']])
        self.assertEqual(self.engine.detail(owned['id'])['history'][0]['id'],before)
        self.assertFalse(list(self.engine.home.glob('.creating-*')))

    def test_one_unborn_or_corrupt_project_does_not_hide_healthy_projects(self):
        owned=self.engine.create('Still available')
        root=self.engine.home/('a'*32);(root/'.unforge').mkdir(parents=True)
        marker=root/'.unforge/project.json';marker.write_text(json.dumps({'name':'Unborn','description':''}))
        self.engine.git(root,'init','--template=','-b','main')
        (root/'unwritten.txt').write_text('Keep this work')
        for data in (marker.read_text(),'{damaged','x' * (128*1024+1),'['*1200+'0'+']'*1200):
            marker.write_text(data)
            inventory=self.engine.project_inventory()
            self.assertEqual([p['id'] for p in inventory['projects']],[owned['id']])
            self.assertEqual(inventory['problems'][0]['id'],'a'*32)
            self.assertEqual((root/'unwritten.txt').read_text(),'Keep this work')
        self.assertTrue(marker.exists())

    def test_new_project_is_only_listed_after_commit_and_promotion(self):
        run=self.engine.git;seen=[]
        def inspect(root,*args,**kwargs):
            if root.name.startswith('.creating-'): seen.extend(self.engine.projects())
            return run(root,*args,**kwargs)
        with patch.object(self.engine,'git',side_effect=inspect):project=self.engine.create('Atomic starter')
        self.assertFalse(seen)
        self.assertEqual(self.engine.projects()[0]['id'],project['id'])
