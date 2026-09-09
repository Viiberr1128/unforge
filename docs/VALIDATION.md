# Validation record

This record distinguishes exercised behavior from wider product ambitions. It contains no customer data or account identifiers. Validation was performed on macOS on 2026-09-09; hosted runner results, if any, are recorded separately in the release.

## Version 0.2 release validation status

The integrated Python suite passed all 120 tests; a final recovery naming change then passed all 21 recovery tests, including its new regression. The JavaScript suite passed all four tests and the production Vite build succeeded. Optional agent tests used local executable doubles; no new model calls or provider provisioning were made.

Browser QA used the Codex in-app browser through Computer Use/Playwright against an isolated data folder. Desktop 1280 × 720 and mobile 390 × 844 views were checked. Page identity, meaningful rendering, absence of a framework error overlay, console health, and screenshots passed; no horizontal overflow was observed in the exercised mobile views. The exercised flows were:

- Project brief edits, blocked navigation and handoff download while unwritten, writing notes, behavior observations, and saving a Git version.
- Service cost provenance, shared resources, retained-resource evidence, and an editable simplification handoff.
- Local SQLite and uploads capture, persisted reconstruction evidence, restoration into a separate project, and retirement recording.
- Retirement evidence remaining legible after its metadata record was saved as a new version.
- A local simulated Codex proposal introducing Stripe: consequence review identified the new dependency before explicit application.
- Shared allowance refusal at the configured limit, successful practice after increasing the limit, and replay without a second charge to the attempt count.
- Usage initialization after projects loaded, persisted records after server restart, and mobile creation/care controls.

Recovery tests cover SQLite WAL backup, corruption, traversal, symbolic links, case and Unicode aliases, bounded extraction, exact saved source identity, ignored data preservation, and named independent restored copies. Ledger tests cover concurrent caps, stable replay, crash recovery, unknown outcome reconciliation, pauses, and old unresolved attempts remaining accessible beyond recent history. Project-care and client tests cover optimistic conflicts, bounded malformed input, portable notes, source/proposal uncertainty, shared impact, and recovery-backed retirement. Human behavior observations entered into Project care remain project data; they are not proof that Unforge executed those application flows.

The extracted-artifact smoke result and public runner evidence are recorded in the release notes. The smoke process rejects any invocation of Node/npm/npx or Codex and exercises the packaged CLI, source recovery, declared-data capsules, retirement records, and allowance replay without external services.

## Version 0.1 baseline

## Automated checks

The Python suite covers local project operations, import and export, API boundaries, offline dependency observations, and optional agent proposals. Agent tests use a fake executable to exercise controlled outcomes, including failure and cancellation, without calling a model. JavaScript tests cover frontend logic. `./check.sh` passed 51 Python tests and two JavaScript tests, followed by a production Vite build. Final checks include refusal of ignored writes/requests and preservation of ignored files during case- or Unicode-equivalent restores. No new external service was provisioned.

## Browser exercise

The running interface was exercised for project creation, file editing, version saving, restoration, bundle import including raw upload, request recording, and edit conflict handling. The mobile view at 390 pixels wide was checked for horizontal overflow; none was observed in the exercised view. The optional AI workflow was also exercised from start through review, application, and named version saving using a clearly identified simulated CLI adapter. Display preferences and read-only project metadata were checked. Browser/IAB viewport screenshots at 1536 × 1024 and 390 × 844 were visually inspected against the design reference. These checks do not establish every browser, device, or possible project layout.

## Live optional Codex integration

One live test used an existing authenticated Codex CLI with a disposable project and a request for a specified README sentence. The CLI exited successfully and only the README was modified in its proposal. The original project stayed clean and unchanged before explicit application.

Application recorded proposal metadata, saving produced a clean version, and bundle export followed by an ordinary Git clone reproduced both the edited content and proposal metadata. This demonstrates that bounded path through the integration. It does not establish general model quality, success on arbitrary tasks, a financial spending cap, or comprehensive sandbox containment.

## Release artifact

`scripts/smoke_release.py` passed against a freshly packaged archive. It verified exact archive manifest coverage and SHA256 digests, extracted into a temporary directory, started the included launcher with Node/npm/npx replaced by failure stubs, checked the served UI assets and health endpoint, and exercised create, edit, save, restore, export, import, and ordinary Git clone recovery. It stopped and verified cleanup of its server process group. This establishes the exercised macOS setup, not a signed native installer or every supported operating-system configuration.

## Not established

Public hosting and deployment are not implemented or live-tested. No claim is made that a source dependency corresponds to a real bill. Peer synchronization, universal repository compatibility, independent security audit, and broad model/task reliability are outside this validation record.
