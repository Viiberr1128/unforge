# Completion audit

Updated September 9, 2026 after implementing the first connected ownership and recovery lifecycle. This is a capability inventory, not a production migration approval. See README and the validation records for tested scope.

| Area | Implemented evidence | Remaining full-use work |
| --- | --- | --- |
| Adoption | `projects.py`, `test_projects.py`: reviewed independent folder imports, supported Git history, dirty/unborn sources and omission acknowledgement | Qualified support for LFS, submodules, symlinks and every production repository; data and credential migration |
| Navigation | Paginated file lists and history, on-demand text, older ancestor restore | Asset viewers, project-wide search, richer merge resolution |
| Durable work | `drafts.py`, `agent_jobs.py`: atomic draft/proposal storage and restart recovery, stale-write checks | Multi-device editing/conflicts and cross-project concurrency |
| Apps and checks | `runtime.py`: trusted static/Node/Python/Swift commands, health/logs, process ownership, persistent or disposable data, local checks | OS isolation for untrusted code; complete native/web behavior automation; production hosting |
| Recovery | `backups.py`: encrypted incremental Restic vaults, exact snapshot reconstruction/hashes, SQLite online snapshots, separate restore; scheduler and native change watcher | Second-device recovery and account-access proof, retention UX, external database/object-store adapters, cross-database consistency |
| Cloud storage | iCloud and Google Drive desktop sync-folder destinations; native iCloud upload metadata and explicit cache eviction/download/content recovery rehearsal | Direct personal Google Drive authentication, second-device recovery and multi-provider quorum |
| Releases | Saved Git versions and isolated source runs | Deployment adapters, immutable release artifacts, schema-compatible rollback, health-based cutover and provider failure reconciliation |
| Spending | Local routed attempt limits and manually sourced resource estimates | Actual provider inventories and billing, scoped runtime/CI/resource enforcement, safe retirement adapters |
| Collaboration | Ordinary Git history/export | User/device identities, transport, portable issues/reviews/attachments, access revocation and multi-person conflict handling |
| Mac distribution | Native app, bundled Python/frontend/Restic/helpers, folder pickers and recovered workspace opening | Developer ID/notarization, recoverable updates, installed Git onboarding, Intel and older macOS hardware qualification |
| Beginner use | Folder review, durable editor drafts, Use app / preview / checks, backup setup and separate recovery | Measured first-time user completion, simpler runtime configuration and connected production migration |

Existing production project folders and hosting remain authoritative. Source in Unforge does not imply their deployed databases, uploads, provider credentials, scheduled jobs or public releases moved. Each must pass a supported adoption inventory, clean-machine source build, app behavior checks, complete data restore, release/rollback rehearsal and cost review before a cutover. Avoid simultaneous migrations; qualify one complete application first.

The release must not be described as replacing all of GitHub's collaboration, CI and deployment dependencies until those paths work independently through Unforge. A passing local suite proves the exercised cases; it does not establish crash-free operation or remote recoverability.
