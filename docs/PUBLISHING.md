# Publishing job-hound as a public repository

This document is the runbook for taking the sanitized tree public. It is written
for the maintainer of the private repository, not for public contributors.

## The decision this encodes

The private repository cannot itself be made public, because its git history
carries personal data. A scan of the 400-commit history found the maintainer's
email address in 8 commits, home region in 13, and 21 commits touching a tracked
`master_resume.yaml` that held a real name, phone number, street-level location
and full employment record.

Sanitizing the working tree does not remove any of that. `git log -p` still
prints it. Three options were weighed:

| Option | Verdict |
| --- | --- |
| Rewrite history with `git-filter-repo`, then publish this repo | Rejected. filter-repo scrubs literal tokens well, but the exposure here is narrative prose spread over hundreds of commits ("the operator ruled this", interview post-mortems, host names in design docs). No token list catches all of it, and you cannot easily prove you succeeded. It also rewrites every SHA, breaking the deployed host checkout. |
| Publish the sanitized tip and accept the history | Rejected. Publishing is effectively irreversible. On GitHub, unreachable objects remain fetchable by raw SHA, and forks and caches keep independent copies. "Publish now, scrub later" does not work. |
| **Snapshot the sanitized tree into a new repository with fresh history** | **Chosen.** One commit, one tree. The safety question becomes "is this working tree clean", which a script can answer, instead of "are 400 commits clean", which nothing can answer with confidence. |

A consequence worth stating plainly: the public repository starts with no
history. That is the point. The design rationale is preserved in prose, in
`CLAUDE.md` and `docs/superpowers/`, rather than in commit messages.

## What stays private

The `feature/public-sanitize` branch **is never merged into `main`**.

This is deliberate and it protects the running system. On `main`, the four
operator data files stay tracked, so the deployment host keeps receiving them on
`git pull` and the daily run keeps its resume and its `do_not_claim` ledger. If
the sanitize branch were merged, the next deploy would delete those files from
the host's working tree and the Fit Gate would start erroring on a missing
capability ledger.

The branch exists only as the source for the export.

## Procedure

1. **Confirm you are on the sanitize branch with a clean tree.**

   ```
   git checkout feature/public-sanitize
   git status
   ```

2. **Run the PII scanner.** It must exit 0. It is intentionally conservative and
   flags real names, the maintainer's contact details, private host names, real
   employers, the companies from the live job search, plus generic shapes such as
   non-example email addresses, US phone numbers and absolute home directory
   paths.

   ```
   ./scripts/check-no-pii.sh
   ```

3. **Run the test suite.** The public repository must pass on a fresh clone,
   where none of the operator data files exist and only the `.example` templates
   are present.

   ```
   source .venv/bin/activate && python -m pytest -q
   ```

4. **Build the snapshot.** This runs the scanner again as a hard gate, exports
   tracked files only, verifies no untracked private file leaked, and creates the
   single initial commit.

   ```
   ./scripts/export-public.sh ../job-hound-public
   ```

5. **Inspect the snapshot by hand before it ever reaches a remote.** This is the
   last point at which a mistake is free.

   ```
   cd ../job-hound-public
   git log --oneline          # expect exactly one commit
   ls -a                      # expect no jobs.db, no un-suffixed data files
   grep -ril "your-surname" . # expect no output
   ```

6. **Create the public repository and push.**

   ```
   gh repo create job-hound --public --source=. --remote=origin --push
   ```

7. **After publishing**, enable on the new repository:
   - Secret scanning and push protection (Settings, Code security)
   - Dependabot alerts
   - Branch protection on `main` requiring the test workflow

## Why the export uses `git archive`

`git archive` emits **tracked files only**. The maintainer's real
`master_resume.yaml`, `profile.yaml`, `ideal-jd.md` and `companies.yaml` still
exist in the private working directory, because they are the live configuration
that the tool actually runs on. They are untracked and gitignored, so an archive
cannot include them.

Never replace this step with `cp -r`, `rsync`, or a plain file copy. Those copy
the working directory, and the working directory contains the real resume.

## Keeping the two repositories in sync afterwards

There is no automation for this, on purpose. Every sync is a chance to leak, so
each one should be a deliberate act:

1. Do the work on `main` in the private repository as usual.
2. Cherry-pick or re-apply the public-safe parts onto `feature/public-sanitize`.
3. Re-run `./scripts/check-no-pii.sh`.
4. Re-run `./scripts/export-public.sh` into a fresh directory, then copy the
   changed files into your public clone and commit them there normally.

The public repository keeps its own ordinary history from its initial commit
forward. Only the first commit is a squashed snapshot.

## If something leaks anyway

Treat it as a disclosed secret, not as an embarrassment to quietly edit:

1. Delete or make the repository private immediately. Deleting is stronger,
   because forks of a deleted public repo are also removed.
2. Rotate anything credential-shaped that appeared.
3. Fix the tree, extend the token list in `scripts/check-no-pii.sh` so the same
   class of leak cannot recur, and re-export from scratch.

A force-push does not remove the leaked objects from GitHub, and it does not
touch anyone's existing fork or clone.
