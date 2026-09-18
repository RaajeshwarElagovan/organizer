# Branch rulesets for `main`

Definitions of the GitHub rulesets that protect `main`, kept here so the
policy is reviewable and re-importable (Settings → Rules → Rulesets →
New ruleset → Import a ruleset). GitHub only offers rulesets on public
repositories (or with a paid plan), so they are applied when the repository
is public. Apply or refresh them with:

```sh
gh api -X POST repos/RaajeshwarElagovan/organizer/rulesets --input .github/rulesets/main-integrity.json
gh api -X POST repos/RaajeshwarElagovan/organizer/rulesets --input .github/rulesets/main-review.json
gh ruleset list      # both should show as active
```

- `main-integrity.json` — **no bypass for anyone, admins included**: no
  deletion, no force-push, every change must arrive through a pull request
  with all four CI checks (`py3.8 (minimum)`, `py3.12 (system)`,
  `py3.14 (newest)`, `debian package` — the job names in
  `.github/workflows/ci.yml`) green and every review conversation resolved.
- `main-review.json` — one approving review from someone other than the last
  pusher, stale approvals dismissed on new commits. Repository admins may
  bypass *this ruleset only*, and only when merging a pull request: GitHub
  does not let an author approve their own PR, so without this the sole
  maintainer's PRs could never be merged. The bypass never applies to direct
  pushes, force-pushes, deletion or the CI checks.

If a job is renamed in `ci.yml`, change the `context` here in the same PR,
otherwise merges are blocked on a check that never reports.
