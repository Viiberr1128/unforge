# Work without GitHub

Unforge is useful when a person can make an app, save it, check it, put it live, and get it back — without opening github.com. The public GitHub repository is only a distribution mirror for Unforge itself.

## The loop

1. Create a project or adopt a local folder.
2. Describe the change. An agent works in an isolated lane, not on live source.
3. Save a version in the lane.
4. Run the local check graph. This is the replacement for GitHub Actions.
5. Merge the lane onto live source. Merge refuses unless checks passed.
6. Publish to a bound destination (a local folder or an https URL). Publishing is not done until that destination serves this version.
7. Keep encrypted workspace recovery on storage you already own.

`unforge.py app PROJECT` reports `githubAbsent: true` only when there is no GitHub remote, a passing Unforge check receipt exists, and a live publish was observed. Turning Actions off while a GitHub remote remains is not that gate.

## What GitHub is still for

Finding Unforge, reading its MIT source, and sending a patch to Unforge the product. User apps do not need a GitHub account to exist, improve, or go live on a bound destination.

## Updating this public mirror

The Unforge app lives in a private working copy. The public repository is updated with:

```sh
python3 scripts/publish_public_github.py
```

That command refuses a dirty tree, runs `scripts/check_public_source.py`, and pushes only this checkout. It never uploads project folders from a person's Unforge home. Maintainers may set `UNFORGE_PRIVATE_PUBLISH_CHECK` to an extra scanner that is not part of this repository.
