# Public website

The `site/` directory is a static website. It has no functions, API routes, secrets, analytics, or dependency on the local application. Deploy only that directory to a static host. Never deploy an Unforge workspace or the repository root.

## Release downloads

Before deployment, stage the audited release artifacts in the ignored `site/downloads/` directory:

- `Unforge-0.3.0-macos-arm64.zip`
- `unforge-0.3.0.tar.gz`
- `SHA256SUMS.txt` containing SHA-256 checksums for both files

Validate checksums against the release manifest and scan extracted archives, nested binaries, archive metadata, and source history for credentials and personal information. Do not rebuild published versioned artifacts in place. For a new release, change filenames, version copy, and checksums together.

## Hosting

The public deployment uses Cloudflare Pages Direct Upload with a free DNS zone. Static requests and storage are free under the current Pages pricing; there are no Pages Functions. Account identifiers and authentication belong in private operator configuration, never this repository. Do not add a Worker, Function, paid tier, or metered service as part of a routine site update.

Use your own authenticated Wrangler installation to deploy `site/`. Verify HTTPS, the www redirect, guide anchors, 404 handling, security headers, and both download checksums after deployment. The `_headers` and `_redirects` files are Cloudflare Pages configuration; other hosts need equivalent configuration.

To preview locally, run `python3 -m http.server 4328 --bind 127.0.0.1 --directory site`. This preview does not apply Pages headers or redirects.

Keep the previous successful deployment available for rollback. Hosting rollback changes the website only and must never modify a local workspace.
