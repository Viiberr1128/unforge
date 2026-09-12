# Completion audit

Updated September 11, 2026 after adding lanes, stacked merge, a local check graph, and observed publish. This is a capability inventory, not a claim that any production app has left GitHub. See README and the validation records for tested scope.

| Area | Implemented evidence | Remaining full-use work |
| --- | --- | --- |
| Adoption | `projects.py`, `test_projects.py`: reviewed independent folder imports, supported Git history, dirty/unborn sources and omission acknowledgement | Qualified support for LFS, submodules, symlinks and every production repository; data and credential migration |
| Navigation | Paginated file lists and history, on-demand text, older ancestor restore | Asset viewers, project-wide search, richer merge resolution |
| Durable work | `drafts.py`, `agent_jobs.py`, `lanes.py`: atomic draft/proposal storage, durable Git worktrees, agent jobs start in a lane | Multi-device editing and a second-person proposal bundle |
| Apps and checks | `runtime.py`, `checks.py`, `github_import.py`: trusted local run; Actions `run` steps import; leftovers explained; archive parks workflow files | OS isolation for untrusted code; marketplace Actions that are not local commands |
| Recovery | `backups.py`: encrypted incremental Restic vaults, exact snapshot reconstruction/hashes, SQLite online snapshots, separate restore; scheduler and native change watcher | Second-device recovery and account-access proof, retention UX, external database/object-store adapters, cross-database consistency |
| Cloud storage | iCloud and Google Drive desktop sync-folder destinations; native iCloud upload metadata and explicit cache eviction/download/content recovery rehearsal | Direct personal Google Drive authentication, second-device recovery and multi-provider quorum |
| Releases | `releases.py`: local, https, Cloudflare Pages, and Supabase function destinations; tokens stay in the owner’s CLI login; publish is incomplete until the bound URL serves the version | Schema/data deploys; live user-flow proof beyond the version marker |
| Spending | Local routed attempt limits and manually sourced resource estimates | Actual provider inventories and billing, scoped runtime/CI/resource enforcement, safe retirement adapters |
| Collaboration | `integrate.py`, `exchange.py`: disjoint merge, restack, stacked lanes; a change file another Unforge can import as a lane | Signed peer identity, live chat, multi-writer on one file |
| Mac distribution | Native app, bundled Python/frontend/Restic/helpers, folder pickers, recovered workspace opening, Developer ID signing, notarization and stapling of the arm64 zip | Recoverable updates, Intel and older macOS hardware qualification |
| Beginner use | Folder review, durable editor drafts, Use app / preview / checks, backup setup and separate recovery | Measured first-time user completion, simpler runtime configuration and connected production migration |

Existing production project folders and hosting remain authoritative. Source in Unforge does not imply their deployed databases, uploads, provider credentials, scheduled jobs or public releases moved. Each must pass a supported adoption inventory, clean-machine source build, app behavior checks, complete data restore, release/rollback rehearsal and cost review before a cutover. Avoid simultaneous migrations; qualify one complete application first.

An app is not GitHub-free until `app` reports `githubAbsent: true`: no GitHub remote, a passing Unforge check receipt, and an observed live publish. Disabling GitHub Actions while keeping a public source mirror is not that gate. A passing local suite proves the exercised cases; it does not establish crash-free operation or remote recoverability.
