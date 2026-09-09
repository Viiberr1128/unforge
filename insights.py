"""Offline, bounded dependency observations. References are not billing evidence."""
import json
import re

MAX_FILES = 500
MAX_CONTENT = 256 * 1024

PACKAGES = {
    '@supabase/supabase-js': ('supabase', 'Supabase'),
    'firebase': ('firebase', 'Firebase'),
    'openai': ('openai', 'OpenAI'),
    '@anthropic-ai/sdk': ('anthropic', 'Anthropic'),
    'stripe': ('stripe', 'Stripe'),
    'twilio': ('twilio', 'Twilio'),
}


def analyze(files):
    """Return descriptive observations without opening files or calling services."""
    found = {}
    limits = [
        'Source references do not prove a live deployment, active account, usage, or a bill.',
        'Only supplied text is inspected; ignored, omitted, binary, and external configuration may be missing.',
        'No prices are fetched or estimated. Availability and data needs must be established before changing architecture.',
    ]
    workflow_count = 0
    scheduled = 0

    def add(sid, name, path, line, meaning=None, step=None):
        item = found.setdefault(sid, {
            'id': sid, 'name': name, 'evidence': [],
            'meaning': meaning or f'This source references {name}. It does not establish active usage or charges.',
            'nextStep': step or 'Confirm which feature uses this dependency and whether a live service is connected before changing it.',
        })
        evidence = {'path': path, 'line': line}
        if evidence not in item['evidence']:
            item['evidence'].append(evidence)

    if not isinstance(files, list):
        files = []
        limits.append('Input was not a file list; no files were inspected.')
    if len(files) > MAX_FILES:
        limits.append(f'Inspection was limited to the first {MAX_FILES} supplied files.')
    for file in files[:MAX_FILES]:
        if not isinstance(file, dict):
            continue
        path, content = file.get('path'), file.get('content')
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        # Invalid paths cannot become evidence or manifest candidates.
        if path.startswith('/') or '\\' in path or any(x in ('', '.', '..') for x in path.split('/')):
            continue
        if len(content) > MAX_CONTENT:
            limits.append(f'An oversized file was omitted (limit {MAX_CONTENT} characters).')
            continue
        base = path.rsplit('/', 1)[-1]
        lines = content.splitlines()
        def first(pattern):
            return next((i for i, line in enumerate(lines, 1) if re.search(pattern, line)), 1)
        if base == 'fly.toml':
            add('fly', 'Fly.io', path, 1)
            for i, line in enumerate(lines, 1):
                if re.match(r'^\s*min_machines_running\s*=\s*[1-9][0-9]*\s*(?:#.*)?$', line):
                    add('fly-minimum', 'Configured Fly machine minimum', path, i,
                        'The configuration requests a positive minimum machine count. If deployed and applicable, that can retain running capacity; no live state or cost is established.',
                        'Inspect the deployed configuration and availability needs before changing the minimum.')
        manifests = {'vercel.json': ('vercel', 'Vercel'), 'netlify.toml': ('netlify', 'Netlify'),
                     'wrangler.toml': ('cloudflare', 'Cloudflare Workers'), 'wrangler.json': ('cloudflare', 'Cloudflare Workers'),
                     'wrangler.jsonc': ('cloudflare', 'Cloudflare Workers')}
        if base in manifests:
            add(*manifests[base], path, 1)
        if path.startswith('.github/workflows/') and path.count('/') == 2 and base.endswith(('.yml', '.yaml')):
            workflow_count += 1
            add('github-actions', 'GitHub Actions', path, 1,
                'Workflow files are present. Their presence does not prove execution, billable minutes, or a required dependency.',
                'Identify the checks, deployment triggers, and schedules that matter before moving execution.')
            if any(re.match(r'^\s*(?:[\'"]schedule[\'"]|schedule)\s*:', line) for line in lines):
                scheduled += 1
        if base in ('cloudbuild.yaml', 'cloudbuild.yml', 'cloudbuild.json'):
            add('google-cloud', 'Google Cloud', path, 1)
        if base.endswith(('.yaml', '.yml', '.json')) and base != 'package.json':
            for i, line in enumerate(lines, 1):
                if not line.lstrip().startswith('#') and re.search(r'(?:run\.googleapis\.com|serving\.knative\.dev/v1|[\'"]run[\'"]\s*,\s*[\'"]deploy[\'"])', line):
                    add('cloud-run', 'Cloud Run configuration candidate', path, i,
                        'Configuration contains a Cloud Run or Knative marker. Knative can run elsewhere; this is not proof of a Google Cloud deployment.',
                        'Confirm the target platform and deployed service settings before making a hosting decision.')
        if base == 'package.json':
            try:
                package = json.loads(content)
            except (ValueError, TypeError):
                limits.append('An invalid package.json was skipped; dependency detection may be incomplete.')
                continue
            if not isinstance(package, dict):
                continue
            for section in ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'):
                deps = package.get(section, {})
                if not isinstance(deps, dict):
                    continue
                for dependency in deps:
                    info = PACKAGES.get(dependency)
                    if dependency.startswith('@aws-sdk/'):
                        info = ('aws', 'AWS SDK')
                    elif dependency.startswith('@google-cloud/'):
                        info = ('google-cloud', 'Google Cloud')
                    if info:
                        # Search only the matching dependency section, avoiding prose/script keys.
                        section_start = first(r'"' + re.escape(section) + r'"\s*:')
                        line = next((i for i in range(section_start, len(lines) + 1)
                                     if re.search(r'"' + re.escape(dependency) + r'"\s*:', lines[i - 1])), section_start)
                        add(*info, path, line)
    return {
        'services': sorted(found.values(), key=lambda item: item['id']),
        'workflowCount': workflow_count,
        'scheduledWorkflows': scheduled,
        'limits': list(dict.fromkeys(limits)),
        'architectures': [
            {'id': 'local', 'title': 'Run on your computer',
             'fit': 'Personal tools that can wait while this computer is unavailable.',
             'tradeoff': 'You own availability, device access, and independent backups.',
             'cost': 'No hosting service is required for local execution; hardware, electricity, and optional external APIs still have costs.'},
            {'id': 'static', 'title': 'Static delivery or on-demand execution',
             'fit': 'Public pages and workloads that can run only when requests or events arrive.',
             'tradeoff': 'Persistent state, background tasks, and startup delays need a separate design.',
             'cost': 'May reduce idle compute; storage, requests, bandwidth, and provider limits still require verification.'},
            {'id': 'managed', 'title': 'Reuse a managed shared service',
             'fit': 'Shared applications that need remote data or continuous availability.',
             'tradeoff': 'Reduces some operating work while retaining provider dependencies and access-management responsibilities.',
             'cost': 'Prefer existing paid capacity where appropriate; verify its floor, scaling limits, and usage charges before adding resources.'},
        ],
    }
