# Unforge 0.1.0 — local ownership alpha

Built for humans. Operated by AI. Owned by you.

A working local software workspace with ordinary Git history and a beginner-oriented interface. GitHub is an optional place to download the code; the app does not require a GitHub account or server.

- Create a project, edit files, preview static HTML, and save named versions.
- Restore earlier work as a new version without erasing later history.
- Export and import supported Git bundles; recover files with ordinary Git.
- Record portable requests for any coding agent, or optionally run an installed Codex CLI in a separate copy, review its proposal, and apply it explicitly.
- Inspect offline configuration clues about external dependencies without claiming they are live services or bills.
- Choose larger text and whether technical details are shown.

Download `unforge-0.1.0.tar.gz`, extract it, and run `start.command` on macOS or `./start.sh` on macOS/Linux. Python 3.10+ and Git 2.30+ are required. The built download does not require Node. Source builds need Node 22.12+ and npm. This is a local browser app, not yet a signed native installer.

Validation: 51 Python tests, two JavaScript tests, production build, desktop/mobile Browser/IAB checks, and an extracted-release smoke test including startup without Node and ordinary Git recovery. One live Codex test completed a specified README change through proposal, application, save, and export; broader model reliability is not established.

The core has no subscription, telemetry service, or required cloud backend. Optional Codex use follows the user's existing account terms and usage limits. This release does not deploy public apps, run full JavaScript app previews, synchronize peers, or audit actual cloud bills. External data and separately stored secrets require their own backups.

MIT licensed. See README, SECURITY.md, and docs/VALIDATION.md for scope and supported limits. The checksum and manifest detect accidental changes; they are not a signed publisher attestation.
