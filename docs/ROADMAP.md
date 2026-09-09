# Roadmap

Unforge's direction is a provider-independent home for software: understandable to beginners, operable by agents, and usable through different tools. This document is a sequence of hypotheses to prove, not a promise that the alpha already supplies a universal forge.

## Foundation: local ownership

The alpha creates local Git projects, imports supported Git bundles, edits text, previews static HTML with scripts disabled, saves versions, restores clean projects, exports committed history, and records AI requests for a separate agent. An optional installed Codex CLI can prepare isolated proposals for explicit review and application; one live README-edit smoke test verified the proposal-to-export path, without establishing general AI quality. A local JSON CLI and HTTP API expose the engine to authorized tools. Offline dependency observations identify limited source configuration clues without claiming active services or bills. React provides the browser interface; a Python standard-library server coordinates files and Git. No account service or cloud database is required.

Success means a person can create, change, recover, and export a project—and continue with ordinary Git after removing Unforge.

## Next: a complete beginner workflow

Expand supported existing-project import beyond bundles, expand working previews beyond script-disabled static HTML, and broaden real-world validation of the existing Codex adapter before supporting more agents. A request should become a reviewable change with evidence, rather than an opaque background process.

Success means a beginner can request and inspect a useful change, understand what remains unchecked, and retain control of the result. Agent failures must leave recoverable work.

## Then: portable collaboration

Define versioned records for proposals, discussions, reviews, and release decisions. Prove interchange between two independent installations before claiming decentralized collaboration. Identity, conflicting decisions, moderation, access changes, and attachment storage need explicit designs.

Success means changing the host or client does not discard the project's collaboration history. Cryptographic provenance alone does not establish correctness or trust.

## Then: cost-aware operation

Introduce narrowly supported build and deployment adapters. Compare local execution, reuse of existing services, and remote options against actual availability needs. Explain estimated costs before creating resources. Distinguish spending targets from enforceable provider caps.

Success means the person knows where the app runs, what requires payment, and how to export or stop it. No provider should be mandatory, including one operated by Unforge's maintainers.

## Research: durable personalization

Explore explicit extension points that preserve a user's presentation preferences and workflow customizations through compatible application updates. Start with constrained application types. Arbitrary source changes cannot be promised conflict-free updates.

Success means a compatible update preserves supported customizations and presents real conflicts in understandable terms.

## Out of scope for the alpha

Public hosting, unattended agent scheduling, production database migration, cloud cost auditing, peer synchronization, signed collaboration records, and native desktop distribution are not implemented. Neither the MIT license nor the local architecture makes paid external services free.
