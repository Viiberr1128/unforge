# Unforge

**Built for humans. Operated by AI. Owned by you.**

A local home for software you own. Create a project, edit its files, save versions, and take its history with you—without a GitHub account, a hosted database, or a cloud subscription.

Unforge is an open-source **working alpha**, not yet a complete replacement for GitHub. This release proves the local ownership foundation. You can record requests for any coding agent, or optionally use an existing configured Codex CLI to prepare a proposal in a separate checkout. Review and apply the proposal before saving a version.

## Start locally

On macOS or Linux, a built release needs **Python 3.10+** and **Git 2.30+**. Building from source additionally needs **Node.js 22.12+ with npm**. Download and extract a release archive, or obtain the source, then open its folder in a terminal:

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
- Record a plain-language request under `.unforge/requests/` for handoff to your preferred coding agent.
- Optionally ask an installed, signed-in Codex CLI for an isolated proposal, review its diff, and apply it explicitly.

Saving a file and saving a version are separate steps. Save a version before exporting changes you want to preserve. Bundle upload accepts up to 20 MiB; local-path import through the CLI accepts up to 512 MiB. Imports require a supported current tree without symbolic links, submodules, or reserved files, with at most 128 MiB of file contents and 10,000 files.

The dependency view uses bounded source inspection. It is not an exhaustive inventory or a live billing audit, and its architecture suggestions are not price estimates.

This release does **not** directly import arbitrary repository folders, deploy applications, manage provider bills, synchronize peers, or provide a native desktop app. It is a local browser interface backed by Python. See the [roadmap](docs/ROADMAP.md) for the direction and the evidence required to expand these claims.

## Optional Codex proposals

Use an existing installed and configured Codex CLI; no model plugin is required. Availability means the executable was found, not that account authentication was verified. Start from a saved project. Codex works in a separate checkout; the app only applies a successful proposal when you choose to accept it and the original project is still clean at the same version. Then save a version to preserve the accepted change.

One job can run at a time per operating-system user, with a 15-minute timeout and bounded output. Unaccepted proposals and logs exist only for the current session and are lost on restart. Automated tests use a fake executable for controlled failure and cancellation coverage. One live Codex smoke test completed a specified README edit in a disposable project: the original stayed unchanged until explicit apply, and a saved bundle cloned with the edit and proposal metadata. This is limited integration evidence, not a general AI-quality or deployment guarantee. A successful CLI exit does not prove that tests passed or anything was deployed. See the [agent API](docs/AGENT-API.md) for limits and commands.

## Your files remain yours

Projects are stored under `~/.local/share/unforge` by default. Set `UNFORGE_HOME` before starting to choose another location:

```sh
UNFORGE_HOME="$HOME/My Unforge Data" ./start.sh
```

The projects contain normal Git repositories. You can work on them with Git directly and leave Unforge without a conversion service. GitHub can distribute this source or serve as a remote you choose; it is not required to run Unforge.

A bundle preserves committed Git history. It is **not** a backup of uncommitted files, ignored secrets, Git LFS content, or a business database. Restore changes project files; it cannot undo external effects such as messages already sent. See [ownership and recovery](docs/OWNERSHIP.md).

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
