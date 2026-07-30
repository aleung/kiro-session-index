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
#   --no-index        install only; do not build or refresh the index
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="session-history"
SKILL_ROOT="${KIRO_SKILLS_DIR:-$HOME/.kiro/skills}"
SKILL_DIR="$SKILL_ROOT/$SKILL_NAME"
DO_INDEX=1

while [ $# -gt 0 ]; do
    case "$1" in
        --skill-dir) SKILL_DIR="$2"; shift 2 ;;
        --no-index)  DO_INDEX=0; shift ;;
        -h|--help)   sed -n '2,12p' "$0"; exit 0 ;;
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
install -m 0755 "$REPO/ksi-index" "$REPO/ksi-query" "$TOOL_DIR/"
install -m 0644 "$REPO"/ksi/*.py "$REPO"/ksi/*.sql "$TOOL_DIR/ksi/"

# The entry points add their own directory to sys.path, so the package sits beside
# them and needs no PYTHONPATH.
mkdir -p "$SKILL_DIR"
sed "s|@TOOL_DIR@|${TOOL_DIR/#$HOME/~}|g" "$REPO/skill/SKILL.md" > "$SKILL_DIR/SKILL.md"

say "✓ installed to $SKILL_DIR"
say "    SKILL.md, scripts/{ksi-index,ksi-query}, scripts/ksi/"

# The point of the install is independence, so verify it rather than assume it.
if grep -qF "$REPO" "$SKILL_DIR/SKILL.md"; then
    die "installed SKILL.md still points at the source repo"
fi
if grep -q '@TOOL_DIR@' "$SKILL_DIR/SKILL.md"; then
    die "installed SKILL.md still contains an unsubstituted placeholder"
fi
for f in ksi-index ksi-query ksi/__init__.py ksi/index.py ksi/query.py ksi/text.py \
         ksi/schema.sql; do
    [ -e "$TOOL_DIR/$f" ] || die "missing from install: $f"
done
# Import from a directory that is not the repo, so a stray relative path would fail here.
(cd / && "$TOOL_DIR/ksi-query" --help >/dev/null) || die "installed ksi-query is not runnable"
say "✓ verified: installed copy is self-contained"

if [ "$DO_INDEX" -eq 1 ]; then
    say "building index (first run reads every session; later runs are incremental)…"
    (cd / && "$TOOL_DIR/ksi-index")
    (cd / && "$TOOL_DIR/ksi-query" --status)
fi

cat <<EOF

Done. The skill is live; this repo is no longer needed at runtime.

  $TOOL_DIR/ksi-query "记忆 遗忘"
  $TOOL_DIR/ksi-query -t "connection refused"
  $TOOL_DIR/ksi-query --status

Optional -- put them on PATH (symlinks point at the installed copy, not the repo):
  ln -sf "$TOOL_DIR/ksi-query" ~/.local/bin/ksi-query
  ln -sf "$TOOL_DIR/ksi-index" ~/.local/bin/ksi-index
EOF
