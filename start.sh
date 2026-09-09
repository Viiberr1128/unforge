#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo 'Unforge needs Python 3.10 or newer. Install Python from python.org.' >&2; exit 1; }
command -v git >/dev/null || { echo 'Unforge needs Git 2.30 or newer. Install Git from git-scm.com.' >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else "Python 3.10 or newer is required.")'
if [ ! -f dist/index.html ]; then
  command -v npm >/dev/null || { echo 'Building from source needs Node.js 22.12 or newer with npm. Install it from nodejs.org, or use a built release.' >&2; exit 1; }
  node -e 'const [major,minor]=process.versions.node.split(".").map(Number); if(major<22 || (major===22&&minor<12)) {console.error("Node.js 22.12 or newer is required."); process.exit(1)}'
  echo 'Preparing your interface. This first build downloads pinned open-source dependencies.'
  npm ci --ignore-scripts --no-fund
  npm run build
fi
exec python3 launcher.py "$@"
