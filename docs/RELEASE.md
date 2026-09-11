# Build a release

Unforge is distributed as source or a built archive. Publishing that archive is separate from deploying a hosted application: the recipient runs Unforge locally.

## Prepare

On macOS or Linux, use Python 3.10+, Git 2.30+, and Node.js 22.12+ with npm. From the repository root:

```sh
npm ci --ignore-scripts
./check.sh
python3 scripts/package_release.py
python3 scripts/smoke_release.py artifacts/unforge-0.3.1.tar.gz
```

`./check.sh` runs backend tests and frontend checks. Packaging requires a built interface and writes:

- `artifacts/unforge-0.3.1.tar.gz`
- `artifacts/unforge-0.3.1.tar.gz.sha256`

The archive also contains `MANIFEST.sha256.json`, recording a SHA-256 digest for each packaged file. The packager writes a copy under `artifacts/`. These checksums detect differences; they are not a signed publisher identity.

The archive includes `dist`, so recipients need Python and Git but do not need Node, npm, or an initial frontend dependency download. Keep the archive and checksum together.

Source users who want encrypted backups run `python3 scripts/fetch_restic.py` once to obtain the checksum-pinned Restic executable. The native Mac package includes it. Native iCloud metadata and rehydration helpers are compiled by the Mac build; a plain source launch must not claim those checks succeeded when the helpers are unavailable.

## Verify before publishing

Source packaging refuses untracked files in a Git checkout, private-state filenames, unsupported file types, source maps and linked directories. Stage intended new source files before packaging. Tar headers omit account names and numeric owners; tar/gzip timestamps use `SOURCE_DATE_EPOCH` or zero. Mac ZIPs preserve required framework links and executable permissions while omitting Finder attributes, owner fields and local timestamps. These checks complement content review; they cannot recognize every secret pasted into a legitimate source file.

Inspect Git history as well as the current tree, scan the extracted release for secrets, and inspect archive headers, image metadata, and bundled executable strings before publishing. Use a secret scanner with redacted output, for example `gitleaks git . --redact` and `gitleaks dir PATH_TO_EXTRACTED_RELEASE --redact`. Never include a real workspace, cloud destination settings, authentication directory, recovery key or private validation receipts. Download the published assets and check their hashes and metadata again.

Check the archive's contents for unintended local files or credentials. Extract it to a new directory and start it with a temporary data root. Exercise project creation, editing, saving, restoration, and bundle export from that extracted copy. Record actual validation results in the release notes; the existence of an archive is not evidence that those checks passed.

A recipient can verify the checksum on macOS from the directory containing both files:

```sh
shasum -a 256 -c unforge-0.3.1.tar.gz.sha256
```

After extracting the archive, run `./start.sh` or double-click `start.command` on macOS. The app listens locally at `http://127.0.0.1:4319`.

## Native Mac package

After the checks above, run `python3 scripts/build_macos.py --skip-web-build` on a Mac. The Apple Silicon package is `artifacts/macos/Unforge-0.3.1-macos-arm64.zip`, with its checksum in `artifacts/macos/SHA256SUMS`. It targets macOS 13 or later and bundles Python, the built interface, Restic, and native workspace and iCloud helpers. Git remains a prerequisite; Node and Python are not needed to open the packaged app. Individual projects may need their own development tools.

The package is ad-hoc signed and is not notarized. Local signature verification does not establish Developer ID identity or guarantee Gatekeeper acceptance on another Mac. Public distribution with that identity requires a separate signing and notarization step. See [the Mac guide](MAC_APP.md) for installation, workspace switching, runtime limits, and build details. Neither packaging script installs the app or publishes it.

## Recovery format and validation scope

Version 0.3 includes incremental Restic recovery points: each `.ufpoint` selects an exact snapshot within its original `.ufvault`. Distribute or retain the whole vault, including its encrypted repository and point descriptors. A descriptor alone cannot restore data. Older standalone `.ufbackup` recovery points remain supported. Recovery always creates a separate workspace and does not start recovered apps or external actions.

Keep the recovery passphrase independently accessible without the original Mac. It decrypts points made with that workspace's existing backup key and is not included in the backup. There is no automatic key rotation or migration of older points. One writer owns an incremental repository; cloud synchronization is not multi-writer coordination. Unchanged content is deduplicated, but retained history and metadata grow, no automatic pruning runs, and damage to a shared encrypted object may affect several points. Independent copies remain necessary.

Local restoration, macOS upload flags, same-Mac iCloud rehydration, and recovery on a second device are separate evidence. An explicit iCloud rehearsal requires full-vault upload confirmation before local-cache eviction, observed eviction and redownload, encrypted-object hash checks, and an exact-snapshot decrypt/restore. Helpers do not delete cloud objects or handle keys. Timestamp changes are reported without substituting for content verification. Do not describe upload metadata or a matching local synced copy as cloud readback, and do not describe same-Mac rehearsal as second-device proof. Record the checks actually run for the packaged revision; these capabilities are not an assertion that a recipient's data or cloud account has been tested.

## Publish wherever you choose

The source and release artifacts can be distributed through GitHub, another file host, or direct transfer under the MIT license. No particular release host is required to run the app. The optional GitHub workflow runs local-equivalent checks for public repository pull requests or manual dispatch; it is skipped in private copies and does not publish or deploy anything. Uploading or publishing is not performed by the packaging script.

To update the public GitHub mirror from a clean checkout of this source, run `python3 scripts/publish_public_github.py`. That push is blocked when `scripts/check_public_source.py` finds a home path, private key, or workspace project folder. It does not copy anything from a person's Unforge home directory.

GitHub Pages cannot run the Python backend. Serving `dist` alone does not provide a functioning Unforge workspace. This release does not automatically provision public hosting, deploy a user's projects, or configure paid providers.

Retain the alpha scope and known limitations from the README in public release descriptions. Do not present planned peer synchronization or deployment adapters as shipped features. The optional Codex adapter has fake-executable test coverage and a live disposable README-edit smoke test through apply, save, export, and ordinary Git recovery. This does not establish general AI quality or public hosting behavior. Refer to the [validation record](VALIDATION.md) and record additional evidence separately. Codex installation and account configuration are optional and are not bundled with the release.
