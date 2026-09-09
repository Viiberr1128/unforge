# Validation record

This record distinguishes exercised behavior from wider product ambitions. It contains no customer data or account identifiers. Validation was performed on macOS on 2026-09-09; hosted runner results, if any, are recorded separately in the release.

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
