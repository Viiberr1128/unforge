#!/usr/bin/env python3
"""Fetch pinned restic into the project-local tool cache; never runs at app launch."""
import bz2
import hashlib
from pathlib import Path
import platform
import subprocess
import tempfile

VERSION = '0.19.1'
HASHES = {
 'darwin_arm64':'7be0a144ccc377880f294204aa271d76e4b79554b42a751151d425ce6ebac143',
 'darwin_amd64':'c38d579622cf602f665234c5a8c315030b6cf70656028fe6dc29a786b60e5f35',
 'linux_arm64':'a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465',
 'linux_amd64':'f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c',
}

def fetch(root=None):
    root=Path(root or Path(__file__).resolve().parents[1]); folder=root/'.tools'; folder.mkdir(exist_ok=True)
    arch={'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine(),platform.machine())
    key=platform.system().lower()+'_'+arch
    if key not in HASHES: raise RuntimeError('No pinned restic build for '+key)
    name=f'restic_{VERSION}_{key}.bz2'
    with tempfile.TemporaryDirectory(dir=folder) as temp:
        archive=Path(temp)/name
        subprocess.run(['curl','--fail','--silent','--show-error','--location','--max-time','120',
          f'https://github.com/restic/restic/releases/download/v{VERSION}/{name}','-o',str(archive)],check=True)
        raw=archive.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=HASHES[key]: raise RuntimeError('Restic release checksum does not match the pinned release.')
        binary=Path(temp)/'restic'; binary.write_bytes(bz2.decompress(raw));binary.chmod(0o755)
        binary.replace(folder/'restic')
    return folder/'restic'

if __name__=='__main__': print(fetch())
