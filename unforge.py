#!/usr/bin/env python3
"""A small, machine-readable client for Unforge's local API."""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import sys
import uuid


class Client:
    def __init__(self, port=4319):
        self.port = port

    def call(self, path, body=None, binary=False):
        headers = {}
        if body is not None:
            token = self.call('/session')['token']
            headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}', 'X-Unforge-Token': token}
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3600)
        try:
            connection.request('GET' if body is None else 'POST', '/api' + path,
                               None if body is None else json.dumps(body, allow_nan=False).encode('utf-8'), headers)
            response = connection.getresponse()
            raw = response.read()
            if response.status >= 400:
                raise ValueError(json.loads(raw).get('error', 'Request failed'))
            return raw if binary else json.loads(raw)
        finally:
            connection.close()


def care_revision(value):
    if value == 'null':
        return None
    if not re.fullmatch(r'[a-f0-9]{64}', value):
        raise argparse.ArgumentTypeError('Use the exact revision from care, or null when no care document exists.')
    return value


def read_json(path, expected_type):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, expected_type):
        raise ValueError('The JSON file must contain ' + ('a document object.' if expected_type is dict else 'a list of assets.'))
    return value


def write_download(path, content):
    # Exclusive creation preserves an existing export even when a caller reuses
    # its output path. Remove only our own incomplete file if its write fails.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        identity = output.fileno()
        created = os.fstat(identity)
        try:
            output.write(content)
            output.flush()
        except OSError:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                path.unlink()
            raise
    return {'exported': str(path.resolve()), 'bytes': len(content)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4319)
    commands = parser.add_subparsers(dest='command', required=True)
    raw = commands.add_parser('api', help='Call any local API endpoint; use a private JSON file for sensitive request fields')
    raw.add_argument('path', help='API path such as /backups or /projects/ID/runtime')
    raw.add_argument('--from-file', type=Path, help='POST a JSON object; omit for GET')
    commands.add_parser('projects', help='List owned projects')
    commands.add_parser('agent-status', help='Check installed optional Codex support; does not invoke it')
    agent_start = commands.add_parser('agent-start', help='Start Codex in an isolated project copy using your configured account')
    agent_start.add_argument('project')
    agent_start.add_argument('--from-file', required=True, type=Path)
    agent_start.add_argument('--operation-id', help='Stable ID for this exact request. Reuse it after a lost response; changing the request requires a different ID.')
    for action in ('agent-show', 'agent-cancel', 'agent-apply', 'agent-consequences'):
        commands.add_parser(action).add_argument('job')
    create = commands.add_parser('create', help='Create a local project')
    create.add_argument('name')
    create.add_argument('--description', default='')
    importer = commands.add_parser('import', help='Import a local Git bundle as a new owned project')
    importer.add_argument('path', type=Path)
    importer.add_argument('--name')
    for name in ('show', 'save', 'restore', 'request', 'write', 'export', 'insights', 'care', 'care-save',
                 'consequences', 'simplify', 'handoff', 'recovery', 'recovery-create', 'recovery-rehearse',
                 'recovery-restore', 'recovery-download', 'retirement', 'retire', 'behavior-check'):
        sub = commands.add_parser(name)
        sub.add_argument('project', help='Project ID returned by projects/create')
        if name == 'save': sub.add_argument('message')
        if name == 'restore': sub.add_argument('revision', help='Full commit ID in project history')
        if name in ('request', 'write'): sub.add_argument('--from-file', required=True, type=Path)
        if name == 'write': sub.add_argument('path', help='Path within the project')
        if name in ('export', 'handoff', 'recovery-download'): sub.add_argument('--output', required=True, type=Path)
        if name == 'care-save': sub.add_argument('--from-file', required=True, type=Path, help='JSON care document, without its response envelope')
        if name in ('care-save', 'retire', 'behavior-check'):
            sub.add_argument('--revision', required=True, type=care_revision, help='Exact care revision already reviewed, or null for the first document; never refreshed silently')
        if name == 'recovery-create':
            sub.add_argument('--assets-file', type=Path, help='JSON list of {path,kind} assets relative to the project; kind is file, directory, or sqlite. Omit for saved source only.')
        if name in ('recovery-rehearse', 'recovery-restore', 'recovery-download', 'retire'):
            sub.add_argument('capsule', help='Recovery capsule ID')
        if name == 'retire': sub.add_argument('--note', default='', help='Local retirement record; this command does not stop external services')
        if name == 'behavior-check':
            sub.add_argument('--example', required=True)
            sub.add_argument('--outcome', required=True, choices=('pass', 'fail', 'not-run'))
            sub.add_argument('--note', default='', help='Evidence for a recorded pass or fail; does not execute the application')
    recovery_import = commands.add_parser('recovery-import', help='Restore an existing local capsule into a new owned project')
    recovery_import.add_argument('path', type=Path)
    commands.add_parser('operations', help='Read shared routed attempt allowance, receipts, and paused projects')
    allowance = commands.add_parser('allowance', help='Set the local daily attempt cap across projects; not a dollar or provider-wide cap')
    allowance.add_argument('daily_limit', type=int)
    allowance.add_argument('--revision', type=int, required=True, help='Exact settings revision returned by operations')
    practice = commands.add_parser('practice', help='Create a local simulated receipt without sending, charging, or calling a webhook')
    practice.add_argument('project')
    practice.add_argument('action', choices=('email', 'payment', 'webhook'))
    practice.add_argument('--operation-id', required=True, help='Stable ID for this exact action and payload')
    practice.add_argument('--from-file', required=True, type=Path, help='JSON payload object; its hash binds this ID to the same intent')
    lanes = commands.add_parser('lanes', help='List isolated worktree lanes for a project')
    lanes.add_argument('project')
    lane_open = commands.add_parser('lane-open', help='Open a Git worktree lane. Live source stays untouched.')
    lane_open.add_argument('project')
    lane_open.add_argument('--name', required=True)
    lane_open.add_argument('--parent', help='Stack this lane on another lane ID')
    lane_write = commands.add_parser('lane-write', help='Write a text file inside a lane')
    lane_write.add_argument('project')
    lane_write.add_argument('lane')
    lane_write.add_argument('path')
    lane_write.add_argument('--from-file', required=True, type=Path)
    lane_save = commands.add_parser('lane-save', help='Commit the lane. This does not merge to live.')
    lane_save.add_argument('project')
    lane_save.add_argument('lane')
    lane_save.add_argument('message')
    lane_merge = commands.add_parser('lane-merge', help='Merge a lane onto live source after checks')
    lane_merge.add_argument('project')
    lane_merge.add_argument('lane')
    lane_restack = commands.add_parser('lane-restack', help='Replay a lane onto current live source after a parent lands')
    lane_restack.add_argument('project')
    lane_restack.add_argument('lane')
    checks = commands.add_parser('checks', help='Run the local check graph; this replaces GitHub Actions for the app')
    checks.add_argument('project')
    checks.add_argument('--lane')
    publish = commands.add_parser('publish', help='Publish a checked version and observe the bound destination')
    publish.add_argument('project')
    publish.add_argument('destination')
    bind = commands.add_parser('bind', help='Bind a live destination that uses YOUR Cloudflare or Supabase account')
    bind.add_argument('project')
    bind.add_argument('--from-file', required=True, type=Path, help='JSON destination object; never include tokens')
    commands.add_parser('setup', help='Show whether Git is installed and how to rehearse a second workspace')
    github = commands.add_parser('github-import', help='Turn GitHub Actions run steps into local Unforge checks')
    github.add_argument('project')
    github_archive = commands.add_parser('github-archive', help='Remove GitHub remotes and park workflow files')
    github_archive.add_argument('project')
    send = commands.add_parser('send-change', help='Write a lane to a file another person can open in Unforge')
    send.add_argument('project')
    send.add_argument('lane')
    send.add_argument('--title', required=True)
    send.add_argument('--note', default='')
    send.add_argument('--output', required=True, type=Path)
    receive = commands.add_parser('receive-change', help='Import a change file as a new lane')
    receive.add_argument('project')
    receive.add_argument('--from-file', required=True, type=Path)
    second = commands.add_parser('second-home', help='Open a capsule in a new workspace folder on this Mac')
    second.add_argument('--capsule', required=True, type=Path)
    second.add_argument('--into', required=True, type=Path)
    commands.add_parser('app', help='Show computed GitHub-absence status, checks, and destinations').add_argument('project')
    commands.add_parser('resume', help='Explicitly resume a project paused after repeated failed or empty attempts').add_argument('project')
    reconcile = commands.add_parser('reconcile', help='Record an independently checked outcome for an unknown attempt; never repeats it')
    reconcile.add_argument('operation_id')
    reconcile.add_argument('--outcome', required=True, choices=('succeeded', 'failed'))
    reconcile.add_argument('--note', required=True)
    args = parser.parse_args()
    client = Client(args.port)
    operation_id = None
    try:
        command = args.command
        if command == 'api':
            if not args.path.startswith('/') or args.path.startswith('//'):
                raise ValueError('Use a local API path starting with one slash.')
            result = client.call(args.path, read_json(args.from_file, dict) if args.from_file else None)
        elif command == 'projects': result = client.call('/projects')
        elif command == 'agent-status': result = client.call('/agent/status')
        elif command == 'agent-start':
            request = args.from_file.read_text(encoding='utf-8')
            operation_id = args.operation_id if args.operation_id is not None else uuid.uuid4().hex
            if args.operation_id is None:
                print(json.dumps({'operationId': operation_id, 'note': 'Keep this ID and use --operation-id with the same request if the response is lost.'}), file=sys.stderr)
            result = client.call('/agent/jobs', {'projectId': args.project, 'request': request, 'operationId': operation_id})
        elif command == 'agent-show': result = client.call('/agent/jobs/' + args.job)
        elif command == 'agent-consequences': result = client.call('/agent/jobs/' + args.job + '/consequences')
        elif command in ('agent-cancel', 'agent-apply'): result = client.call('/agent/jobs/' + args.job + '/' + command.removeprefix('agent-'), {})
        elif command == 'create': result = client.call('/projects', {'name': args.name, 'description': args.description})
        elif command == 'import': result = client.call('/import', {'path': str(args.path.resolve()), 'name': args.name})
        elif command == 'recovery-import': result = client.call('/recovery/import', {'path': str(args.path.resolve())})
        elif command == 'operations': result = client.call('/operations')
        elif command == 'allowance': result = client.call('/operations/settings', {'dailyLimit': args.daily_limit, 'expectedRevision': args.revision})
        elif command == 'practice':
            result = client.call('/operations/practice', {'projectId': args.project, 'operationId': args.operation_id,
                                                         'action': args.action, 'payload': read_json(args.from_file, dict)})
        elif command == 'lanes': result = client.call(f'/projects/{args.project}/lanes')
        elif command == 'lane-open': result = client.call(f'/projects/{args.project}/lanes', {'name': args.name, 'parent': args.parent})
        elif command == 'lane-write':
            result = client.call(f'/projects/{args.project}/lanes/{args.lane}/file',
                                 {'path': args.path, 'content': args.from_file.read_text(encoding='utf-8')})
        elif command == 'lane-save': result = client.call(f'/projects/{args.project}/lanes/{args.lane}/save', {'message': args.message})
        elif command == 'lane-merge': result = client.call(f'/projects/{args.project}/lanes/{args.lane}/merge', {})
        elif command == 'lane-restack': result = client.call(f'/projects/{args.project}/lanes/{args.lane}/restack', {})
        elif command == 'checks': result = client.call(f'/projects/{args.project}/checks', {'laneId': args.lane})
        elif command == 'publish': result = client.call(f'/projects/{args.project}/releases/publish', {'destinationId': args.destination})
        elif command == 'bind': result = client.call(f'/projects/{args.project}/releases/bind', {'destination': read_json(args.from_file, dict)})
        elif command == 'setup': result = client.call('/setup')
        elif command == 'github-import': result = client.call(f'/projects/{args.project}/github/import', {})
        elif command == 'github-archive': result = client.call(f'/projects/{args.project}/github/archive', {})
        elif command == 'send-change':
            result = client.call(f'/projects/{args.project}/exchange/export', {
                'laneId': args.lane, 'title': args.title, 'note': args.note, 'path': str(args.output.resolve())})
        elif command == 'receive-change':
            result = client.call(f'/projects/{args.project}/exchange/import', {'path': str(args.from_file.resolve())})
        elif command == 'second-home':
            result = client.call('/second-home', {'capsule': str(args.capsule.resolve()), 'destination': str(args.into.resolve())})
        elif command == 'app': result = client.call(f'/projects/{args.project}/app')
        elif command == 'resume': result = client.call('/operations/resume', {'projectId': args.project})
        elif command == 'reconcile': result = client.call('/operations/reconcile', {'operationId': args.operation_id, 'outcome': args.outcome, 'note': args.note})
        else:
            prefix = f'/projects/{args.project}'
            if command == 'show': result = client.call(prefix)
            elif command in ('insights', 'care', 'consequences', 'simplify', 'retirement', 'recovery'):
                result = client.call(prefix + '/' + command)
            elif command == 'care-save':
                result = client.call(prefix + '/care', {'document': read_json(args.from_file, dict), 'expectedRevision': args.revision})
            elif command == 'behavior-check':
                result = client.call(prefix + '/behavior-check', {'example': args.example, 'outcome': args.outcome,
                                                                  'note': args.note, 'expectedRevision': args.revision})
            elif command == 'retire':
                result = client.call(prefix + '/retirement', {'capsuleId': args.capsule, 'expectedRevision': args.revision, 'note': args.note})
            elif command == 'recovery-create':
                result = client.call(prefix + '/recovery', {'assets': read_json(args.assets_file, list) if args.assets_file else []})
            elif command in ('recovery-rehearse', 'recovery-restore'):
                result = client.call(prefix + '/recovery/' + args.capsule + '/' + command.removeprefix('recovery-'), {})
            elif command == 'recovery-download':
                result = write_download(args.output, client.call(prefix + '/recovery/' + args.capsule + '/download', binary=True))
            elif command == 'handoff':
                result = write_download(args.output, client.call(prefix + '/handoff', binary=True))
            elif command == 'save': result = client.call(prefix + '/save', {'message': args.message})
            elif command == 'restore': result = client.call(prefix + '/restore', {'revision': args.revision})
            elif command == 'request': result = client.call(prefix + '/request', {'message': args.from_file.read_text(encoding='utf-8')})
            elif command == 'write':
                from urllib.parse import quote
                try:
                    original = client.call(prefix + '/content?path=' + quote(args.path, safe=''))['content']
                except ValueError as error:
                    if str(error) != 'This file is not part of the visible project source': raise
                    original = None
                result = client.call(prefix + '/file', {'path': args.path, 'content': args.from_file.read_text(encoding='utf-8'), 'expectedContent': original})
            else:
                detail = client.call(prefix)
                if detail['dirty']:
                    raise ValueError('Save current changes before exporting with this client.')
                bundle = client.call(prefix + '/export', binary=True)
                result = write_download(args.output, bundle)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, http.client.HTTPException) as error:
        print(json.dumps({'error': str(error), **({'operationId': operation_id} if operation_id is not None else {})}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
