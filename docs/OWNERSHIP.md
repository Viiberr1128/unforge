# Ownership, backup, and leaving

Unforge stores projects locally using ordinary files and Git. The interface is optional: your committed source remains readable with Git independently of Unforge.

## Find your data

Each project has its own ID-named directory containing a `.git` repository and a tracked `.unforge/project.json` manifest. AI handoff requests are text files under `.unforge/requests/`. Project intent, service records, human behavior observations, and retirement records live in `.unforge/care.json`; save a version to include changes in Git history. The default data root is `~/.local/share/unforge`. The `UNFORGE_HOME` environment variable selects an alternative root when the server starts. This is separate from the folder containing Unforge's own source code.

The data root also holds `operations.sqlite3`, the durable local attempt ledger, and `.recovery/`, the capsule archives and rehearsal receipts. These workspace-level records are separate from a project's Git bundle. A Markdown handoff exports the current brief and recorded evidence, not the actual data, credentials, or complete local ledger.

Keep the whole data root in your backup plan if you want to preserve Unforge's local project records as well as project files. Stop the server and other writers while making a consistent filesystem copy. Back up to independent storage; another directory on the same disk does not protect against disk loss.

## Save, version, export

Editing a file updates the working copy. Saving a version records a Git commit. Bundle export includes committed Git history, not every file visible on disk.

You can inspect a downloaded bundle and clone its source into a new ordinary Git repository:

```sh
git clone /absolute/path/unforge-project.bundle recovered-project
cd recovered-project
git bundle verify /absolute/path/unforge-project.bundle
git log --oneline
```

The export downloads as `unforge-project.bundle`; your browser may rename repeated downloads. Replace the example path with the actual downloaded file.

The resulting clone is usable with Git directly. To add the project to Unforge instead, import the bundle through its interface or local CLI. Cloning it manually does not register the clone in Unforge. A Git bundle is not a substitute for backing up the complete Unforge data root.

## Import a bundle

The browser accepts a bundle upload up to 20 MiB. The local CLI can import an absolute path to a regular `.bundle` file up to 512 MiB:

```sh
python3 unforge.py import /absolute/path/unforge-project.bundle --name "Recovered project"
```

`--name` is optional. Existing Unforge metadata is preserved when supported; bundles without it receive local project metadata. Import creates a managed copy and preserves the exported Git refs. The selected checkout is limited to 128 MiB of file contents and 10,000 files. The current tree must use supported paths and must not contain symlinks, submodules, or reserved files such as `.gitattributes` and credential filenames. Rejected bundles remain usable with ordinary Git subject to its own requirements.

Import does not sanitize historical commits. Check provenance and committed content before sharing or handing a project to an agent. The app does not directly import arbitrary repository folders.

## What an export does not preserve

- Unsaved or uncommitted changes.
- Ignored files, including ignored credentials and local configuration.
- Git LFS object contents stored outside ordinary Git history.
- Databases, uploaded business files, or data held by external providers.
- External actions such as messages sent, purchases made, or deployed resources.

A bundle **can contain secrets that were committed**, including secrets removed in later commits. Review history before sharing it. Export is a portability feature, not a secret scrubber.

## Capture and rehearse local recovery

Open a project's **Recovery** tab after saving its source. A capsule includes a complete Git bundle and a manifest. You can additionally declare local files, folders, and SQLite databases inside that project. Declare their paths in a saved `.gitignore` first so recovered data remains separate from committed source. The app refuses overlapping source/data paths, symlinks, special files, and reserved credential paths.

SQLite capture uses SQLite's backup API and verifies integrity. Pause writers to other files before capture; changing files can cause capture to fail. Recovery is bounded to 100 declared asset roots, 256 MiB, and 10,000 archive entries. It does not crawl the computer for forgotten data or discover hosted databases and uploads.

Choose **Rehearse recovery** to reconstruct the capsule in a temporary workspace, verify manifest hashes and Git integrity, and check declared SQLite databases. A successful receipt names the exact capsule hash and source revision. Corrupting the capsule invalidates its exposed receipt. The rehearsal does not run application code, test logins, replay migrations, or contact providers. Source-only capsules are labeled accordingly.

**Restore as a new project** creates a separate managed copy and leaves the source project intact. A downloaded capsule can also be imported through the interface or local CLI on another installation. Capsules use a standard gzip-compressed tar archive containing `project.bundle`, manifest information, and declared assets; the source bundle remains usable with ordinary Git.

Capsules are **unencrypted**. They may contain customer records, private uploads, and historical source contents. Store them on independent storage you control, with the access protections that material needs. Keeping a capsule only on the same computer does not protect against losing that computer. Credentials kept separately, hosted data, undeclared local files, external Git LFS objects, and unsaved source remain omitted.

## Record a retirement plan

In **Project care → Services & costs**, record each resource as checked and stopped, or intentionally retained, with supporting observations. Account for service references found in the source even when your evidence is that a reference is unused. If a stopped resource was shared, record that you reviewed the effect on those projects. Unknown resources and unresolved shared effects keep the plan incomplete.

Save the final records, create a fresh capsule, and rehearse it. The retirement action requires successful server-side recovery evidence matching that exact saved source revision. It writes a historical plan record into care; save another version to preserve that receipt. The record refers to the capsule's earlier saved version, since recording the receipt itself changes the care file.

Completing the plan does not close accounts, stop compute, delete files, revoke access, or verify a zero bill. Retained storage, images, domains, backups, schedules, and shared resources need their own checks and later billing evidence. The note should identify what remains and where the independent backup is stored.

## Restore a version

The interface displays the latest 100 commits. The restore action accepts a version from that history, requires a clean working directory, and records the selected version's files as a new commit. It preserves the earlier Git history rather than deleting intervening versions. Save current changes before restoring.

Restoring project source does not restore an external database, reverse a migration, recall a message, or change a production deployment. Those systems require their own recovery procedures.

## Agent proposal records

Accepted Codex proposals place source changes and a record under `.unforge/proposals/{job-id}.json` in the working repository, staged but not yet committed. Save a version to include both in committed history and bundle exports. The record contains the request, base version, timing, provider, changed paths, and CLI exit code; it does not preserve raw logs or establish that tests passed.

Unaccepted proposals and raw output are held only in the current app session. Restarting discards them. Review or apply anything you want to retain before stopping the app. The source project is not changed by an unapplied proposal through the adapter. The separate operation ledger retains the attempt identity and recorded outcome; it is not a replacement for the lost proposal workspace. Reusing the same operation ID returns that history instead of automatically running the work again.

## Leave without permission

You can stop Unforge, copy your project repositories, and continue with ordinary Git and your preferred editor. No account cancellation, proprietary conversion, or Unforge server is required. Keep any external service exports and necessary credentials separately and securely.

The open-source MIT license covers Unforge. It does not transfer ownership of third-party dependencies or change the license of material you place in a project.
