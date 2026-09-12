# Roadmap

Unforge's direction is a provider-independent home for software: understandable to beginners, operable by agents, and usable through different tools. Capabilities below are separated by what the local implementation can establish.

The current product shape is in [PRODUCT.md](PRODUCT.md). Remaining work below is what this implementation still does not establish.

## Implemented foundation

The app creates local Git projects, imports supported Git bundles, edits text, previews static HTML with scripts disabled, saves versions, restores clean projects, and exports committed history. The local JSON CLI and HTTP API expose the same engine to other tools. An optional installed Codex CLI prepares a proposal in a separate copy for explicit review and application. React supplies the interface; Python's standard library and ordinary Git supply the local runtime. No Unforge account or cloud database is required.

Success means a person can create, change, recover, and export a project, then continue with ordinary Git after removing Unforge.

## Implemented in 0.2: remember, understand, recover

- **Project care:** portable purpose, behavior examples, decisions, setup notes, pending decisions, and credential locations. Human behavior observations retain evidence and remain separate from automated test results.
- **Change consequences:** bounded configuration observations and proposal comparisons for dependencies, service references, and recurring work. A simplification request incorporates the project's stated behaviors and reasons behind existing choices.
- **Service inventory:** manually sourced cost ranges, resource status, shared dependencies, and evidence. Unknown prices remain unknown; the app does not infer a live invoice from source code.
- **Shared local allowance:** durable attempt accounting across routed Codex and built-in practice actions. Stable operation IDs prevent those actions from starting twice on replay. Repeated failed, empty, or unknown outcomes pause the project.
- **Practice receipts:** simulated email, payment, and webhook actions demonstrate duplicate handling without contacting an external system. This does not sandbox an arbitrary application.
- **Recovery rehearsals:** capture saved source and declared local files, folders, or SQLite data; verify reconstruction and integrity; restore a separate project copy. The archive is unencrypted and application code is not executed during rehearsal.
- **Retirement planning:** account for detected service references and shared resources, require current recovery evidence, and record a portable completed plan. Provider shutdown and billing verification remain external work.

These capabilities address ownership and operating surprises with evidence the app can actually collect. They are not a universal deployment service or a guarantee of zero operating cost.

## Implemented in current source: connected local ownership

Reviewed folder adoption, paginated source/history, durable editor drafts and proposals, local trusted app/check execution, persistent managed app data, encrypted workspace recovery points, automatic change tracking, native iCloud upload metadata and opening a recovered workspace are implemented. See the completion audit for limits. Recovery points are independent full repositories, not cross-point incremental backups; local reconstruction and upload metadata are not second-device recovery evidence.

## Implemented: copies, merge, local checks, publish on your host

You can work on a copy, restack when copies overlap, merge after local checks pass, and publish to a host you already own. After you publish, open the URL and check it’s this version. Unforge only treats an app as off GitHub when there is no GitHub remote, checks passed, and that live page looks right. Agent proposals start on a copy and merge instead of patching a frozen project. Cloudflare Pages and Supabase functions use your signed-in CLI — any domain, no shared Unforge host. GitHub Actions `run` steps import as local checks; leftover `uses`/secrets are explained. A change file can move work between Macs without GitHub. A recovered backup can open in a new folder on this Mac (the iOS Simulator cannot run Unforge). Remaining: a real app that has actually left GitHub.

## Next: production application proof

Support narrowly defined application types with runnable previews and deterministic behavior checks. A clean build should be accompanied by proof that the intended user flow works, that data survives reopening, and that a restored copy can start. Test environments need explicit integration adapters that cannot inherit production destinations or schedules. Only claim these protections for adapters whose boundaries are implemented and tested.

Expand existing-project import beyond supported bundles and broaden real-world validation of the Codex adapter before adding other agent runtimes. Retain useful partial work when an agent fails, and make pending outcomes understandable.

## Next: provider-aware operation

Introduce narrowly supported deployment and recovery adapters after comparing reuse of current services, local or on-demand execution, and managed options against actual availability needs. Show the monthly floor, expected range, usage signal, and how to stop the service before provisioning it. Model provider-enforced caps separately from alerts and estimates.

Backups for hosted databases, uploads, identities, configuration, and secret recovery require provider-specific export and restore proof. A source bundle or local capsule cannot establish these outcomes. Any shutdown adapter must account for shared resources and retained storage; a healthy deployment is not cost evidence.

Success means the owner knows where the app runs, what requires payment, what the cap actually covers, and how to leave. No provider should become mandatory, including one run by Unforge's maintainers.

## Research: portable collaboration and personalization

Define portable proposal, discussion, review, and release-decision records, and prove interchange between independent installations. Identity, conflicts, moderation, access changes, and attachment storage require explicit designs. Cryptographic provenance alone does not establish correctness or trust.

Explore constrained extension points that preserve personal presentation and workflow choices through compatible updates. Arbitrary source edits cannot be promised conflict-free upgrades. The app already lets the user choose text size and technical detail explicitly; richer adaptation should preserve that control.

The native Mac shell, Developer ID signing, and notarized arm64 packaging are implemented; automatic updates remain future distribution work. Public hosting, unattended agent scheduling, production database migration, live cloud cost auditing and peer synchronization remain unimplemented. Neither the MIT license nor local execution changes external providers' terms or makes their services free.
