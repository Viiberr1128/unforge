#!/usr/bin/env python3
"""A small, machine-readable client for Unforge's local API."""
import argparse
import http.client
import json
from pathlib import Path
import sys


class Client:
    def __init__(self, port=4319):
        self.port = port

    def call(self, path, body=None, binary=False):
        headers = {}
        if body is not None:
            token = self.call('/session')['token']
            headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}', 'X-Unforge-Token': token}
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=40)
        try:
            connection.request('GET' if body is None else 'POST', '/api' + path,
                               None if body is None else json.dumps(body).encode('utf-8'), headers)
            response = connection.getresponse()
            raw = response.read()
            if response.status >= 400:
                raise ValueError(json.loads(raw).get('error', 'Request failed'))
            return raw if binary else json.loads(raw)
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4319)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('projects', help='List owned projects')
    commands.add_parser('agent-status', help='Check installed optional Codex support; does not invoke it')
    agent_start = commands.add_parser('agent-start', help='Start Codex in an isolated project copy using your configured account')
    agent_start.add_argument('project')
    agent_start.add_argument('--from-file', required=True, type=Path)
    for action in ('agent-show', 'agent-cancel', 'agent-apply'):
        commands.add_parser(action).add_argument('job')
    create = commands.add_parser('create', help='Create a local project')
    create.add_argument('name')
    create.add_argument('--description', default='')
    importer = commands.add_parser('import', help='Import a local Git bundle as a new owned project')
    importer.add_argument('path', type=Path)
    importer.add_argument('--name')
    for name in ('show', 'save', 'restore', 'request', 'write', 'export', 'insights'):
        sub = commands.add_parser(name)
        sub.add_argument('project', help='Project ID returned by projects/create')
        if name == 'save': sub.add_argument('message')
        if name == 'restore': sub.add_argument('revision', help='Full commit ID in project history')
        if name in ('request', 'write'): sub.add_argument('--from-file', required=True, type=Path)
        if name == 'write': sub.add_argument('path', help='Path within the project')
        if name == 'export': sub.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    client = Client(args.port)
    try:
        command = args.command
        if command == 'projects': result = client.call('/projects')
        elif command == 'agent-status': result = client.call('/agent/status')
        elif command == 'agent-start': result = client.call('/agent/jobs', {'projectId': args.project, 'request': args.from_file.read_text(encoding='utf-8')})
        elif command == 'agent-show': result = client.call('/agent/jobs/' + args.job)
        elif command in ('agent-cancel', 'agent-apply'): result = client.call('/agent/jobs/' + args.job + '/' + command.removeprefix('agent-'), {})
        elif command == 'create': result = client.call('/projects', {'name': args.name, 'description': args.description})
        elif command == 'import': result = client.call('/import', {'path': str(args.path.resolve()), 'name': args.name})
        else:
            prefix = f'/projects/{args.project}'
            if command == 'show': result = client.call(prefix)
            elif command == 'insights': result = client.call(prefix + '/insights')
            elif command == 'save': result = client.call(prefix + '/save', {'message': args.message})
            elif command == 'restore': result = client.call(prefix + '/restore', {'revision': args.revision})
            elif command == 'request': result = client.call(prefix + '/request', {'message': args.from_file.read_text(encoding='utf-8')})
            elif command == 'write':
                detail = client.call(prefix)
                original = next((f['content'] for f in detail['files'] if f['path'] == args.path), None)
                result = client.call(prefix + '/file', {'path': args.path, 'content': args.from_file.read_text(encoding='utf-8'), 'expectedContent': original})
            else:
                detail = client.call(prefix)
                if detail['dirty']:
                    raise ValueError('Save current changes before exporting with this client.')
                bundle = client.call(prefix + '/export', binary=True)
                with args.output.open('xb') as output:
                    output.write(bundle)
                result = {'exported': str(args.output.resolve()), 'bytes': len(bundle)}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, http.client.HTTPException) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
