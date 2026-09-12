# Work without GitHub

Unforge is useful when a person can make an app, save it, check it, put it live, and get it back — without opening github.com. Get Unforge from unforge.app. The public GitHub repository is a copy of the source, not the storefront.

## The loop

1. Create a project or adopt a local folder.
2. Describe the change. Work happens on a copy, not the live project.
3. Save a version on that copy.
4. Run the project’s checks on your Mac. That is the replacement for GitHub Actions.
5. Merge onto the live project. Unforge will not merge until checks pass.
6. Connect a host you already own — Cloudflare Pages, a Supabase project if the app needs one, or a local folder. Any domain works. Sign in with Wrangler or the Supabase CLI using your account. Unforge does not provide a shared host.
7. Publish. Then open the site (or folder) and make sure it’s the version you just saved.
8. Keep encrypted backups on a drive you already own.

Unforge only treats an app as off GitHub when there is no GitHub remote, checks passed, and that live page looks right. Turning Actions off while GitHub is still the remote does not count.

If the project still has GitHub Actions, use **Import GitHub Actions** then **Leave GitHub for this app**. Checkout and setup-node steps are leftovers: the files are already on the Mac. Deploy secrets belong in your own Cloudflare or database login.

To send work to another person, write a change file from a lane. They need the same project history on their Mac first (export the project, or share the folder), then they import the change as a lane.

A second computer is **File → Open Workspace** on a recovered folder. The iOS Simulator cannot run Unforge. On one Mac, recover a capsule into a new folder to rehearse that path.

## What GitHub is still for

Inspecting Unforge the product, if you want. User apps do not need a GitHub account. Patches to Unforge can go to support@unforge.app as a bundle or a change file.

## Updating this public mirror

The Unforge app lives in a private working copy. The public repository is updated with:

```sh
python3 scripts/publish_public_github.py
```

That command refuses a dirty tree, runs `scripts/check_public_source.py`, and pushes only this checkout. It never uploads project folders from a person's Unforge home. Maintainers may set `UNFORGE_PRIVATE_PUBLISH_CHECK` to an extra scanner that is not part of this repository.
