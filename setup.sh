#!/usr/bin/env bash
# Install the session-history skill and build the index.
#
# The repo is the single source of truth: this copies SKILL.md into the global skill
# directory. Re-run it after editing skill/SKILL.md.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="session-history"
SKILL_DIR="${KIRO_SKILLS_DIR:-$HOME/.kiro/skills}/$SKILL_NAME"

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

chmod +x "$REPO/ksi-index" "$REPO/ksi-query"

mkdir -p "$SKILL_DIR"
# Point the skill at wherever this repo actually lives.
sed "s|~/projects/personal/kiro-session-index|${REPO/#$HOME/~}|g" \
    "$REPO/skill/SKILL.md" > "$SKILL_DIR/SKILL.md"
say "✓ installed skill -> $SKILL_DIR/SKILL.md"

say "building index (first run reads every session; later runs are incremental)…"
"$REPO/ksi-index"
"$REPO/ksi-query" --status

cat <<EOF

Done. Try:
  $REPO/ksi-query "记忆 遗忘"
  $REPO/ksi-query -t "connection refused"
  $REPO/ksi-query --status

Optional -- put the tools on PATH:
  ln -sf "$REPO/ksi-query" ~/.local/bin/ksi-query
  ln -sf "$REPO/ksi-index" ~/.local/bin/ksi-index
EOF
