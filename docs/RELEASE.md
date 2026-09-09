# Build a release

Unforge is distributed as source or a built archive. Publishing that archive is separate from deploying a hosted application: the recipient runs Unforge locally.

## Prepare

On macOS or Linux, use Python 3.10+, Git 2.30+, and Node.js 22.12+ with npm. From the repository root:

```sh
npm ci --ignore-scripts
./check.sh
python3 scripts/package_release.py
python3 scripts/smoke_release.py artifacts/unforge-0.1.0.tar.gz
```

`./check.sh` runs backend tests and frontend checks. Packaging requires a built interface and writes:

- `artifacts/unforge-0.1.0.tar.gz`
- `artifacts/unforge-0.1.0.tar.gz.sha256`

The archive also contains `MANIFEST.sha256.json`, recording a SHA-256 digest for each packaged file. The packager writes a copy under `artifacts/`. These checksums detect differences; they are not a signed publisher identity.

The archive includes `dist`, so recipients need Python and Git but do not need Node, npm, or an initial frontend dependency download. Keep the archive and checksum together.

## Verify before publishing

Check the archive's contents for unintended local files or credentials. Extract it to a new directory and start it with a temporary data root. Exercise project creation, editing, saving, restoration, and bundle export from that extracted copy. Record actual validation results in the release notes; the existence of an archive is not evidence that those checks passed.

A recipient can verify the checksum on macOS from the directory containing both files:

```sh
shasum -a 256 -c unforge-0.1.0.tar.gz.sha256
```

After extracting the archive, run `./start.sh` or double-click `start.command` on macOS. The app listens locally at `http://127.0.0.1:4319`.

## Publish wherever you choose

The source and release artifacts can be distributed through GitHub, another file host, or direct transfer under the MIT license. No particular release host is required to run the app. The optional GitHub workflow runs local-equivalent checks for public repository pull requests or manual dispatch; it is skipped in private copies and does not publish or deploy anything. Uploading or publishing is not performed by the packaging script.

GitHub Pages cannot run the Python backend. Serving `dist` alone does not provide a functioning Unforge workspace. This release does not automatically provision public hosting, deploy a user's projects, or configure paid providers.

Retain the alpha scope and known limitations from the README in public release descriptions. Do not present planned peer synchronization or deployment adapters as shipped features. The optional Codex adapter has fake-executable test coverage and a live disposable README-edit smoke test through apply, save, export, and ordinary Git recovery. This does not establish general AI quality or public hosting behavior. Refer to the [validation record](VALIDATION.md) and record additional evidence separately. Codex installation and account configuration are optional and are not bundled with the release.
