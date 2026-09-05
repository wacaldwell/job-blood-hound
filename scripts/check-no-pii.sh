#!/usr/bin/env bash
#
# check-no-pii.sh - pre-publish scanner for personal information.
#
# Run this before publishing this repository, before opening a fork to the
# public, and after any sanitization pass. It greps the working tree for a list
# of forbidden identity tokens plus a few generic shapes that personal data
# takes (email addresses, US phone numbers, absolute home-directory paths),
# prints every hit as file:line, and exits non-zero if it found anything.
#
# Usage:
#   scripts/check-no-pii.sh              scan the files git is tracking
#   scripts/check-no-pii.sh --tracked    the same thing, said explicitly
#   scripts/check-no-pii.sh --tree       scan the whole working tree
#   scripts/check-no-pii.sh --help
#
# Exit codes:
#   0   clean
#   1   at least one hit (read the output, fix or allowlist each one)
#   2   usage error
#
# The default is the tracked set, because that is exactly what `git archive`
# exports and so exactly what can reach a public repository. --tree also covers
# untracked and gitignored files, which is worth running occasionally (a data
# directory that is ignored today can be committed tomorrow by an over-broad
# `git add -f`), but it reports thousands of hits from local scratch
# directories that can never be published.
#
# This scanner is deliberately conservative and WILL produce false positives.
# Matching is case-insensitive and by substring, so a short token can match
# inside an unrelated word. Read every hit; do not add a token to an allowlist
# just to get a green run.
#
# To extend it, edit the arrays at the top. Nothing else needs to change.

set -uo pipefail

# ---------------------------------------------------------------------------
# TOKEN LIST. Fixed strings, matched case-insensitively anywhere in a line.
# Add a new forbidden name, handle, host or company here.
# ---------------------------------------------------------------------------
FORBIDDEN_TOKENS=(
  # --- identity, contact, location ---
  "Alex"
  "Caldwell"
  "wacaldwell"
  "acaldwell"
  "alexcaldwell"
  "@gmail.com"
  "(828)"
  "828-435"
  "Hendersonville"
  "Asheville"

  # --- private infrastructure ---
  "cavemanbeats"
  "tools.cavemanbeats"
  "mvd-clawbase"
  "mvd-mc"
  "mvd-job-api"
  "Mission Control"
  "100.84."

  # --- real employers ---
  "Mikmak"
  "Spins"
  "PayPal"
  "TATA"

  # --- real companies from the live job search ---
  "Airbnb"
  "AbbVie"
  "Allergan"
  "Bamboo Insurance"
  "Dragos"
  "DeepWatch"
  "ClaritasRx"
  "Navii"
  "SEI"
  "Calendly"
  "Aeroflow"
  "Carbon Mapper"
  "Sourcegraph"
  "LaunchDarkly"
  "Coinbase"
  "Komodo Health"
  "Penn Mutual"
  "Northflank"
  "PerfectServe"
  "Lowe's"
  "Truist"
  "IQVIA"
  "Labcorp"
  "FullStack"

  # --- real people ---
  "Andres"
  "Audwin"
  "Andy Ganoe"
  "David"
  "Hermes"
)

# ---------------------------------------------------------------------------
# Email domains that are safe to appear. Anything else that looks like an email
# address is reported. These are the RFC 2606 / RFC 6761 reserved names.
# ---------------------------------------------------------------------------
ALLOWED_EMAIL_DOMAINS=(
  "example.com"
  "example.org"
  "example.net"
  "example.edu"
  "example.invalid"
  "example-host"
  "localhost"
  # Vendor no-reply addresses. Not a person, and they turn up in commit
  # trailers on every generated commit.
  "anthropic.com"
)

# ---------------------------------------------------------------------------
# Home-directory path segments that are obviously placeholders, so
# /home/<one of these> and /Users/<one of these> are not reported.
# ---------------------------------------------------------------------------
ALLOWED_PATH_USERS=(
  "you"
  "user"
  "username"
  "USER"
  "youruser"
  "deploy"
  "runner"
)

# ---------------------------------------------------------------------------
# Directories never worth scanning: version control internals, virtualenvs,
# caches, and vendored dependencies.
# ---------------------------------------------------------------------------
EXCLUDED_DIRS=(
  ".git"
  ".venv"
  ".venv_new"
  ".venv.bak"
  "__pycache__"
  ".pytest_cache"
  ".mypy_cache"
  ".ruff_cache"
  "node_modules"
  "site-packages"
)

# ---------------------------------------------------------------------------

