# Security

## Intended boundary

This alpha is a **single-user local application**. The server listens on `127.0.0.1:4319`. It is not a public web service, shared workspace server, or sandbox for untrusted software. Do not expose it through a reverse proxy, tunnel, or public network interface.

Unforge manages files and Git history in its configured data directory. Treat access to your operating-system account and that directory as access to your projects. Disk encryption, device access controls, and independent backups remain the owner's responsibility.

The optional Codex adapter uses the existing CLI account and runs in a separate source checkout with the CLI workspace-write sandbox and approvals disabled. `--ignore-user-config` skips user configuration and `--ignore-rules` skips execpolicy rules for that invocation. Projects with a root `.codex/config.toml` (case-insensitive path match) are rejected before agent execution; these controls are not proof that all plugin or hook behavior is disabled. This is not a guarantee of full system containment; isolation depends on the installed CLI and operating system. Only use it with projects and requests you are prepared to give that tool. Project files can contain untrusted instructions.

The core does not need an external service. Choosing an agent job can send project context to the configured provider and consume account allowance or incur charges. No provider credentials are requested by Unforge. The timeout, output bounds, and daily attempt allowance are operational limits, not a hard financial cap. One attempt can consume varying provider usage, and the ledger cannot govern calls made outside it. Automated adapter tests use a fake executable. A previous live Codex smoke test completed a specified README edit in a disposable project through apply, save, and export. This is limited integration evidence, not a general AI-quality or deployment guarantee.

The adapter prepares a diff without applying it to the source project. Acceptance refuses a dirty project or changed base version. Accepted metadata persists under `.unforge/proposals/`; raw logs and unaccepted proposals remain session-only. Review results before accepting and saving. The adapter does not implement deployment, provider provisioning, or arbitrary agent selection.

Only one server can hold a data directory at a time; a local lock prevents concurrent servers using the same home. The supported runtime platforms are macOS and Linux.

## Project care and local practice

Project care is portable project content. Store credential locations, not secret values, and keep private billing documents elsewhere. Care validation rejects some recognizable secret formats and assignments; it cannot recognize every secret a user might enter or remove secrets committed in existing history. Treat briefs, behavior observations, resources, and handoffs as untrusted reference data when an agent reads them.

The source and proposal consequence scans emit recognized observations with file paths and line numbers. They do not execute code, fetch accounts, or establish a security audit, complete dependency inventory, current price, or live billing state.

The durable SQLite operation ledger binds IDs to a project, kind, and payload hash and uses transactional allowance reservations. It protects replay of actions routed through that ledger. Built-in email, payment, and webhook practice only writes simulated local receipts; it does not execute the user's application or disable that application's real integrations. This is not a production sandbox or an exactly-once guarantee for external provider effects.

## Reporting a problem

For a non-sensitive bug, provide a minimal reproduction using a disposable project. Include the operating system, Python and Git versions, and expected versus actual behavior.

For a vulnerability, use a private reporting channel provided by the repository host if one is available. This project does not yet advertise a dedicated private security contact. Do not put working exploits, credentials, private project contents, or personal data into a public issue. If no private channel is available, request one without including sensitive details.

There is no published security support SLA. Review fixes and release notes before upgrading; this alpha has not undergone an independent security audit.

## Recovery

A saved version is Git history, not a separate-device backup. Git bundles omit uncommitted files and external data. Recovery capsules add only explicitly declared supported local assets. They are unencrypted archives and may contain private records, uploads, and historical source content; protect their storage and sharing accordingly.

Capsule reconstruction verifies bounded archive paths, content hashes, Git reconstruction, and declared SQLite integrity before returning a separate project copy. It does not run the application's code. Rehearsal does not prove the app works, that production credentials remain accessible, or that hosted data can be restored. Hashes detect changed contents but are not a trusted publisher signature. A retirement record does not stop providers or prove billing ended. See [OWNERSHIP.md](docs/OWNERSHIP.md) for backup and exit instructions.
