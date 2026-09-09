# Working on Unforge

Build for humans. Make operations legible to agents. Keep ownership with the user.

- Keep `README.md` truthful about implemented capabilities. No fake agent execution, deployment success, project data, or cost estimates.
- Runtime must work without GitHub, a hosted database, telemetry, or an Unforge account.
- The engine binds to loopback only. Preserve Host, Origin, session-token, path, and Git hook boundaries.
- Keep project history and exports usable with ordinary Git. Never replace restore with a destructive reset.
- Retain optimistic edit checks so a browser does not silently overwrite another tool's work.
- Use temporary data directories for tests. Never register, modify, or delete real user projects to populate a demonstration.
- Preserve accessibility, empty states, small-screen layout, and the distinction between a written file and a saved version.
- Run `python3 -m unittest -v` and `npm run check` before handing off changes. On constrained machines, run one heavy check at a time.
- Source distributions may live on GitHub; checks and the runtime must also work locally without it.
- Do not add paid services or external model calls without explicit approval of the provider and expected cost.
