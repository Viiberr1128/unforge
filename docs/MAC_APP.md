# Unforge for Mac

Unforge 0.3 has a native Mac window around its local workspace. The app bundles the
Python engine and the built interface, so opening it does not require Terminal,
Node, Python, a browser tab, an Unforge account, or GitHub.

## Install and open

Unzip the macOS download and drag `Unforge.app` into Applications (or your own
`~/Applications` folder). Open it from Finder, Spotlight, or the Dock.

- macOS 13 or later is the build target. The archive names its CPU architecture;
  use `arm64` for Apple Silicon. Compatibility with every supported macOS release
  has not been tested.
- Git is a runtime prerequisite. The app checks for it and explains how to get
  Apple's command line tools if needed. An existing Git installation works too.
- A **Developer ID** build that Apple has notarized and that still has its
  stapled ticket opens like other Mac software. An **ad-hoc** build (no
  Developer ID identity on the build Mac, or a build made with
  `--skip-notarize` and no notary credentials) is for a machine you already
  trust: right-click the app, choose Open, then Open again. If macOS still
  blocks it, use System Settings → Privacy & Security → Open Anyway for that
  build only. Do not disable Gatekeeper system-wide.
- Codex is optional. Agent work still requires your installed Codex CLI and your
  own account; the app does not include AI credits or make paid requests at launch.
- Running a project may require its own Node, Python, or Swift tools. These are
  separate from the bundled engine that opens Unforge itself.

Your existing workspace stays in `~/.local/share/unforge`. Installing, updating,
or deleting the app bundle does not delete those files. Projects and recovery
capsules are stored separately from the application. No private pilot project,
credential, journal entry, insurance file, or family data is part of the build.

The native window communicates with its local engine over loopback. It is not a
public hosting service and does not make this Mac a production host. Explicitly
started project commands can use the network and this Mac with your account's
permissions; trusted local execution is not a sandbox.

## Using the Mac window

The red close button hides the window and preserves its drafts and running
engine. Click Unforge in the Dock to return. Choose **Unforge → Quit Unforge**
or press **Command-Q** to quit; the app checks for unwritten drafts and pending
agent work before closing. Quitting stops the engine the app started. If the
app connected to an independently started local engine, that engine stays
running for its other users.

Choose **File → Open Workspace…** to open another managed workspace, including a
separately restored copy. Unforge remembers that choice. Switching closes its
owned engine before opening the selected workspace; an independently started
engine remains under its original owner's control.

The **File** menu can also reveal your project folder, open the workspace in your
browser, or reveal the engine log. **View** provides reload and text size
controls. Reload also checks for unwritten changes. Engine logs live in
`~/Library/Logs/Unforge/engine.log`; they stay on this Mac.

## Try an app or use it locally

Runtime suggestions inspect common static, Node, Python, and Swift entrypoints.
Saving those settings does not execute code. Run and Check require explicit
trust and a saved source version, then work in a separate candidate copy. Logs
and exit results are real; an HTTP response or successful command does not prove
every application behavior works. Checks and dependency preparation are
serialized, and commands have bounded running times and retained logs.

Preview uses fresh data. Explicit persistent local use keeps `UNFORGE_DATA_DIR`
and configured data directories/variables in `.app-data/<project-id>` inside
the selected workspace. Other candidate files remain temporary. Removing a
stopped attempt does not delete that persistent data. Apps must actually use the
declared data paths; native frameworks may ignore a temporary HOME and need an
explicit store setting. Project commands that detach into another process
session are unsupported.

## Encrypted workspace backups

The app includes Restic for encrypted workspace recovery points. Set up a
recovery passphrase and choose your own external or synced destination folder.
Keep the passphrase somewhere accessible without this Mac. The passphrase is
required to decrypt every point made with that workspace's backup key; the
encrypted backup does not contain it. Changing the key is not supported because
doing so could strand older backups. Source, persistent
local app data, saved drafts, and proposals are within managed workspace scope;
external databases, original imported folders, and external credentials require
their own backup arrangements.

New incremental points share encrypted objects inside a `.ufvault` folder, so
unchanged file contents do not need another full encrypted copy. A `.ufpoint`
under its `points` folder identifies one exact snapshot; it is not a standalone
backup. Keep the entire vault together and select the point to restore. Existing
standalone `.ufbackup` points remain recoverable. Restic writes to a private local
repository, then Unforge publishes immutable objects to the selected destination.
Only one writer is supported; sharing a synced vault does not create multi-Mac
collaboration. Shared-object damage can affect multiple points, so keep an
independent destination copy. No points are automatically pruned. Disk-space
guards stop work before the local reserve is consumed, but history, metadata,
and storage-provider quota still grow.

