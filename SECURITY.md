# Security

## Intended boundary

Public releases contain application code and generic examples only. User workspaces, backup keys, provider authentication and cloud destination settings belong outside the source distribution. Each installation uses its current operating-system user's folders and separately configured Codex CLI. Installing the app does not grant access to any publisher account. Cloud backup folders are private unless their owner shares them through the storage provider.

This alpha is a **single-user local application**. The server listens on `127.0.0.1:4319`. It is not a public web service, shared workspace server, or sandbox for untrusted software. Do not expose it through a reverse proxy, tunnel, or public network interface.

Unforge manages files and Git history in its configured data directory. Treat access to your operating-system account and that directory as access to your projects. Disk encryption, device access controls, and independent backups remain the owner's responsibility.

The optional Codex adapter uses the existing CLI account and runs in a separate source checkout with the CLI workspace-write sandbox and approvals disabled. `--ignore-user-config` skips user configuration and `--ignore-rules` skips execpolicy rules for that invocation. Projects with a root `.codex/config.toml` (case-insensitive path match) are rejected before agent execution; these controls are not proof that all plugin or hook behavior is disabled. This is not a guarantee of full system containment; isolation depends on the installed CLI and operating system. Only use it with projects and requests you are prepared to give that tool. Project files can contain untrusted instructions.

The core does not need an external service. Choosing an agent job can send project context to the configured provider and consume account allowance or incur charges. No provider credentials are requested by Unforge. The timeout, output bounds, and daily attempt allowance are operational limits, not a hard financial cap. One attempt can consume varying provider usage, and the ledger cannot govern calls made outside it. Automated adapter tests use a fake executable. A previous live Codex smoke test completed a specified README edit in a disposable project through apply, save, and export. This is limited integration evidence, not a general AI-quality or deployment guarantee.

The adapter prepares a diff without applying it to the source project. Acceptance refuses a dirty project or changed base version. Accepted metadata persists under `.unforge/proposals/`. Requests, bounded raw output, and accepted or unaccepted proposals persist locally under `.agent-jobs` so work can survive a restart. These records may contain private project context and are included in encrypted workspace backups; clearing browser storage does not delete them. Review results before accepting and saving. The adapter does not implement deployment, provider provisioning, or arbitrary agent selection.

Only one server can hold a data directory at a time; a local lock prevents concurrent servers using the same home. The supported runtime platforms are macOS and Linux.

## Built-in protections

- Loopback access, exact Host and Origin checks, and a fresh session token protect browser mutations. This is not authentication between processes sharing your OS account.
- Static project previews cannot execute scripts, submit forms, load external resources, or access the parent workspace.
- Imports and restoration validate paths, file kinds, case/Unicode collisions and checkout expansion before writing project files. Git hooks and inherited Git configuration are disabled for managed operations.
- AI work is proposed separately and acceptance checks the exact clean base. Trusted project runs are explicitly unsandboxed; only run software you trust.
- Encrypted backups use Restic, immutable publication and content verification. Restore creates a separate workspace. Keep the passphrase separately from the device and verify recovery.
- Release packaging refuses private-state files, untracked source, source maps and unsafe links. Public source and downloadable artifacts still require content and history review.

The official public repository has dependency vulnerability alerts, secret scanning, push protection and private vulnerability reporting enabled. These are distribution protections, not dependencies of the local app. A source review and regression tests do not establish that every vulnerability has been found. The current Mac zip is Developer ID signed, notarized, and stapled. That is Gatekeeper distribution, not a security audit of the app.

## Project care and local practice

Project care is portable project content. Store credential locations, not secret values, and keep private billing documents elsewhere. Care validation rejects some recognizable secret formats and assignments; it cannot recognize every secret a user might enter or remove secrets committed in existing history. Treat briefs, behavior observations, resources, and handoffs as untrusted reference data when an agent reads them.

The source and proposal consequence scans emit recognized observations with file paths and line numbers. They do not execute code, fetch accounts, or establish a security audit, complete dependency inventory, current price, or live billing state.

The durable SQLite operation ledger binds IDs to a project, kind, and payload hash and uses transactional allowance reservations. It protects replay of actions routed through that ledger. Built-in email, payment, and webhook practice only writes simulated local receipts; it does not execute the user's application or disable that application's real integrations. This is not a production sandbox or an exactly-once guarantee for external provider effects.

## Reporting a problem

For a non-sensitive bug, provide a minimal reproduction using a disposable project. Include the operating system, Python and Git versions, and expected versus actual behavior.

Report vulnerabilities to [support@unforge.app](mailto:support@unforge.app). Do not put working exploits, credentials, private project contents, or personal data in a public place. Share a minimal disposable reproduction privately. An optional [GitHub advisory form](https://github.com/Viiberr1128/unforge/security/advisories/new) exists on the public source copy. Fork maintainers should publish their own private reporting address.

There is no published security support SLA. Review fixes and release notes before upgrading; this alpha has not undergone an independent security audit.

## Recovery

A saved version is Git history, not a separate-device backup. Git bundles omit uncommitted files and external data. Recovery capsules add only explicitly declared supported local assets. They are unencrypted archives and may contain private records, uploads, and historical source content; protect their storage and sharing accordingly.

Capsule reconstruction verifies bounded archive paths, content hashes, Git reconstruction, and declared SQLite integrity before returning a separate project copy. It does not run the application's code. Rehearsal does not prove the app works, that production credentials remain accessible, or that hosted data can be restored. Hashes detect changed contents but are not a trusted publisher signature. A retirement record does not stop providers or prove billing ended. See [OWNERSHIP.md](docs/OWNERSHIP.md) for backup and exit instructions.