SELF="$(basename "$0")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Default to TRACKED files. That is the exact set `git archive` exports, so it
# is the set that can actually reach the public repository. Scanning the whole
# working tree instead reports thousands of hits from gitignored scratch dirs
# (.superpowers/, archive/, local notes) that cannot be published, which buries
# the real findings and trains you to ignore the scanner. Use --tree when you
# deliberately want to audit untracked local files too.
MODE="tracked"
case "${1:-}" in
  "") ;;
  --tracked) MODE="tracked" ;;
  --tree) MODE="tree" ;;
  -h|--help)
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "$SELF: unknown option '$1' (try --help)" >&2
    exit 2
    ;;
esac

cd "$REPO_ROOT" || exit 2

# Shared grep options. -I skips binary files, -n numbers lines, -H always
# prints the filename so the output is file:line everywhere.
GREP_OPTS=(-I -n -H)
for d in "${EXCLUDED_DIRS[@]}"; do
  GREP_OPTS+=(--exclude-dir="$d")
done
# The scanner quotes every forbidden token, so it would always match itself.
GREP_OPTS+=(--exclude="$SELF")

# In tracked mode, feed grep an explicit file list instead of the whole tree.
# -r stays on either way: git ls-files never yields a directory, so recursion
# changes nothing there, and keeping the flag avoids an empty array expansion
# that bash 3.2 rejects under `set -u`.
TARGETS=(.)
if [ "$MODE" = "tracked" ]; then
  TARGETS=()
  while IFS= read -r f; do
    TARGETS+=("$f")
  done < <(git ls-files)
  if [ "${#TARGETS[@]}" -eq 0 ]; then
    echo "$SELF: git ls-files returned nothing; is this a git repository?" >&2
    exit 2
  fi
fi

hits_total=0

# report SECTION < matching lines on stdin
report() {
  local section="$1" count line
  local -a lines=()
  while IFS= read -r line; do
    lines+=("$line")
  done
  count="${#lines[@]}"
  if [ "$count" -gt 0 ]; then
    echo
    echo "== $section ($count)"
    printf '%s\n' "${lines[@]}"
    hits_total=$(( hits_total + count ))
  fi
}

# --- 1. forbidden tokens ---------------------------------------------------
token_args=()
for t in "${FORBIDDEN_TOKENS[@]}"; do
  token_args+=(-e "$t")
done
report "forbidden tokens" < <(
  grep -r "${GREP_OPTS[@]}" -F -i "${token_args[@]}" -- "${TARGETS[@]}" 2>/dev/null
)

# --- 2. email addresses outside the reserved example domains ---------------
# Both "@example.com" and ".example.com", so a subdomain of a reserved name
# (globex.example.com, the domain the test fixtures use) is allowed too.
email_filter=()
for d in "${ALLOWED_EMAIL_DOMAINS[@]}"; do
  email_filter+=(-e "@${d}" -e ".${d}")
done
report "email addresses" < <(
  grep -r "${GREP_OPTS[@]}" -E -i \
    -e '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' \
    -- "${TARGETS[@]}" 2>/dev/null \
  | grep -F -i -v "${email_filter[@]}"
)

# --- 3. US phone numbers ---------------------------------------------------
# 555 is the exchange reserved for fiction, so a 555 number is a placeholder
# and is not reported. The gap allowed after the exchange is up to three
# characters, so "(555) 010-4477" is recognized and not just "555-010-4477".
report "phone numbers" < <(
  grep -r "${GREP_OPTS[@]}" -E \
    -e '(\+1[ .-]?)?\(?[0-9]{3}\)?[ .-][0-9]{3}[ .-][0-9]{4}([^0-9]|$)' \
    -- "${TARGETS[@]}" 2>/dev/null \
  | grep -E -v '(^|[^0-9])555[^0-9]{0,3}[0-9]{3}[ .-][0-9]{4}'
)

# --- 4. absolute home-directory paths --------------------------------------
path_filter=()
for u in "${ALLOWED_PATH_USERS[@]}"; do
  path_filter+=(-e "/Users/${u}" -e "/home/${u}")
done
report "absolute home paths" < <(
  grep -r "${GREP_OPTS[@]}" -E \
    -e '/(Users|home)/[A-Za-z0-9._-]+' \
    -- "${TARGETS[@]}" 2>/dev/null \
  | grep -F -i -v "${path_filter[@]}"
)

# ---------------------------------------------------------------------------
echo
if [ "$hits_total" -eq 0 ]; then
  echo "check-no-pii: clean ($MODE scan, no hits)"
  exit 0
fi
echo "check-no-pii: $hits_total hit(s) in the $MODE scan. Not safe to publish."
echo "Fix each one, or extend the allowlists at the top of $SELF if it is a"
echo "genuine false positive."
exit 1
