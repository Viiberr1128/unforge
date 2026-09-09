import copy
import json
from pathlib import Path
import tempfile
import unittest

from care import CARE_PATH, Care, blank, consequences, proposed_files, validate
from engine import Engine, Problem


def resource(**changes):
    value = {'id': 'host', 'name': 'Application host', 'provider': 'Fly.io', 'kind': 'compute',
             'purpose': 'Public web access', 'sharedWith': [], 'monthlyLow': 0, 'monthlyHigh': 10,
             'currency': 'USD', 'costSource': 'User planning allowance, not an invoice',
             'costCheckedAt': '2026-09-09', 'status': 'retained',
             'evidence': 'Retained for access to archived customer records', 'sharedImpactReviewed': False}
    value.update(changes)
    return value


class CareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.engine = Engine(self.temporary.name)
        self.pid = self.engine.create('Care test')['id']
        self.root = self.engine.root(self.pid)
        self.care = Care(self.engine)

    def tearDown(self):
        self.temporary.cleanup()

    def saved_care(self, resources=None):
        state = self.care.get(self.pid)
        document = state['document']
        document['brief']['purpose'] = 'Help a beginner keep ownership of a personal tool.'
        document['brief']['keepWorking'] = ['Create a note, reopen it, and find the same text.']
        document['brief']['credentialPointers'] = ['Password manager / hosting account']
        if resources is not None:
            document['resources'] = resources
        result = self.care.save(self.pid, document, state['revision'])
        self.engine.save(self.pid, 'Record project care')
        return result

    def recovery(self, head=None):
        head = head or self.engine.git(self.root, 'rev-parse', 'HEAD').decode().strip()
        return {'capsules': [{'id': 'capsule1', 'sourceHead': head, 'sha256': 'a' * 64}],
                'rehearsals': [{'id': 'rehearsal1', 'capsuleId': 'capsule1', 'sourceHead': head,
                                'capsuleSha256': 'a' * 64, 'ok': True}]}

    def test_new_care_is_inert_and_first_save_does_not_commit(self):
        before = self.engine.detail(self.pid)
        state = self.care.get(self.pid)
        self.assertIsNone(state['revision'])
        self.assertFalse((self.root / CARE_PATH).exists())
        document = state['document']
        document['brief']['purpose'] = 'Personal notes'
        result = self.care.save(self.pid, document, None)
        self.assertEqual(len(result['revision']), 64)
        after = self.engine.detail(self.pid)
        self.assertTrue(after['dirty'])
        self.assertEqual(before['history'], after['history'])

    def test_optimistic_conflict_preserves_other_writer(self):
        first = self.saved_care()
        theirs = copy.deepcopy(first['document'])
        theirs['brief']['purpose'] = 'Changed in another window'
        self.care.save(self.pid, theirs, first['revision'])
        mine = copy.deepcopy(first['document'])
        mine['brief']['purpose'] = 'Old editor overwriting'
        with self.assertRaisesRegex(Problem, 'changed elsewhere'):
            self.care.save(self.pid, mine, first['revision'])
        self.assertEqual(self.care.get(self.pid)['document']['brief']['purpose'], 'Changed in another window')

    def test_new_document_respects_ignored_path(self):
        self.engine.edit(self.pid, '.gitignore', '.unforge/care.json\n')
        with self.assertRaisesRegex(Problem, 'ignore rule'):
            self.care.save(self.pid, blank(), None)
        self.assertFalse((self.root / CARE_PATH).exists())

    def test_malformed_file_fails_without_disclosing_raw_content(self):
        (self.root / CARE_PATH).write_text('{"password": "do-not-return", invalid')
        with self.assertRaises(Problem) as caught:
            self.care.get(self.pid)
        self.assertNotIn('do-not-return', str(caught.exception))
        self.assertIn('not valid', str(caught.exception))

    def test_reserved_symlink_is_not_followed(self):
        outside = Path(self.temporary.name) / 'outside.json'
        outside.write_text(json.dumps(blank()))
        (self.root / CARE_PATH).symlink_to(outside)
        with self.assertRaisesRegex(Problem, 'Symbolic'):
            self.care.get(self.pid)

    def test_secrets_rejected_on_write_and_read(self):
        document = blank()
        document['brief']['credentialPointers'] = ['api_key=abc123']
        with self.assertRaisesRegex(Problem, 'never a password'):
            self.care.save(self.pid, document, None)
        self.assertFalse((self.root / CARE_PATH).exists())
        (self.root / CARE_PATH).write_text(json.dumps(document))
        with self.assertRaisesRegex(Problem, 'never a password'):
            self.care.handoff(self.pid)

    def test_cost_validation_requires_source_and_ordered_finite_range(self):
        for changes in ({'monthlyLow': -1}, {'monthlyHigh': float('inf')}, {'monthlyLow': True},
                        {'monthlyLow': 11}, {'monthlyLow': None}, {'costSource': ''},
                        {'costCheckedAt': 'yesterday'}, {'currency': 'usd'}):
            document = blank()
            document['resources'] = [resource(**changes)]
            with self.subTest(changes=changes), self.assertRaises(Problem):
                self.care.save(self.pid, document, None)
        document['resources'] = [resource(monthlyLow=None, monthlyHigh=None, costSource='')]
        self.assertIsNone(self.care.save(self.pid, document, None)['document']['resources'][0]['monthlyLow'])

    def test_unknown_and_oversized_document_fields_are_rejected(self):
        for document in ({'brief': {'secretValue': 'value'}}, {'resources': [resource()] * 49},
                         {'schemaVersion': True}, {'schemaVersion': 2}, {'brief': {'purpose': 'x' * 2001}},
                         {'checks': 'not an array'}, {'resources': [resource(), resource()]}):
            with self.subTest(document=str(document)[:80]), self.assertRaises(Problem):
                validate(document)

    def test_handoff_and_simplify_include_intent_and_unknowns(self):
        state = self.saved_care([resource()])
        document = state['document']
        document['brief']['pendingDecisions'] = ['Decide whether the archive needs public access.']
        self.care.save(self.pid, document, state['revision'])
        handoff = self.care.handoff(self.pid)
        self.assertIn('Create a note, reopen it', handoff)
        self.assertIn('Decide whether the archive', handoff)
        self.assertIn('manually recorded ranges', handoff)
        self.assertIn('Credential values, ignored files', handoff)
        proposal = self.care.simplify_request(self.pid)
        self.assertIn('Create a note, reopen it', proposal['request'])
        self.assertIn('Do not deploy', proposal['request'])
        self.assertLessEqual(len(proposal['request']), 8000)

    def test_long_simplification_context_remains_bounded(self):
        document = blank()
        document['brief']['keepWorking'] = ['k' * 1000] * 24
        self.care.save(self.pid, document, None)
        request = self.care.simplify_request(self.pid)['request']
        self.assertLessEqual(len(request), 8000)
        self.assertIn('read .unforge/care.json', request)
        self.assertIn('Do not deploy', request)

    def test_recorded_behavior_results_require_evidence_and_survive_export(self):
        with self.assertRaisesRegex(Problem, 'evidence'):
            self.care.record_check(self.pid, 'Reopen a note', 'pass', '', None)
        result = self.care.record_check(self.pid, 'Reopen a note', 'pass', 'Manually reopened note A with identical text', None)
        self.assertEqual(result['document']['checks'][0]['outcome'], 'pass')
        self.assertIn('human reports', self.care.handoff(self.pid))
        self.engine.save(self.pid, 'Record manual check')
        bundle = Path(self.temporary.name) / 'portable.bundle'
        bundle.write_bytes(self.engine.export(self.pid))
        imported = self.engine.import_bundle(str(bundle))
        self.assertEqual(self.care.get(imported['id'])['document']['checks'], result['document']['checks'])

    def test_retirement_blocks_unsaved_unknown_and_unreviewed_shared_resources(self):
        state = self.saved_care([resource(status='active', sharedWith=['another-project'])])
        plan = self.care.prepare_retirement(self.pid)
        self.assertFalse(plan['canComplete'])
        self.assertTrue(any('another-project' in risk for risk in plan['risks']))
        with self.assertRaisesRegex(Problem, 'resolve recorded resources'):
            self.care.retire(self.pid, state['revision'], self.recovery(), 'capsule1')
        document = state['document']
        document['resources'][0]['status'] = 'stopped'
        state = self.care.save(self.pid, document, state['revision'])
        self.engine.save(self.pid, 'Record stopped resource')
        self.assertFalse(self.care.prepare_retirement(self.pid)['canComplete'])
        document['resources'][0]['sharedImpactReviewed'] = True
        state = self.care.save(self.pid, document, state['revision'])
        self.assertFalse(self.care.prepare_retirement(self.pid)['canComplete'])
        self.engine.save(self.pid, 'Record reviewed shared impact')
        self.assertTrue(self.care.prepare_retirement(self.pid)['canComplete'])

    def test_detected_services_cannot_be_skipped_and_provider_aliases_match(self):
        self.engine.edit(self.pid, 'fly.toml', 'min_machines_running = 1\n')
        self.engine.save(self.pid, 'Add hosting configuration')
        self.assertFalse(self.care.prepare_retirement(self.pid)['canComplete'])
        self.saved_care([resource()])
        self.assertTrue(self.care.prepare_retirement(self.pid)['canComplete'])

    def test_retirement_requires_verified_matching_rehearsal(self):
        state = self.saved_care([resource()])
        for mutation in ('old-head', 'wrong-hash', 'failed', 'missing'):
            recovery = self.recovery()
            if mutation == 'old-head':
                recovery['capsules'][0]['sourceHead'] = '0' * 40
            elif mutation == 'wrong-hash':
                recovery['rehearsals'][0]['capsuleSha256'] = 'b' * 64
            elif mutation == 'failed':
                recovery['rehearsals'][0]['ok'] = False
            else:
                recovery['rehearsals'] = []
            with self.subTest(mutation=mutation), self.assertRaises(Problem):
                self.care.retire(self.pid, state['revision'], recovery, 'capsule1')
        result = self.care.retire(self.pid, state['revision'], self.recovery(), 'capsule1', 'Keep host for the archive.')
        self.assertEqual(result['document']['retirement']['state'], 'complete')
        self.assertEqual(result['document']['retirement']['rehearsalId'], 'rehearsal1')
        self.assertTrue(self.engine.detail(self.pid)['dirty'])

    def test_form_cannot_forge_retirement_and_new_intent_invalidates_record(self):
        state = self.saved_care()
        forged = copy.deepcopy(state['document'])
        forged['retirement'] = {'state': 'complete'}
        with self.assertRaisesRegex(Problem, 'retirement action'):
            self.care.save(self.pid, forged, state['revision'])
        retired = self.care.retire(self.pid, state['revision'], self.recovery(), 'capsule1')
        document = retired['document']
        document['brief']['purpose'] = 'Project is useful again.'
        updated = self.care.save(self.pid, document, retired['revision'])
        self.assertEqual(updated['document']['retirement'], {'state': 'active'})


