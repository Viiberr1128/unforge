#!/usr/bin/env python3
"""Start one local Unforge workspace. No daemon or login item is installed."""
import argparse
import os
import signal
from pathlib import Path
import sys
import threading
import webbrowser

from engine import Engine, Server


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--runtime-worker':
        from runtime import worker_main
        return worker_main(sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4319)
    parser.add_argument('--home', default=os.environ.get('UNFORGE_HOME', '~/.local/share/unforge'))
    parser.add_argument('--open', action='store_true', help='Open your default browser')
    parser.add_argument('--desktop-parent', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--desktop-id', default='', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Choose a port from 1024 to 65535.')
    try:
        server = Server(('127.0.0.1', args.port), Engine(args.home), Path(__file__).parent / 'dist')
    except (OSError, ValueError) as error:
        print(f'Could not start Unforge: {error}. If it is already running, open http://127.0.0.1:{args.port}.', file=sys.stderr)
        return 1
    url = f'http://127.0.0.1:{server.server_port}'
    server.desktop_id = args.desktop_id
    server.desktop_managed = args.desktop_parent
    print(f'\nUnforge is ready: {url}\nYour files: {server.engine.home}\nPress Control-C to stop.\n', flush=True)
    if args.open:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    # The Mac app holds the write end of stdin. Closing it (including an app
    # crash) releases the engine and its workspace lock without a login daemon.
    if args.desktop_parent:
        def parent_closed():
            try:
                # Unbuffered OS reads avoid holding Python's stdin lock when
                # the engine is terminated while this daemon thread is waiting.
                while os.read(sys.stdin.fileno(), 1):
                    pass
            finally:
                server.shutdown()
        threading.Thread(target=parent_closed, daemon=True).start()
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nWorkspace stopped. Your files remain on this computer.')
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
