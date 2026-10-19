# Contributing

Unforge aims to make software ownership practical for people who do not want to manage a development platform. Contributions should make a real task easier while keeping projects portable.

## Try the ownership loop

Useful testing does not require writing code. Start with a disposable project that contains no personal or customer information:

1. Create or bring in the project and save a named version.
2. Change one file. Confirm that Unforge distinguishes the written file from a saved version.
3. Write an outcome request. If you already have a supported coding tool installed, review its plan and proposal; do not enable a paid fallback for the test.
4. Create and rehearse a source capsule. Confirm that the recovered copy has the expected files and history.
5. Report where you hesitated, what you expected, what happened, and whether reopening the app changed the result.

Use a [first-run report](https://github.com/Viiberr1128/unforge/issues/new?template=first-run.yml) for that complete loop or a [bug report](https://github.com/Viiberr1128/unforge/issues/new?template=bug.yml) for one reproducible failure. Screenshots must use disposable data.

## Work locally

1. Obtain a copy of this source and install the requirements in the README.
2. Make a focused change with a clear user benefit.
3. Run `python3 -m unittest -v` and `npm run check` after installing frontend dependencies with `npm ci --ignore-scripts`. The combined release check is `./check.sh`.
4. For interface changes, exercise the affected flow in the browser. Use a temporary `UNFORGE_HOME` for disposable projects.
5. Describe the behavior before and after the change, the checks performed, and any remaining limitations.

Get the source from [unforge.app](https://unforge.app/guide.html#source). Run the checks locally. Send a Git patch or bundle to [support@unforge.app](mailto:support@unforge.app), or a change file from Unforge. A GitHub pull request on the public source copy still works if that is easier. This project does not use GitHub Actions.

The public repository is a source mirror, not a person's workspace. Do not commit home paths, credentials, databases, Unforge project IDs, or private application names. Run `python3 scripts/check_public_source.py` before sending a change. Maintainers publish a clean checkout with `python3 scripts/publish_public_github.py`.

## Design principles

- Use plain language. Explain what happened and what the person can do next.
- Keep saving a file, saving a version, and publishing a release distinct.
- Preserve ordinary files and Git interoperability.
- Keep the installed app usable without our servers.
- Do not add telemetry, paid services, or automatic provider calls as incidental dependencies.
- Report real capabilities. A saved AI request is not a completed agent task.
- Add meaningful tests for behavior and security boundaries; avoid tests that merely repeat implementation text.
- Never include credentials, private business data, or local development artifacts in a contribution.

Discuss substantial protocol, execution, or data-format changes before implementing them. These choices affect whether people can recover their work and switch tools.

By contributing, you agree that your contribution is available under this repository's MIT license. Do not submit material you lack permission to share.
