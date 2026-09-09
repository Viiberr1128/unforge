# Unforge

**Built for humans. Operated by AI. Owned by you.**

A local home for software you own. Keep its files, history, purpose, operating decisions, and recovery material together—without a GitHub account, a hosted database, or an Unforge subscription.

Unforge is an open-source **working alpha**. Version 0.2 adds project care, change consequences, a shared local attempt allowance, practice receipts, recovery rehearsals, and retirement planning. You can record requests for any coding agent, or optionally use an existing configured Codex CLI to prepare a proposal in a separate checkout. Review and apply the proposal before saving a version.

## Start locally

On macOS or Linux, a built release needs **Python 3.10+** and **Git 2.30+**. Building from source additionally needs **Node.js 22.12+ with npm**. Download and extract an archive from [Releases](https://github.com/Viiberr1128/unforge/releases), or obtain the source, then open its folder in a terminal:

```sh
./start.sh
```

Open **http://127.0.0.1:4319**. On macOS, you can also double-click `start.command`, which starts the app and opens your browser. Keep its terminal open while using Unforge; press **Control-C** there to stop it.

If the built interface is absent, the first start installs pinned frontend dependencies with `npm ci --ignore-scripts` and builds it. That source setup requires Internet access. A built release includes the interface and skips this download. The core app runs locally without telemetry or a required cloud account. Optional Codex jobs contact the configured provider and use your existing account allowance or charges. There is no hard dollar cap; the execution timeout is not a spending guarantee.

## What you can do today

- Create managed local Git projects or import a supported Git bundle.
- Adjust text size and technical-detail preferences.
- Read and edit small text files (up to 128 KiB each; the interface lists up to 100 files).
- Preview a static HTML page with scripts disabled.
- Save named versions as ordinary Git commits.
- Restore a saved version as a new commit when the working directory is clean.
- Export committed history as a standard Git bundle.
- Inspect recognized dependency and hosting configuration clues, with file/line evidence.
- Keep a portable project brief, behavior examples, setup notes, decisions, and credential locations without secret values.
- Record services, estimated monthly ranges with their sources, shared dependencies, and evidence about what remains active.
- Compare configuration consequences of a Codex proposal and prepare a behavior-preserving simplification request.
- Record what happened when you tried a behavior in your own app, without confusing that observation with an automated test.
- Set one daily attempt allowance across this workspace's routed Codex and practice actions; replay an operation ID without starting it twice.
- Try built-in pretend email, payment, and webhook actions that produce local receipts without contacting a provider.
- Capture saved source and explicitly declared local data, rehearse its reconstruction, and restore a separate project copy.
- Complete a retirement plan after accounting for detected service references, shared resources, and a verified recovery capsule.
- Record a plain-language request under `.unforge/requests/` for handoff to your preferred coding agent.
- Optionally ask an installed, signed-in Codex CLI for an isolated proposal, review its diff, and apply it explicitly.

Saving a file and saving a version are separate steps. Save a version before exporting changes you want to preserve. Bundle upload accepts up to 20 MiB; local-path import through the CLI accepts up to 512 MiB. Imports require a supported current tree without symbolic links, submodules, or reserved files, with at most 128 MiB of file contents and 10,000 files.

The dependency and consequences views use bounded source inspection. They cannot determine actual invoices, deployed settings, or data flows. Recorded monthly ranges are your estimates with provenance. An attempt allowance limits how many routed actions start, not how much a provider charges for one action or for work started elsewhere.

Unforge runs as a local browser interface backed by Python. Public deployment adapters, live provider billing controls, peer collaboration, arbitrary repository-folder import, full JavaScript app previews, and a native desktop installer remain on the [roadmap](docs/ROADMAP.md). Built-in practice actions demonstrate a local operation ledger; they do not sandbox your app or switch its real integrations into test mode.

## Start with one useful project

1. Create a project or import a supported Git bundle. In **Project care → Your brief**, describe what it is for and one behavior that must keep working, such as “Save a note, reopen the app, and find it again.” Write the notes, then **Save a version**.
2. In **Services & costs**, record the accounts and resources the project depends on. Leave unknown amounts blank. Include who else uses a shared resource and where your estimate came from. Save another version after writing the records.
3. Open **Consequences** to inspect source clues. **Make this simpler** prepares an editable request that preserves your stated behaviors. Starting Codex is a separate action using your existing account.
4. In **Usage & practice**, set your local daily attempt allowance. Try a pretend action, then replay the same action and see the existing receipt returned. No email is sent and no money moves.
5. In **Recovery**, create a capsule of the saved project. Add local SQLite data or uploads when appropriate; declare these paths in `.gitignore` and save that rule first. Choose **Rehearse recovery**, then download a copy to independent storage.
6. When retiring a project, update service records with what you checked and what you will retain. Save those records, create and rehearse a fresh capsule, then record the completed retirement plan. This records your evidence; it does not delete provider resources or establish that billing stopped.

Capsules are unencrypted and can contain private source history and declared data. Reconstruction checks archive contents and SQLite integrity; it does not execute the application or prove that logins and external integrations work. Keep credential values separately and use the brief to record where access is kept.

## Optional Codex proposals

Use an existing installed and configured Codex CLI; no model plugin is required. Availability means the executable was found, not that account authentication was verified. Start from a saved project. Codex works in a separate checkout; the app only applies a successful proposal when you choose to accept it and the original project is still clean at the same version. Then save a version to preserve the accepted change.

One job can run at a time per operating-system user, with a 15-minute timeout and bounded output. The shared local ledger defaults to 20 attempts per UTC day and pauses a project after three consecutive failed, empty, or unknown outcomes. A lost response can be retried with the same operation ID without starting a second job. Unaccepted proposals and logs exist only for the current session; durable operation records survive restart and require an unknown outcome to be reconciled before a fresh retry.

Automated agent tests use a fake executable for controlled failure and cancellation coverage. A previous live Codex smoke test completed a specified README edit in a disposable project through apply, save, and export. This is limited integration evidence, not a general AI-quality or deployment guarantee. A successful CLI exit does not prove that tests passed or anything was deployed. See the [agent API](docs/AGENT-API.md) for limits and commands.

## Your files remain yours

Projects are stored under `~/.local/share/unforge` by default. Set `UNFORGE_HOME` before starting to choose another location:

```sh
UNFORGE_HOME="$HOME/My Unforge Data" ./start.sh
```

The projects contain normal Git repositories. You can work on them with Git directly and leave Unforge without a conversion service. GitHub can distribute this source or serve as a remote you choose; it is not required to run Unforge.

A Git bundle preserves committed source history. A recovery capsule adds the local files, folders, and SQLite databases you explicitly declare. Neither automatically discovers hosted databases, uploads kept elsewhere, credentials, or Git LFS objects. Restore creates local files; it cannot undo external effects such as messages already sent. See [ownership and recovery](docs/OWNERSHIP.md).

## Development

The frontend uses React and Vite. The backend uses the Python standard library and the installed Git executable. There is no backend package installation or hosted database.

From the repository root:

```sh
python3 -m unittest -v
npm ci --ignore-scripts
npm run build
```

After frontend edits, run `npm run build` again before starting the built app. Run one build or test suite at a time on resource-constrained machines.

Coding agents can use the [local JSON CLI and HTTP API](docs/AGENT-API.md). See [release packaging](docs/RELEASE.md) to create a distributable archive. The [validation record](docs/VALIDATION.md) describes exercised flows and remaining limits. The [product direction](docs/PRODUCT.md) and [design rationale](docs/HISTORY-INFORMED-DESIGN.md) explain the problems guiding the work.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before contributing and [SECURITY.md](SECURITY.md) for the local trust boundary. Unforge is available under the [MIT license](LICENSE).

## Interface

![Unforge desktop workspace with three disposable example projects](docs/desktop.png)

The screenshot shows real example projects created for validation. A new workspace starts empty. See the [design verification](docs/DESIGN.md) and [validation record](docs/VALIDATION.md).
