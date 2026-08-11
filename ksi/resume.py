"""Pick a past session and go back into it -- the human-facing half of the index.

Two properties here follow from the premise in DESIGN.md rather than from
convenience, and both are easy to "simplify" away later.

The session list is built from the `.json` sidecars, never from the index's
`sessions` table. The sidecars are the source of truth for which sessions exist;
the table is a derived cache, populated only for sessions whose `.jsonl` was
parsed. A launcher that cannot list your sessions because a cache is cold or
incomplete is a broken launcher, so the default listing does not open the index
at all.

Searching does need the index, and when it is unavailable the failure is loud
rather than quietly downgraded. An earlier version fell back to grepping the raw
logs, which cannot see inside CJK words (the tokenizer needs text.py's splitting)
nor inside identifiers -- so it answered "nothing found" for things that were
discussed at length, which reads as "this never happened".

Sub-agent sessions are excluded outright, from both the list and the search.
Every `user` turn inside one was written by the orchestrator, not by a person:
measured over 299 sub-agent sessions, 55% of their prose is tool-call purposes
and the rest is execution reporting. Searching them answers a question nobody
asked -- "what did a delegate report" -- when the question is "what did I discuss".
"""

import argparse
import calendar
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import unicodedata

from . import index as I
from . import query as Q

# Hits at or above this sort ahead of everything else, and within each band the
# order stays chronological. Sorting purely by recency buries an old session that
# matched 200 times under new ones that matched once; sorting purely by hit count
# breaks the common case, which is "carry on with yesterday's work".
BAND_THRESHOLD = 5


def load_sessions(sessions_dir=None):
    """Every session's metadata, read from the .json sidecars: {session_id: meta}.

    Unreadable sidecars are skipped rather than fatal: one corrupt file must not
    make the whole list unavailable.
    """
    sessions_dir = sessions_dir or I._sessions_dir()
    out = {}
    for path in glob.glob(os.path.join(sessions_dir, "*.json")):
        try:
            with open(path, encoding="utf8", errors="replace") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        sid = meta.get("session_id") or os.path.splitext(os.path.basename(path))[0]
        out[sid] = meta
    return out


def is_subagent(meta):
    return bool(meta.get("parent_session_id"))


