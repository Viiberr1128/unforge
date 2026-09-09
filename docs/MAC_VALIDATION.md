# Mac app validation

## 0.3 source and recovery validation — September 9, 2026

- All 280 Python tests passed in 255.023 seconds. This includes HTTP ownership journeys, draft/proposal restart recovery, live SQLite WAL snapshots, wrong-key/corruption rejection, exact incremental restores, vault rollover and interrupted pointer publication, destination-space guards, malformed optional metadata, and process cleanup.
- Four JavaScript tests and the production frontend build passed.
- Global backup status, collapsed history, older-point disclosure, and narrow-screen layout passed rendered browser checks with no JavaScript errors.
- Desktop and 390-pixel browser flows exercised incremental backup, wrong-key rejection, passphrase clearing after both failure and success, and separate workspace recovery. Long recovery paths now wrap without horizontal overflow.
- Real iCloud testing observed all 20 encrypted shared-vault files uploaded, evicted from the local cache, and downloaded again. Exact snapshot restoration verified 764 files and all five managed project HEADs; a standalone recovery also checked three SQLite databases. A separately stored emergency key document survived its own cloud download byte-for-byte.
- Two real incremental copies added 129,424,364 bytes then 241,045 bytes, with 129,420,467 bytes reused on the second. Both remain independently selectable recovery points within the complete shared vault.
- These are tests on one Apple Silicon Mac. Independent second-device account sign-in, hosted databases, public deployments, and full application behavior were not established by file recovery. Private paths, keys and project data are excluded from this public validation record.

## Earlier native shell validation


Validated on the development Apple Silicon Mac on September 9, 2026. This is a locally signed build, not a notarized public release or a cross-version compatibility certification.

- 124 Python tests passed, including parent-pipe closure, explicit termination, workspace lock release, persisted project files, and desktop identity/Host checks.
- Four JavaScript tests and the Vite build passed.
- The frozen engine ran with only system executable paths available: no separately installed Python or Node was needed. Bundled frontend, local project creation, project care, operation ledger, recovery capsule creation/rehearsal and clean parent exit passed.
- Through the native AppKit/WebKit window: created a temporary project, edited its README, invoked Quit with an unwritten draft, cancelled Quit, verified the retained text, wrote the file and saved a version.
- Downloaded a normal Git bundle using NSSavePanel; the resulting file was private (0600) and readable by Git. Imported that same bundle using NSOpenPanel and the app import flow. Both copies had matching text and two saved versions.
- Quit the app through its native menu. Its owned engine exited and released the port. Reopened the app and confirmed both temporary projects persisted.
- The final shell discovers the optional CLI in the installed Codex app without running a model request.
- Installed the app in the user's Applications folder after verifying its ad-hoc signature. The existing workspace's project IDs and saved HEADs were identical before and after moving engine ownership from the old CLI process to the app.
- Temporary GUI test projects were kept outside the existing user workspace and removed after the test app exited. No paid services or model calls were used.

Packaging keeps source/runtime artifacts separate from user projects. Engine liveness uses a parent pipe; a native app crash or normal quit closes that pipe. Quitting an app attached to an independently started CLI engine does not stop that external engine. Red close preserves the window's content and engine; Quit warns about unfinished form work or session-only agent work. The active-agent warning has not been exercised with a paid model call.

The app still relies on an installed Git executable. Developer ID signing, notarization, automatic updates, Intel hardware testing and older macOS runtime testing are outstanding distribution work. The source build can target Intel on an Intel build host; this delivered artifact is arm64.
