#!/usr/bin/env bash
#
# export-public.sh - build a clean, history-free snapshot of the sanitized tree,
# ready to push to a NEW public repository.
#
# Why a snapshot instead of publishing this repo directly: this repository's git
# history contains the maintainer's real resume, contact details and private
# infrastructure notes across hundreds of commits. Sanitizing the working tree
# does not remove any of that from the history, and once a repository is public
# those objects stay reachable by SHA even after a rewrite. So the public
# repository gets a fresh, single-commit history instead.
#
# The export is built with `git archive`, which emits TRACKED FILES ONLY. That is
# the load-bearing detail: the maintainer's real master_resume.yaml, profile.yaml,
# ideal-jd.md and companies.yaml still exist in the working directory (they are
# the live config) but are untracked and gitignored, so they cannot travel. Never
# replace this with `cp -r` or `rsync`, which would copy them.
#
# Usage:
#   scripts/export-public.sh [DEST_DIR]
#
# DEST_DIR defaults to ../job-hound-public. The script refuses to write into an
# existing non-empty directory rather than deleting anything.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$(dirname "$REPO_ROOT")/job-hound-public}"
BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"

cd "$REPO_ROOT"

say()  { printf '%s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. refuse to export from anything but a committed, sanitized branch ------
if [ -n "$(git status --porcelain)" ]; then
    fail "working tree is dirty. Commit or stash first, so the export matches a real commit."
fi

say "Exporting branch: $BRANCH"
say "Source commit:    $(git rev-parse --short HEAD)"

# --- 2. the PII scan is a hard gate, not a warning ----------------------------
if [ -x scripts/check-no-pii.sh ]; then
    say ""
    say "Running the pre-publish PII scan..."
    if ! ./scripts/check-no-pii.sh; then
        fail "PII scan FAILED. Nothing was exported. Fix the hits above and re-run."
    fi
    say "PII scan clean."
else
    fail "scripts/check-no-pii.sh is missing or not executable. Refusing to export unscanned."
fi

# --- 3. never overwrite, never delete ----------------------------------------
if [ -e "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
    fail "destination '$DEST' exists and is not empty. Move it aside first (this script never deletes)."
fi
mkdir -p "$DEST"

# --- 4. tracked files only ----------------------------------------------------
git archive --format=tar "$BRANCH" | tar -x -C "$DEST"

# --- 5. prove the untracked live config did not travel ------------------------
leaked=0
for f in master_resume.yaml profile.yaml ideal-jd.md companies.yaml jobs.db; do
    if [ -e "$DEST/$f" ]; then
        printf 'ERROR: %s leaked into the export\n' "$f" >&2
        leaked=1
    fi
done
[ "$leaked" -eq 0 ] || fail "export aborted: untracked private files were present."

# --- 6. fresh history ---------------------------------------------------------
cd "$DEST"
git init -q
git add -A
git commit -q -m "Initial public release

job-hound: a read-only job discovery and application-prep pipeline.

Published as a fresh repository with no prior history. See CONTRIBUTING.md
for the development workflow and SECURITY.md for the data-handling posture."

say ""
say "Done. Clean snapshot at: $DEST"
say "  commits: $(git rev-list --count HEAD)   files: $(git ls-files | wc -l | tr -d ' ')"
say ""
say "Verify before you publish:"
say "  cd $DEST && git log --oneline && ls -a"
say ""
say "Then create the public repo (this does NOT push anything on its own):"
say "  cd $DEST"
say "  gh repo create job-hound --public --source=. --remote=origin --push"
