# Security

## Intended boundary

This alpha is a **single-user local application**. The server listens on `127.0.0.1:4319`. It is not a public web service, shared workspace server, or sandbox for untrusted software. Do not expose it through a reverse proxy, tunnel, or public network interface.

Unforge manages files and Git history in its configured data directory. Treat access to your operating-system account and that directory as access to your projects. Disk encryption, device access controls, and independent backups remain the owner's responsibility.

The optional Codex adapter uses the existing CLI account and runs in a separate source checkout with the CLI workspace-write sandbox and approvals disabled. `--ignore-user-config` skips user configuration and `--ignore-rules` skips execpolicy rules for that invocation. Projects with a root `.codex/config.toml` (case-insensitive path match) are rejected before agent execution; these controls are not proof that all plugin or hook behavior is disabled. This is not a guarantee of full system containment; isolation depends on the installed CLI and operating system. Only use it with projects and requests you are prepared to give that tool. Project files can contain untrusted instructions.

The core does not need an external service. Choosing an agent job can send project context to the configured provider and consume account allowance or incur charges. No API keys are collected by Unforge. The timeout and output bounds are operational limits, not a hard financial cap. Automated adapter tests use a fake executable. One live Codex smoke test completed a specified README edit in a disposable project: the original stayed unchanged until explicit apply, and a saved bundle cloned with the edit and proposal metadata. This is limited integration evidence, not a general AI-quality or deployment guarantee.

The adapter prepares a diff without applying it to the source project. Acceptance refuses a dirty project or changed base version. Accepted metadata persists under `.unforge/proposals/`; raw logs and unaccepted proposals remain session-only. Review results before accepting and saving. The adapter does not implement deployment, provider provisioning, or arbitrary agent selection.

Only one server can hold a data directory at a time; a local lock prevents concurrent servers using the same home. The supported runtime platforms are macOS and Linux.

## Reporting a problem

For a non-sensitive bug, provide a minimal reproduction using a disposable project. Include the operating system, Python and Git versions, and expected versus actual behavior.

For a vulnerability, use a private reporting channel provided by the repository host if one is available. This project does not yet advertise a dedicated private security contact. Do not put working exploits, credentials, private project contents, or personal data into a public issue. If no private channel is available, request one without including sensitive details.

There is no published security support SLA. Review fixes and release notes before upgrading; this alpha has not undergone an independent security audit.

## Recovery

A saved version is Git history, not a separate-device backup. Git bundles omit uncommitted files and external data. See [OWNERSHIP.md](docs/OWNERSHIP.md) for backup and exit instructions.
