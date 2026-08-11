#!/usr/bin/env bash
# Install session-history as a self-contained skill.
#
# Copies everything needed -- SKILL.md, both entry points, and the ksi package -- into
# the skill directory, following the convention of other skills here (code under
# scripts/). After this runs, THIS REPO IS NOT USED AT RUNTIME: the installed copy has
# no reference back to it. Re-run after changing anything in the repo.
#
# Options:
#   --skill-dir DIR   install somewhere other than ~/.kiro/skills/session-history
#   --bin-dir DIR     put the kiro-resume/ksi-query symlinks somewhere other than ~/bin
#   --no-index        install only; do not build or refresh the index

# Re-exec under bash when started by another shell, e.g. `sh setup.sh`: dash has no
# `set -o pipefail` and no ${BASH_SOURCE}, and its failure message points at the wrong
# thing. Kept in POSIX syntax so dash can parse this block, and placed before any
# bash-only construct so dash never reaches one.
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash > /dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
    echo "error: setup.sh needs bash; install it or run: bash setup.sh" >&2
    exit 1
fi

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="session-history"
SKILL_ROOT="${KIRO_SKILLS_DIR:-$HOME/.kiro/skills}"
SKILL_DIR="$SKILL_ROOT/$SKILL_NAME"
# Human-invoked tools need to be on PATH to exist at all, so the install places
# symlinks itself rather than printing a suggestion. Overridable because the tests
# must never write into the real ~/bin.
BIN_DIR="${KIRO_BIN_DIR:-$HOME/bin}"
DO_INDEX=1

while [ $# -gt 0 ]; do
    case "$1" in
        --skill-dir) SKILL_DIR="$2"; shift 2 ;;
        --bin-dir)   BIN_DIR="$2"; shift 2 ;;
        --no-index)  DO_INDEX=0; shift ;;
        -h|--help)
            # Print the leading comment block, which stops at the first blank line,
            # rather than a hardcoded line range that drifts when the header changes.
            sed -n '2,/^$/p' "$0" | sed 's/^#[[:space:]]\?//'
            exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

TOOL_DIR="$SKILL_DIR/scripts"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 is required"

# Confirm the stdlib can do what the schema needs. Everything else is stdlib-only,
# so this is the entire dependency check.
python3 - <<'PY' || die "your python3 sqlite3 cannot create the required FTS5 table"
import sqlite3, sys
con = sqlite3.connect(":memory:")
try:
    con.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='unicode61',"
                " content='', contentless_delete=1)")
    con.execute("INSERT INTO t(rowid,x) VALUES(1,'a')")
    con.execute("DELETE FROM t WHERE rowid=1")
except sqlite3.Error as e:
    print(f"  sqlite {sqlite3.sqlite_version}: {e}", file=sys.stderr)
    sys.exit(1)
PY
say "✓ python3 $(python3 -c 'import sqlite3;print("with sqlite "+sqlite3.sqlite_version)')"

# Replace scripts/ wholesale so a removed or renamed module cannot linger and shadow
# the current code.
rm -rf "$TOOL_DIR"
mkdir -p "$TOOL_DIR/ksi"
install -m 0755 "$REPO/ksi-index" "$REPO/ksi-query" "$REPO/tools/kiro-resume" "$TOOL_DIR/"
install -m 0644 "$REPO"/ksi/*.py "$REPO"/ksi/*.sql "$TOOL_DIR/ksi/"

# The entry points add their own directory to sys.path, so the package sits beside
# them and needs no PYTHONPATH.
mkdir -p "$SKILL_DIR"
sed "s|@TOOL_DIR@|${TOOL_DIR/#$HOME/~}|g" "$REPO/skill/SKILL.md" > "$SKILL_DIR/SKILL.md"

say "✓ installed to $SKILL_DIR"
say "    SKILL.md, scripts/{ksi-index,ksi-query,kiro-resume}, scripts/ksi/"

# The point of the install is independence, so verify it rather than assume it.
if grep -qF "$REPO" "$SKILL_DIR/SKILL.md"; then
    die "installed SKILL.md still points at the source repo"
fi
if grep -q '@TOOL_DIR@' "$SKILL_DIR/SKILL.md"; then
    die "installed SKILL.md still contains an unsubstituted placeholder"
fi
for f in ksi-index ksi-query kiro-resume ksi/__init__.py ksi/index.py ksi/query.py \
         ksi/text.py ksi/schema.sql; do
    [ -e "$TOOL_DIR/$f" ] || die "missing from install: $f"
done
# Import from a directory that is not the repo, so a stray relative path would fail here.
(cd / && "$TOOL_DIR/ksi-query" --help >/dev/null) || die "installed ksi-query is not runnable"
say "✓ verified: installed copy is self-contained"

# PATH symlinks. kiro-resume is invoked by hand, so it is useless unless it is on
# PATH; ksi-query is the other half of the same pair for hand use. ksi-index is left
# out on purpose: every query refreshes the index already, so a manual rebuild is
# rare enough to spell out in full.
#
# The symlink is what makes an in-process `import ksi` work from a PATH directory:
# CPython resolves the link before computing sys.path[0], so the entry point still
# sees the package sitting beside its real location.
mkdir -p "$BIN_DIR" || die "cannot create $BIN_DIR"
for tool in kiro-resume ksi-query; do
    ln -sfn "$TOOL_DIR/$tool" "$BIN_DIR/$tool" \
        || die "cannot link $BIN_DIR/$tool -> $TOOL_DIR/$tool"
done
say "✓ linked into $BIN_DIR: kiro-resume, ksi-query"

# A tool that is installed but not runnable is worse than one that is absent, so
# fail the whole install rather than warn. Run through the symlink, from an
# unrelated directory, which is exactly how the user will invoke it.
(cd / && "$BIN_DIR/kiro-resume" --help >/dev/null 2>&1) \
    || die "$BIN_DIR/kiro-resume is not runnable through its symlink"
say "✓ verified: kiro-resume runs through its symlink"

command -v fzf >/dev/null 2>&1 || say "  note: fzf not found — kiro-resume will use a numbered menu"

if [ "$DO_INDEX" -eq 1 ]; then
    say "building index (first run reads every session; later runs are incremental)…"
    (cd / && "$TOOL_DIR/ksi-index")
    (cd / && "$TOOL_DIR/ksi-query" --status)
fi

cat <<EOF

Done. The skill is live; this repo is no longer needed at runtime.

On PATH now:
  kiro-resume              # pick a past session and resume it
  kiro-resume 记忆         # ...find it by what was said in it first
  ksi-query "记忆 遗忘"    # search without leaving your current session
  ksi-query --status

Manual index rebuild, rarely needed (every query refreshes incrementally):
  $TOOL_DIR/ksi-index --full
EOF
