# Unforge 0.3.0 — durable local work and encrypted recovery

This is a working alpha for local ownership and private qualification. It is not a production migration or a claim that all GitHub services have been replaced.

- Review and import an independent local folder copy with supported Git history.
- Encrypted incremental vaults reuse unchanged contents; exact-point restores and iCloud download rehearsals verify recovery while preserving existing work. Older standalone backups remain supported.
- Browse full paginated files/history and recover saved editor drafts and agent proposals after restart.
- Use trusted apps with managed persistent data or disposable previews; execute configured checks locally.
- Protect persistent data against a second writer after supervisor failure.
- Make encrypted whole-workspace recovery points using bundled Restic; verify a complete local restore before copying to existing storage.
- Schedule backups while Unforge is open, observe external edits in the native Mac build and inspect macOS iCloud upload metadata.
- Keep backup failures and upload uncertainty visible throughout the workspace. Vault rollover preserves earlier history while bounding individual recovery inventories.
- Restore into a separate folder and open that workspace through the Mac File menu.

The Mac package includes Python, the web interface, Restic and native helpers. It still requires installed Git and is ad-hoc signed, not Developer ID notarized. Public runtime/deployment adapters, team collaboration, second-device recovery, complete external cloud-data recovery and cross-platform qualification remain outstanding. See README for limitations and key recovery requirements.

---

# Unforge 0.2.0 — project care and recovery

Built for humans. Operated by AI. Owned by you.

This release extends the local ownership foundation into everyday operating work: understanding consequences before accepting an AI change, keeping project knowledge portable, tracking routed attempts, and proving what a recovery archive can reconstruct.

- Write a project brief with the behaviors that must keep working, setup notes, decisions, pending questions, and credential locations without secret values.
- Record services, manually sourced monthly ranges, shared dependencies, and evidence about what remains active or intentionally retained.
- Inspect source and proposal configuration consequences. Prepare an editable simplification request that preserves the project's useful behaviors.
- Record observed behavior results separately from automated checks.
- Share one local daily attempt allowance across routed Codex and built-in practice actions. Replay operation IDs without starting those actions twice; inspect durable outcomes after restart.
- Try simulated email, payment, and webhook receipts without contacting a provider. Repeated failed, empty, or unknown attempts pause a project for review.
- Capture saved Git source plus declared ignored files, folders, or SQLite data. Rehearse reconstruction, download the capsule, or restore a separate managed project.
- Record a retirement plan after accounting for detected service references, shared-resource impact, and a successful current-version recovery rehearsal.

Create and edit projects, save versions, restore source, import and export Git bundles, and use optional Codex proposals as before. Existing source remains ordinary Git.

The release archive is `unforge-0.2.0.tar.gz`. Extract it and run `start.command` on macOS or `./start.sh` on macOS/Linux. Python 3.10+ and Git 2.30+ are required. The built archive includes the interface and does not require Node. Source builds need Node 22.12+ and npm.

The local allowance counts attempts, not dollars or tokens, and cannot cap a provider bill or work started in other tools. Cost ranges are manually recorded. Built-in practice does not sandbox arbitrary app code. Recovery capsules are unencrypted; declared data and Git history may contain private material. Rehearsal verifies reconstruction and SQLite integrity, not application behavior, hosted databases, or live integrations. Retirement records evidence and does not stop or delete provider resources.

The core has no subscription, telemetry service, or required cloud backend. Optional Codex use follows the existing account's terms and usage limits. Public app deployment, full JavaScript previews, peer synchronization, and a native installer remain outside this release.

Validation: 120 integrated Python tests passed, followed by all 21 recovery tests after the final recovered-copy naming change; four JavaScript tests and the production build passed. Desktop and mobile browser workflows were exercised with isolated test data. See [VALIDATION.md](VALIDATION.md) for evidence and limits. MIT licensed. Checksums are integrity evidence, not signed publisher attestations.

The built archive passed independent extraction and packaged smoke checks across all 75 manifest files. The smoke covered startup, source roundtrip, portable care, declared-data recovery, retirement recording, zero-limit refusal, and replay counted once, with Node/npm/npx and Codex blocked.
