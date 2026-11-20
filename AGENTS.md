# Project instructions

## GitHub workflow

- After every requested code, configuration, or documentation change, review the diff and push the finished project changes to `origin/main` in the same work session, unless the user explicitly asks not to push.
- Stage intended project changes with `git add -A`; honor `.gitignore` and never add local browser profiles, credentials, runtime state, private logs, or generated driver files.
- Commit before pushing. Do not report success unless `git push origin main` succeeds.
- Do not run automated tests unless the user asks for tests or verification. Report when tests were not run.