Automatic backups are opt-in and operate while Unforge is open. A native helper
reports workspace changes without sending filenames outside the local engine.
Each completed backup is restored and checked locally before it is reported as
locally verified. A synced-folder copy is not proof of a completed cloud upload.
The upload-status helper reads macOS metadata only. Explicit iCloud recovery
rehearsal is separate: after every file has confirmed upload status, another
helper evicts only the local cache, observes every file as not downloaded,
requests the iCloud copy, and waits for download completion. Unforge then checks
encrypted file hashes and decrypts the selected snapshot into a new workspace.
Incremental rehearsal must cover the complete vault, not just the point
descriptor. Timestamp normalization during download is reported separately;
matching metadata alone never verifies content. This is a same-Mac cloud
round trip, not second-device recovery proof. Missing or timed-out evidence
remains unverified. Google Drive upload confirmation is not inferred from a
local file's existence.

Recovery opens a separate workspace instead of overwriting the current one.
Recovered apps and external actions do not start automatically. This package
does not buy storage or add a paid cloud provider; the limits and costs of any
destination you already use still apply.

## Build from source

On a Mac, install Apple's command line tools, Python 3.10 or later, and Node
22.12 or later. Then, from this repository:

```sh
npm ci
python3 -m unittest -v
npm run check
python3 scripts/build_macos.py --skip-web-build
```

The script installs pinned build dependencies into `.venv-macos`; it never uses
global pip. It compiles the AppKit/WebKit shell, freezes the Python engine and
web assets, compiles the iCloud upload-status, cloud-rehydration, and workspace
change helpers, downloads checksum-pinned
Restic and its license, creates the icon from its vector source, includes notices, signs
the app, verifies that signature, and writes:

```text
artifacts/macos/Unforge.app
artifacts/macos/Unforge-0.3.1-macos-arm64.zip
artifacts/macos/SHA256SUMS
```

The architecture follows the build Mac (`arm64` or `x86_64`). This is a
repeatable build procedure, not a claim of byte-for-byte reproducible archives:
Python, the Apple SDK, signatures, and archive timestamps affect the output.
`Contents/Resources/BUILD.json` records the version, source commit and dirty
status when Git metadata is available, Python version, and signature status. Build
dependencies come from [PyInstaller's official PyPI distribution](https://pypi.org/project/pyinstaller/).

The app contains the Python runtime, its standard library, PyInstaller's
bootloader, and Restic. Their licenses and the interface notices are in
`Contents/Resources/Licenses`. Apple frameworks and the user's Git executable
remain system dependencies.

If the build Mac's Keychain has a **Developer ID Application** identity, the
script signs with that identity, the hardened runtime, and a secure timestamp.
Python's runtime needs the JIT / unsigned-executable-memory exceptions in
`macos/Unforge.entitlements`. Override the identity with
`UNFORGE_CODESIGN_IDENTITY` when more than one Developer ID Application
certificate is present. Without that identity, the script still ad-hoc signs for
local use.

Notarization is optional and uses credentials that never belong in this
repository. Create an App Store Connect API key with Notary access, keep the
`.p8` private, and write `~/.config/unforge/apple/notary-key-meta.json`:

```json
{
  "key_id": "KEYID",
  "issuer_id": "00000000-0000-0000-0000-000000000000",
  "key_path": "~/.config/unforge/apple/AuthKey.p8"
}
```

Team keys need `issuer_id`; individual keys omit it. The script then submits
the zip with `notarytool`, staples the ticket, and re-packs the archive. Pass
`--skip-notarize` to stop after signing. `Contents/Resources/BUILD.json` records `signing` as
`developer-id` or `ad-hoc`, and `notarized` after a successful staple.
Signature verification on the build Mac is not App Store review.

## Updates and recovery

Quit Unforge before replacing its app bundle. Keep independent recovery
capsules or Git bundle exports on another device: keeping everything on the
same Mac does not protect against losing that Mac. There is no automatic
updater, telemetry, login item, or paid background service installed by this
package.

Vaults roll over before their inventories exceed supported cloud checks. Each new vault stores a fresh seed and preserves prior vaults. This bounds per-vault inventories; it does not cap total retained storage or automatically delete history.

On first use of iCloud Drive, macOS may ask whether Unforge can access iCloud Drive. Choose **Allow** to let the backup finish. Folder copies made earlier by another application do not grant Unforge this per-app permission. A denied or unanswered prompt is not a completed backup.
