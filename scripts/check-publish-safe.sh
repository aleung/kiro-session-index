#!/usr/bin/env bash
# Fail if anything employer-specific reached tracked files, commit messages, or history.
#
# Patterns come from .publish-deny (untracked, one extended-regex per line, '#' comments).
# They are deliberately kept out of git: a deny-list of internal terms, committed to a
# public repo, is itself the leak.
#
# Usage: scripts/check-publish-safe.sh [--history-only|--tree-only]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DENY="${PUBLISH_DENY:-$REPO/.publish-deny}"
cd "$REPO"

if [ ! -f "$DENY" ]; then
    cat >&2 <<EOF
error: no deny-list at $DENY

Create it (it is gitignored) with one case-insensitive regex per line, covering:
  employer and product names, internal repo name prefixes, ticket id patterns,
  internal domains and hostnames, internal tooling names, corporate usernames,
  absolute home paths, IP addresses.
EOF
    exit 2
fi

# Strip comments, trailing whitespace and blank lines.
mapfile -t TERMS < <(sed -e 's/[[:space:]]*$//' "$DENY" | grep -vE '^([[:space:]]*#|$)')
[ "${#TERMS[@]}" -gt 0 ] || { echo "error: deny-list is empty" >&2; exit 2; }

# A deny term that matches ordinary vocabulary makes the report useless: every line
# of every file hits, and a real leak is invisible in the noise. Validate each term
# against benign controls before scanning. Unanchored product abbreviations are the
# usual cause -- a short one can appear inside a common word such as "session", which
# this project mentions constantly.
CONTROLS='session
sessions
kiro-session-index
message
process
assessment
index
hello world
python3 stdlib'
broad=0
for t in "${TERMS[@]}"; do
    if printf '%s\n' "$CONTROLS" | grep -qEi -- "$t" 2>/dev/null; then
        printf 'error: deny pattern %s matches ordinary text\n' "'$t'" >&2
        printf '       anchor it, e.g. %s\n' "'\\b${t}\\b'" >&2
        broad=1
    fi
done
[ "$broad" -eq 0 ] || { echo "refusing to scan with an over-broad deny-list" >&2; exit 2; }

PAT="$(printf '%s|' "${TERMS[@]}")"; PAT="${PAT%|}"

mode="${1:-all}"
fail=0
hit() { printf '\n%s\n' "$1" >&2; fail=1; }

if [ "$mode" != "--history-only" ]; then
    if out=$(git grep -nIiE "$PAT" -- . 2>/dev/null) && [ -n "$out" ]; then
        hit "FAIL tracked files:"; printf '%s\n' "$out" >&2
    fi
    if out=$(git ls-files | grep -Ei '\.(db|db-wal|db-shm|sqlite3?|jsonl)$'); then
        hit "FAIL session data is tracked:"; printf '%s\n' "$out" >&2
    fi
    # Staged but uncommitted content is what is about to enter history.
    if out=$(git diff --cached | grep -nIiE "^\+.*($PAT)") && [ -n "$out" ]; then
        hit "FAIL staged changes:"; printf '%s\n' "$out" >&2
    fi
fi

if [ "$mode" != "--tree-only" ]; then
    if out=$(git log --all --format='%H %s%n%b' 2>/dev/null | grep -nIiE "$PAT"); then
        hit "FAIL commit messages:"; printf '%s\n' "$out" >&2
    fi
    # History is published with the repo: a term removed by a later commit is still exposed.
    if out=$(git log --all -p --no-color 2>/dev/null | grep -nIiE "^\+.*($PAT)" | head -40); then
        hit "FAIL history (rewrite required before publishing):"; printf '%s\n' "$out" >&2
    fi
fi

if [ "$fail" -eq 0 ]; then
    echo "publish-safe: clean"
else
    echo "" >&2
    echo "Working-tree fixes do not remove terms from history; see AGENTS.md." >&2
fi
exit "$fail"
