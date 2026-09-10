# Public website

The `site/` directory is a static website. It has no functions, API routes, secrets, analytics, or dependency on the local application. Deploy only that directory to a static host. Never deploy an Unforge workspace or the repository root.

## Release downloads

Before deployment, stage the audited release artifacts in the ignored `site/downloads/` directory:

- `Unforge-0.3.1-macos-arm64.zip`
- `unforge-0.3.1.tar.gz`
- `SHA256SUMS.txt` containing SHA-256 checksums for both files

Validate checksums against the release manifest and scan extracted archives, nested binaries, archive metadata, and source history for credentials and personal information. Do not rebuild published versioned artifacts in place. For a new release, change filenames, version copy, and checksums together.

## Hosting

The public deployment uses Cloudflare Pages Direct Upload with a free DNS zone. Static requests and storage are free under the current Pages pricing; there are no Pages Functions. Account identifiers and authentication belong in private operator configuration, never this repository. Do not add a Worker, Function, paid tier, or metered service as part of a routine site update.

Use your own authenticated Wrangler installation to deploy `site/`. Verify HTTPS, the www redirect, guide anchors, information-page links, 404 handling, security headers, and both download checksums after deployment. The `_headers` file is Cloudflare Pages configuration; other hosts need equivalent configuration. Domain-level redirects are not supported in Pages `_redirects`. Configure a host redirect rule matching only `www.unforge.app`, returning 301 to `concat("https://unforge.app", http.request.uri.path)` with query preservation enabled. Keep certificate-validation paths under `/.well-known/` outside this redirect so certificate renewal can complete. Both hostnames must have valid certificates, and both DNS records should be proxied when using a Cloudflare redirect rule.

To preview locally, run `python3 -m http.server 4328 --bind 127.0.0.1 --directory site`. This preview does not apply Pages headers or redirects.

Keep the previous successful deployment available for rollback. Hosting rollback changes the website only and must never modify a local workspace.

## Public information pages

Deploy `about.html`, `terms.html`, `privacy.html`, and `cookies.html` alongside the homepage and guide. Each page and the 404 page must expose the same information footer. Keep policy dates, descriptions of local storage, release limitations, and provider behavior aligned with the implementation. The software remains MIT licensed; website terms must not narrow that grant.

The site currently has no authored cookies, analytics scripts, advertising pixels, forms, or third-party embeds. Cloudflare may still process network requests and issue security cookies. Check ordinary responses and browser storage after deployment; do not equate no analytics scripts with no hosting records. If optional tracking is introduced, implement the applicable consent controls before loading it and update the notices. Do not add a consent banner as decoration.

The public maintainer identity is the GitHub username, and the repository provides general contact and private vulnerability reporting. Do not insert a personal name, email, postal address, account identifier, or local filesystem path. Before collecting account, payment, marketing, or other new personal information, establish the required operator identity and private privacy-contact arrangements, determine applicable legal obligations, and update the policies. These plain-language notices are not an independent legal compliance certification.

Primary references for the current notices: [Cloudflare Privacy Policy](https://www.cloudflare.com/privacypolicy/), [Cloudflare security cookies](https://developers.cloudflare.com/fundamentals/reference/policies-compliances/cloudflare-cookies/), [GitHub Privacy Statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement), and the [MIT license](https://opensource.org/license/mit). Local application statements must be verified against its source and security documentation.
