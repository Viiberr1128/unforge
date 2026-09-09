import unittest
from insights import analyze


class InsightTests(unittest.TestCase):
    def test_plain_prose_does_not_detect_services(self):
        result = analyze([{'path': 'README.md', 'content': 'Fly.io supabase openai Cloud Run run.googleapis.com'},
                          {'path': 'notes.txt', 'content': 'min_machines_running = 8'}])
        self.assertEqual(result['services'], [])

    def test_only_real_dependency_sections_count(self):
        text = '''{
  "description": "openai and firebase",
  "scripts": {"twilio": "echo hello"},
  "dependencies": {
    "@supabase/supabase-js": "^2",
    "openai": "^4"
  },
  "devDependencies": {"@aws-sdk/client-s3": "^3"}
}'''
        result = analyze([{'path': 'web/package.json', 'content': text}])
        by_id = {s['id']: s for s in result['services']}
        self.assertEqual(set(by_id), {'supabase', 'openai', 'aws'})
        self.assertEqual(by_id['supabase']['evidence'], [{'path': 'web/package.json', 'line': 5}])
        self.assertEqual(by_id['openai']['evidence'][0]['line'], 6)
        self.assertEqual(by_id['aws']['evidence'][0]['line'], 8)
        self.assertIn('does not establish active usage or charges', by_id['openai']['meaning'])

    def test_fly_minimum_has_precise_line_without_value_exposure(self):
        result = analyze([{'path': 'fly.toml', 'content': '[http_service]\nmin_machines_running = 2\nsecret = "do-not-echo"\n'}])
        by_id = {s['id']: s for s in result['services']}
        self.assertEqual(by_id['fly-minimum']['evidence'], [{'path': 'fly.toml', 'line': 2}])
        self.assertNotIn('do-not-echo', str(result))
        self.assertIn('no live state or cost', by_id['fly-minimum']['meaning'])
        self.assertNotIn('fly-minimum', {s['id'] for s in analyze([{'path': 'fly.toml', 'content': 'min_machines_running = 0'}])['services']})

    def test_workflow_counts_are_bounded_to_workflow_files(self):
        result = analyze([
            {'path': '.github/workflows/test.yml', 'content': 'on:\n  push:\n'},
            {'path': '.github/workflows/nightly.yaml', 'content': 'on:\n  schedule:\n    - cron: "0 0 * * *"'},
            {'path': '.github/workflows/README.md', 'content': 'schedule:'},
            {'path': 'docs/.github/workflows/example.yml', 'content': 'schedule:'},
        ])
        self.assertEqual(result['workflowCount'], 2)
        self.assertEqual(result['scheduledWorkflows'], 1)

    def test_exact_manifests_and_cloud_markers(self):
        result = analyze([
            {'path': 'not-fly.toml', 'content': ''},
            {'path': 'wrangler.notes', 'content': ''},
            {'path': 'wrangler.jsonc', 'content': '{}'},
            {'path': 'deploy/service.yaml', 'content': 'apiVersion: serving.knative.dev/v1\nkind: Service'},
            {'path': 'cloudbuild.yaml', 'content': 'steps: []'},
        ])
        by_id = {s['id']: s for s in result['services']}
        self.assertEqual(set(by_id), {'cloudflare', 'cloud-run', 'google-cloud'})
        self.assertEqual(by_id['cloud-run']['evidence'][0]['line'], 1)
        self.assertIn('Knative can run elsewhere', by_id['cloud-run']['meaning'])

    def test_invalid_and_large_inputs_are_reported(self):
        result = analyze([{'path': 'package.json', 'content': '{bad'},
                          {'path': 'fly.toml', 'content': 'a' * (256 * 1024 + 1)},
                          {'path': '../fly.toml', 'content': ''}])
        self.assertEqual(result['services'], [])
        self.assertTrue(any('invalid package.json' in x for x in result['limits']))
        self.assertTrue(any('oversized' in x for x in result['limits']))
        self.assertEqual(len(result['architectures']), 3)
        self.assertFalse(any('$' in a['cost'] for a in result['architectures']))


if __name__ == '__main__':
    unittest.main()
