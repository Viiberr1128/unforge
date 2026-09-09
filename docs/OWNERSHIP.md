# Ownership, backup, and leaving

Unforge stores projects locally using ordinary files and Git. The interface is optional: your committed source remains readable with Git independently of Unforge.

## Find your data

Each project has its own ID-named directory containing a `.git` repository and a tracked `.unforge/project.json` manifest. AI handoff requests are text files under `.unforge/requests/`. The default data root is `~/.local/share/unforge`. The `UNFORGE_HOME` environment variable selects an alternative root when the server starts. This is separate from the folder containing Unforge's own source code.

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

## Restore a version

The interface displays the latest 100 commits. The restore action accepts a version from that history, requires a clean working directory, and records the selected version's files as a new commit. It preserves the earlier Git history rather than deleting intervening versions. Save current changes before restoring.

Restoring project source does not restore an external database, reverse a migration, recall a message, or change a production deployment. Those systems require their own recovery procedures.

## Agent proposal records

Accepted Codex proposals place source changes and a record under `.unforge/proposals/{job-id}.json` in the working repository, staged but not yet committed. Save a version to include both in committed history and bundle exports. The record contains the request, base version, timing, provider, changed paths, and CLI exit code; it does not preserve raw logs or establish that tests passed.

Unaccepted proposals and raw output are held only in the current app session. Restarting discards them. Review or apply anything you want to retain before stopping the app. The source project is not changed by an unapplied proposal through the adapter.

## Leave without permission

You can stop Unforge, copy your project repositories, and continue with ordinary Git and your preferred editor. No account cancellation, proprietary conversion, or Unforge server is required. Keep any external service exports and necessary credentials separately and securely.

The open-source MIT license covers Unforge. It does not transfer ownership of third-party dependencies or change the license of material you place in a project.