def age(ts):
    """'3d ago' / '5h ago' / 'just now' from an ISO-8601 UTC timestamp."""
    try:
        t = calendar.timegm(time.strptime(str(ts).split(".")[0], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return "?"
    d = max(time.time() - t, 0)
    for unit, secs in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= secs:
            return f"{int(d // secs)}{unit} ago"
    return "just now"


def short_path(p):
    """~ for home, and only the last two components once a path gets deep."""
    home = os.path.expanduser("~")
    if p.startswith(home):
        p = "~" + p[len(home):]
    parts = p.split("/")
    if len(parts) > 4:
        return "~/" + "/".join(parts[-2:])
    return p


def display_width(s):
    """Columns occupied, counting CJK and other wide characters as two."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def truncate(s, max_w):
    """Cut to max_w display columns, marking the cut with a single ellipsis."""
    if display_width(s) <= max_w:
        return s
    out, w = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if w + cw > max_w - 1:      # leave a column for the ellipsis
            break
        out.append(c)
        w += cw
    return "".join(out) + "…"


def count_hits(keyword, db=None, sessions_dir=None):
    """{session_id: hits} across conversation prose and tool output.

    Refreshes the index first, and lets any failure propagate: the caller turns it
    into a loud error rather than a degraded search.
    """
    con, _refreshed = Q.open_index(db, auto_update=True, sessions_dir=sessions_dir)
    try:
        hits = Q.count_by_session(con, keyword)
        for sid, n in Q.count_by_session(con, keyword, tool_output=True).items():
            hits[sid] = hits.get(sid, 0) + n
        return hits
    finally:
        con.close()


def list_sessions(sessions, cwd=None, all_dirs=False, hits=None, limit=20):
    """Resumable sessions, newest first, as display-ready rows.

    Sub-agent sessions never appear. When `hits` is given, only sessions with a
    hit appear, and the high-hit band sorts first.
    """
    rows = []
    for sid, meta in sessions.items():
        if is_subagent(meta):
            continue
        if not all_dirs and meta.get("cwd") != cwd:
            continue
        if hits is not None and sid not in hits:
            continue
        n = hits.get(sid, 0) if hits else 0
        updated = meta.get("updated_at") or ""
        rows.append({
            "session_id": sid,
            "updated_at": updated,
            "age": age(updated),
            "title": (meta.get("title") or "(untitled)").replace("\t", " ")
                                                        .replace("\n", " "),
            "cwd": meta.get("cwd") or "",
            "hits": n,
        })
    rows.sort(key=lambda r: (1 if r["hits"] >= BAND_THRESHOLD else 0, r["updated_at"]),
              reverse=True)
    return rows[:limit]


def format_rows(rows, cols=100, show_cwd=False, show_hits=False, color=False):
    """One display line per row: age, optional hit count, optional cwd, then title.

    The title takes whatever width is left, so the fixed columns are budgeted
    first. Widths are counted in display columns, not characters, or a CJK title
    wraps and the list stops being scannable.
    """
    if color:
        dim, cyan, yellow, reset = "\033[2m", "\033[36m", "\033[33m", "\033[0m"
    else:
        dim = cyan = yellow = reset = ""

    avail = cols - 4          # the caller prefixes a line number: 'NN  '
    out = []
    for r in rows:
        used = 10
        age_part = f"{dim}{r['age']:<8s}  {reset}"
        hits_part = ""
        if show_hits:
            shown = (str(r["hits"]) + "x").rjust(5) + " "
            used += len(shown)
            hits_part = f"{yellow}{shown}{reset}"
        cwd_part = ""
        if show_cwd:
            shown = short_path(r["cwd"]) + "  "
            used += display_width(shown)
            cwd_part = f"{cyan}{shown}{reset}"
        out.append(age_part + hits_part + cwd_part
                   + truncate(r["title"], max(avail - used, 10)))
    return out


# ------------------------------------------------------------------ selection

def _choose(display):
    """Index of the row the user picked, or None if they cancelled.

    Rows are handed to fzf as "<index>\x1f<display>" and only field 2 is shown.
    The separator is deliberately a control character: titles routinely contain
    the box-drawing and pipe characters that an earlier version used, which made
    the parse depend on the content it was parsing.
    """
    if shutil.which("fzf"):
        payload = "\n".join(f"{i}\x1f{line}" for i, line in enumerate(display))
        proc = subprocess.run(
            ["fzf", "--ansi", "--no-sort", "--prompt=resume session> ",
             "--header=Select a session to resume (Esc to cancel)",
             "--delimiter=\x1f", "--with-nth=2"],
            input=payload, capture_output=True, text=True)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            return int(proc.stdout.split("\x1f", 1)[0])
        except ValueError:
            return None

    for i, line in enumerate(display, start=1):
        print(f"{i:2d}  {line}")
    print()
    try:
        raw = input("Number to resume (blank to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return n - 1 if 1 <= n <= len(display) else None


def _resume(row):
    """Replace this process with a Kiro session, in that session's directory."""
    target = row["cwd"]
    print(f"Resuming {row['session_id']} ...")
    if target and target != os.getcwd() and os.path.isdir(target):
        print(f"  cd {target}")
        os.chdir(target)
    try:
        os.execvp("kiro-cli", ["kiro-cli", "chat", "--resume-id", row["session_id"]])
    except OSError as e:
        print(f"error: cannot start kiro-cli: {e}", file=sys.stderr)
        return 1


# ------------------------------------------------------------------ entry point

_EPILOG = """\
KEYWORD is full-text syntax: `a b` = both, `"a b"` = phrase, `a*` = prefix,
`a -b` = a without b. Chinese matches inside words, so 忘机 finds 遗忘机制.

Sub-agent sessions are never listed or searched -- they contain no words of
yours. Searching needs the index and will build it if missing; listing does not
touch the index at all.
"""


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="kiro-resume",
        description="List recent Kiro sessions for this directory and resume one.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", "--limit", type=int, default=20,
                   help="show at most N sessions (default 20)")
    p.add_argument("--all-dirs", action="store_true",
                   help="sessions from every directory, not just this one")
    p.add_argument("-s", "--search", metavar="KEYWORD",
                   help="only sessions whose content matches; implies --all-dirs")
    p.add_argument("--sessions", help="source directory of session logs")
    p.add_argument("--db", help="index path (default ~/.cache/kiro-session-index)")
    args = p.parse_args(argv)

    sessions_dir = args.sessions or I._sessions_dir()
    if not os.path.isdir(sessions_dir):
        print(f"error: no session directory at {sessions_dir}", file=sys.stderr)
        return 2

    hits = None
    if args.search:
        args.all_dirs = True
        try:
            hits = count_hits(args.search, db=args.db, sessions_dir=args.sessions)
        except Exception as e:
            # Loud, and non-zero. The alternative -- grepping the raw logs -- cannot
            # match inside CJK words or identifiers, so it would answer "nothing
            # found" for things that were discussed at length.
            print(f"error: cannot search the index: {e}", file=sys.stderr)
            print("       the session list still works without it: kiro-resume",
                  file=sys.stderr)
            return 1

    rows = list_sessions(load_sessions(sessions_dir), cwd=os.getcwd(),
                         all_dirs=args.all_dirs, hits=hits, limit=args.limit)

    if not rows:
        if args.search:
            print(f"No sessions matching: {args.search}")
            print('(full-text syntax: one word, "a phrase", or a prefix like term*)')
        else:
            print(f"No sessions for {os.getcwd()}")
            print("(--all-dirs for every directory, -s KEYWORD to search content)")
        return 0

    display = format_rows(
        rows,
        cols=shutil.get_terminal_size((100, 24)).columns,
        show_cwd=args.all_dirs,
        show_hits=bool(args.search),
        # stderr, not stdout: stdout may be a pipe while the user still has a tty.
        color=sys.stderr.isatty())

    if args.search:
        print(f"Sessions matching '{args.search}':\n")

    picked = _choose(display)
    if picked is None:
        print("cancelled")
        return 0
    return _resume(rows[picked])