class ConsequenceTests(unittest.TestCase):
    def test_proposal_detects_real_added_dependency_without_returning_source_values(self):
        before = '{\n  "dependencies": {}\n}\n'
        patch = '''diff --git a/package.json b/package.json
index 111..222 100644
--- a/package.json
+++ b/package.json
@@ -1,3 +1,3 @@
 {
-  "dependencies": {}
+  "dependencies": {"openai": "^4", "left-pad": "^1"}, "privateToken": "do-not-return"
 }
'''
        report = consequences([{'path': 'package.json', 'content': before}], patch)
        self.assertEqual(report['dependencies']['added'], [{'name': 'left-pad', 'path': 'package.json'}, {'name': 'openai', 'path': 'package.json'}])
        self.assertEqual([s['id'] for s in report['newServices']], ['openai'])
        self.assertNotIn('do-not-return', str(report))
        self.assertIn('Costs remain unknown', ' '.join(report['limits']))

    def test_removed_dependency_and_added_schedule_have_distinct_consequences(self):
        files = [{'path': 'package.json', 'content': '{"dependencies":{"openai":"4"}}\n'}]
        patch = '''diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1 +1 @@
-{"dependencies":{"openai":"4"}}
+{"dependencies":{}}
diff --git a/.github/workflows/night.yml b/.github/workflows/night.yml
new file mode 100644
--- /dev/null
+++ b/.github/workflows/night.yml
@@ -0,0 +1,4 @@
+on:
+  schedule:
+    - cron: '0 * * * *'
+jobs: {}
'''
        report = consequences(files, patch)
        self.assertEqual(report['dependencies']['removed'], [{'name': 'openai', 'path': 'package.json'}])
        self.assertIn('scheduled-work', [c['id'] for c in report['chores']])
        self.assertEqual([s['id'] for s in report['services']], ['github-actions'])

    def test_stale_patch_does_not_invent_successful_comparison(self):
        files = [{'path': 'package.json', 'content': '{"changed":true}\n'}]
        patch = '''diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1 +1 @@
-{}
+{"dependencies":{"openai":"4"}}
'''
        report = consequences(files, patch)
        self.assertEqual(report['newServices'], [])
        self.assertTrue(any('did not match' in line for line in report['limits']))

    def test_malformed_proposal_is_not_reported_as_dependency_reduction(self):
        files = [{'path': 'package.json', 'content': '{"dependencies":{"openai":"4"}}\n'}]
        patch = '''diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1 +1 @@
-{"dependencies":{"openai":"4"}}
+{broken
'''
        report = consequences(files, patch)
        self.assertEqual(report['dependencies']['removed'], [])
        self.assertTrue(any('removals for that file are unknown' in line for line in report['limits']))
        nested = consequences([{'path': 'package.json', 'content': '[' * 2000 + '0' + ']' * 2000}])
        self.assertEqual(nested['services'], [])
        self.assertTrue(any('malformed' in line for line in nested['limits']))

    def test_no_newline_patch_and_deleted_file(self):
        files = [{'path': 'package.json', 'content': '{}'}]
        patch = r'''diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1 +1 @@
-{}
\ No newline at end of file
+{"dependencies":{"openai":"4"}}
\ No newline at end of file
'''
        after, limits = proposed_files(files, patch)
        self.assertEqual(limits, [])
        self.assertEqual(after[0]['content'], '{"dependencies":{"openai":"4"}}')
        deletion = r'''diff --git a/package.json b/package.json
--- a/package.json
+++ /dev/null
@@ -1 +0,0 @@
-{}
\ No newline at end of file
'''
        self.assertEqual(proposed_files(files, deletion)[0], [])

    def test_unsupported_and_unbounded_inputs_are_not_silently_trusted(self):
        report = consequences([], 'nonsense')
        self.assertTrue(any('not a supported Git text diff' in line for line in report['limits']))
        with self.assertRaises(Problem):
            consequences([], 'x' * (512 * 1024 + 1))
        patch = '''diff --git a/big/package.json b/big/package.json
--- a/big/package.json
+++ b/big/package.json
@@ -1 +1 @@
-{}
+{"dependencies":{"openai":"4"}}
'''
        report = consequences([], patch)
        self.assertTrue(any('outside the bounded baseline' in line for line in report['limits']))


if __name__ == '__main__':
    unittest.main()
