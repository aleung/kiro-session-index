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

Tool output is excluded too, for the same reason one step further out: a command
that printed a word is not a discussion of it. It is not a small effect --
`pipeline` matches 139 sessions in prose and another 134 in command output alone,
`connection` 47 and 81 -- so including it roughly doubles the list with sessions
that merely logged the word, and a log that repeated it 400 times would sort
straight to the top. `ksi-query -t` is where that corpus belongs.

Searching shows a matched sentence under each session, because the title cannot
answer the question being asked of it. Titles are the opening prompt truncated to
about 50 characters, so they describe how a session *started*, and the thing you
searched for usually came up later: of the sessions matching `记忆` 0 of 16 had
the term in the title, `pipeline` 6 of 139, `vulnerability` 0 of 55. A list of
titles is a list of near-random labels with respect to the query.
"""

import argparse
import calendar
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata

from . import index as I
from . import query as Q
from . import text as T

# Context T.snip keeps either side of the match at query time, in characters. This is
# not a display width and is deliberately larger than any terminal we would cut to:
# it exists only to bound how much text a row carries, since the body it slices is
# already fully in memory. The cut that has to fit the screen happens at display time,
# in `window`, where the unit is columns -- the unit the terminal actually uses.
#
# An earlier version derived this from the terminal instead, which worked but put the
# screen's width into the query layer: snip counts characters and a CJK character costs
# two columns, so any single character width either leaves an ASCII snippet filling half
# a wide terminal or lets a CJK one overrun. That is a display problem and it is fixed
# where display happens.
SNIP_CONTEXT = 400

# Columns a snippet line gets even on an absurdly narrow terminal, and the width of
# the line number the numbered-menu path prefixes ('NN  ').
MIN_SNIPPET_ROOM = 20
LINENO_WIDTH = 4

# Rows shown before the picker opens. Searching gets a bigger budget because the cut
# happens *before* fzf sees the list, so anything past it cannot be reached by typing
# either -- and a search can easily match 139 sessions. Listing stays small: it is
# ordered by recency, so the tail is not what you came for.
DEFAULT_LIMIT_LIST = 20
DEFAULT_LIMIT_SEARCH = 50

SNIPPET_INDENT = "    "

# Intensity, not colour. UNDIM is SGR 22 ("normal intensity"), which is why the
# matched term is marked by clearing the dim rather than by adding bold -- see
# mark_terms.
DIM = "\033[2m"
UNDIM = "\033[22m"
RESET = "\033[0m"


def load_sessions(sessions_dir=None):
    """Every session's metadata, read from the .json sidecars: {session_id: meta}.

    Sessions with nothing in them are left out. A session file gets created when a
    session is opened, so an abandoned launch leaves a sidecar with no title beside a
    zero-byte log -- and resuming one lands you in a blank session, which is what
    starting a new one does anyway. They are not rare enough to ignore: 17 of 688
    top-level sessions, 5 of them inside the most recent 20 in one project. Measured
    over the whole corpus the two sets coincide exactly -- every untitled session had
    an empty log, and every empty log was untitled -- so this removes the noise and
    nothing else.

    Unreadable sidecars are skipped rather than fatal: one corrupt file must not make
    the whole list unavailable.
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
        if not has_content(path[: -len(".json")] + ".jsonl"):
            continue
        sid = meta.get("session_id") or os.path.splitext(os.path.basename(path))[0]
        out[sid] = meta
    return out


def has_content(log_path):
    """Did anything ever get said in this session?

    One stat, not a parse: the log is append-only, so a non-zero size means at least
    one record was written. Treating a missing log the same as an empty one is
    deliberate -- either way there is no conversation to go back to.
    """
    try:
        return os.path.getsize(log_path) > 0
    except OSError:
        return False


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


def snippet_room(cols):
    """Display columns the snippet line has to fill, on a terminal `cols` wide.

    Its own indent, and the line number the numbered-menu path prefixes to the first
    line of an item -- charged to both lines so the two stay aligned.
    """
    return max(cols - display_width(SNIPPET_INDENT) - LINENO_WIDTH, MIN_SNIPPET_ROOM)


def _fits(chars, budget):
    """How many leading characters of `chars` fit in `budget` columns: (count, width)."""
    n = w = 0
    for c in chars:
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if w + cw > budget:
            break
        w += cw
        n += 1
    return n, w


def window(s, terms, room):
    """Cut `s` to `room` display columns, keeping the matched term inside the result.

    `truncate` cannot do this job, and that is the whole reason this exists: it cuts
    from the right, so on a snippet carrying generous context the term -- which sits in
    the middle -- is the first thing to go. Cutting a window *around* the match is what
    makes it safe for the query layer to return more text than the screen can hold, and
    that is what lets the line be filled at any width.

    The term keeps its context on both sides, and whichever side runs out of text
    donates its share to the other, so a match near the beginning or the end of a
    sentence still fills the line rather than leaving it half empty.
    """
    if display_width(s) <= room:
        return s
    pos, hit = T.first_match(s, terms)
    if pos < 0:
        # Nothing to centre on: the snippet is being shown for its opening words.
        return truncate(s, room)
    end_of_hit = pos + len(hit)
    budget = room - 2 - display_width(s[pos:end_of_hit])   # 2 for the ellipses
    if budget < 0:
        # The term alone is wider than the line. Show its start; nothing else fits.
        return truncate(s[pos:], room)
    # Three passes, not two, so the donation works in both directions: the right side
    # is offered half, the left takes what the right left over, and the right is then
    # re-offered whatever the left could not use.
    _, right_w = _fits(s[end_of_hit:], budget // 2)
    left_n, left_w = _fits(reversed(s[:pos]), budget - right_w)
    right_n, _ = _fits(s[end_of_hit:], budget - left_w)
    start, stop = pos - left_n, end_of_hit + right_n
    return (("…" if start > 0 else "") + s[start:stop].strip()
            + ("…" if stop < len(s) else ""))


def project_of(cwd):
    """Just the last path component -- the project, not the route to it.

    In search mode the row has a matched sentence to carry, so the directory column
    earns only what it needs to answer "which project": a two-component path costs
    another ~20 columns and pushes the title into being sliced mid-word.
    """
    return os.path.basename((cwd or "").rstrip("/"))


def build_query(terms):
    """Command-line words to one full-text query string.

    Multiple words become AND, which is what build_match does with them. A single
    argument containing a space becomes a phrase, so the rule you have to remember
    is the shell's, not FTS5's: `kiro-resume 遗忘 机制` is two terms,
    `kiro-resume "遗忘 机制"` is a phrase.
    """
    return " ".join(f'"{t}"' if " " in t else t for t in terms)


def search_sessions(query, db=None, sessions_dir=None):
    """{session_id: {"hits": int, "snippet": str}} for everything that matches.

    Snippets come back wider than any screen; `window` cuts them to fit at display
    time. Nothing here needs to know how wide the terminal is.

    Refreshes the index first, and lets any failure propagate: the caller turns it
    into a loud error rather than a degraded search.
    """
    con, _refreshed = Q.open_index(db, auto_update=True, sessions_dir=sessions_dir)
    try:
        return Q.sessions_matching(con, query, snip_width=SNIP_CONTEXT)
    finally:
        con.close()


def mark_terms(text, terms, color=False):
    """Dim the whole snippet, at normal intensity only where the query matched.

    Contrast comes from *removing* the dim rather than adding bold: ANSI keeps bold
    and faint in one intensity slot and SGR 22 clears both, so bold nested inside
    faint is undefined and terminals disagree about it. Un-dimming has one meaning
    everywhere.

    One pass over a combined pattern, not one pass per term: replacing term by term
    would let a later term match the digits inside an escape code already inserted.
    """
    if not color:
        return text
    wanted = [t for t in dict.fromkeys(terms) if t]
    if not wanted:
        return DIM + text + RESET
    pattern = re.compile("|".join(re.escape(t) for t in wanted), re.IGNORECASE)
    return DIM + pattern.sub(lambda m: UNDIM + m.group(0) + DIM, text) + RESET



def list_sessions(sessions, cwd=None, all_dirs=False, matches=None, limit=20):
    """Resumable sessions as display-ready rows.

    Sub-agent sessions never appear. When `matches` is given, only sessions that
    matched appear, ordered by hit count and then recency; without it the order is
    purely recency.

    The two orders reflect two intents. Bare `kiro-resume` means "carry on with
    yesterday's work", where recent is what you want. `kiro-resume <words>` means
    "find the one about this", where relevance is. An earlier version served both at
    once by banding hit counts, a compromise made when the row showed nothing but a
    title; with a matched sentence on every row you can judge relevance yourself, and
    the banding was degenerate anyway -- prose-only counts have a median of 1 to 2, so
    for most queries nobody reached the band at all.
    """
    rows = []
    for sid, meta in sessions.items():
        if is_subagent(meta):
            continue
        if not all_dirs and meta.get("cwd") != cwd:
            continue
        match = matches.get(sid) if matches is not None else None
        if matches is not None and match is None:
            continue
        updated = meta.get("updated_at") or ""
        snippet = (match["snippet"] if match else "") or ""
        rows.append({
            "session_id": sid,
            "updated_at": updated,
            "age": age(updated),
            "title": (meta.get("title") or "(untitled)").replace("\t", " ")
                                                        .replace("\n", " "),
            "cwd": meta.get("cwd") or "",
            "hits": match["hits"] if match else 0,
            "snippet": snippet.replace("\t", " ").replace("\n", " "),
        })
    if matches is not None:
        rows.sort(key=lambda r: (r["hits"], r["updated_at"]), reverse=True)
    else:
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return rows[:limit]


def format_rows(rows, cols=100, show_cwd=False, searching=False, terms=(),
                color=False):
    """One item per row; searching makes each item two lines.

    First line: age, hit count, project, title. Second line, indented: the sentence
    the match was found in. Two lines rather than one because on one line they compete
    for the same ~54 columns left after the fixed fields, and neither survives it.
    The title alone cannot do this job -- it is the opening prompt truncated, so it
    describes how the session started, not why it matched.

    Widths are display columns, not characters, or a CJK row wraps and the list stops
    being scannable. The snippet is cut the same way; the match sits at its centre, so
    a cut costs trailing context rather than the thing you were looking for.
    """
    if color:
        dim, cyan, yellow, reset = DIM, "\033[36m", "\033[33m", RESET
    else:
        dim = cyan = yellow = reset = ""

    avail = cols - LINENO_WIDTH   # the caller prefixes a line number: 'NN  '
    room = snippet_room(cols)
    out = []
    for r in rows:
        used = 10
        age_part = f"{dim}{r['age']:<8s}  {reset}"
        hits_part = ""
        if searching:
            shown = (str(r["hits"]) + "x").rjust(5) + " "
            used += len(shown)
            hits_part = f"{yellow}{shown}{reset}"
        cwd_part = ""
        if searching or show_cwd:
            # Searching spends the width on the snippet instead, so the directory
            # column shrinks to the project name -- enough to answer "which one".
            shown = (project_of(r["cwd"]) if searching else short_path(r["cwd"])) + "  "
            used += display_width(shown)
            cwd_part = f"{cyan}{shown}{reset}"
        item = (age_part + hits_part + cwd_part
                + truncate(r["title"], max(avail - used, 10)))
        if searching and r.get("snippet"):
            item += "\n" + SNIPPET_INDENT + mark_terms(window(r["snippet"], terms, room),
                                                       terms, color)
        out.append(item)
    return out


# ------------------------------------------------------------------ selection

def _choose(display):
    """Index of the row the user picked, or None if they cancelled.

    Items are handed over as "<index>\x1f<item>" with only field 2 onward shown, and
    NUL-separated via --read0 so that an item may contain a newline and still be one
    entry. The field separator is deliberately a control character: titles routinely
    contain the pipe and box-drawing characters an earlier version used, which made
    the parse depend on the content being parsed.

    Typing filters against both lines, so a word you remember from the matched
    sentence narrows the list too, not just a word from the title.
    """
    if shutil.which("fzf"):
        payload = "".join(f"{i}\x1f{item}\x00" for i, item in enumerate(display))
        # stdout only. fzf draws its interface on stderr and writes just the chosen
        # line to stdout -- that split is what lets its output be piped. Capturing
        # stderr here leaves fzf waiting for keystrokes with nothing on screen, which
        # to the user is indistinguishable from a hang.
        # No --gap. In fzf 0.67 a gap is drawn as a dashed rule, and it costs a screen
        # row per session -- 7 sessions visible in a 24-row terminal instead of 10.
        # Nothing is lost by removing it: the snippet is indented and dimmed, and the
        # current-item highlight covers both of an entry's lines, so which line belongs
        # to which session stays unambiguous. (--gap=1 --gap-line= keeps the blank row
        # without the rule, if the density ever turns out to be too tight.)
        proc = subprocess.run(
            ["fzf", "--ansi", "--read0", "--no-sort",
             "--prompt=resume session> ",
             "--header=Select a session to resume (Esc to cancel)",
             "--delimiter=\x1f", "--with-nth=2.."],
            input=payload, stdout=subprocess.PIPE, text=True)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            return int(proc.stdout.split("\x1f", 1)[0])
        except ValueError:
            return None

    for i, item in enumerate(display, start=1):
        first, _, rest = item.partition("\n")
        print(f"{i:2d}  {first}")
        if rest:
            print(rest)
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
examples:
  kiro-resume                    sessions from this directory, newest first
  kiro-resume 记忆               sessions that discussed it, most matches first
  kiro-resume 记忆 遗忘          both words present
  kiro-resume "遗忘 机制"        that exact phrase
  kiro-resume 'token*'          words starting with token -- quoted, or the shell
                                expands it against your filenames first
  kiro-resume -- memory -pipeline
                                the first word, without the second

Searching implies --all-dirs, and shows the sentence each match was found in.
Only your side of the conversation is searched: not command output (use
`ksi-query -t`), and not sub-agent sessions, which contain no words of yours.
Searching needs the index and will build it if missing; listing does not touch it.
"""


class _Parser(argparse.ArgumentParser):
    """Turns the one confusing failure into a pointed one.

    `kiro-resume memory -pipeline` is a reasonable thing to type -- `-pipeline` is
    how full-text search spells "without this word" -- but to a command line it
    looks like an option name, and the stock message says only "unrecognized
    arguments", which does not tell you what to do about it.
    """

    def error(self, message):
        if "unrecognized arguments" in message and re.search(r"(^|\s)-\w", message):
            message += ("\nto search for a word without another, put the terms "
                        "after --:  kiro-resume -- memory -pipeline")
        super().error(message)


def main(argv=None):
    p = _Parser(
        prog="kiro-resume",
        description="List recent Kiro sessions and resume one. "
                    "With words, only sessions that discussed them.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("terms", nargs="*", metavar="WORD",
                   help="search terms; several mean all of them must appear")
    p.add_argument("-n", "--limit", type=int, default=None,
                   help=f"show at most N sessions (default {DEFAULT_LIMIT_LIST}, "
                        f"or {DEFAULT_LIMIT_SEARCH} when searching)")
    p.add_argument("--all-dirs", action="store_true",
                   help="sessions from every directory, not just this one")
    p.add_argument("--sessions", help="source directory of session logs")
    p.add_argument("--db", help="index path (default ~/.cache/kiro-session-index)")
    args = p.parse_args(argv)

    sessions_dir = args.sessions or I._sessions_dir()
    if not os.path.isdir(sessions_dir):
        print(f"error: no session directory at {sessions_dir}", file=sys.stderr)
        return 2

    searching = bool(args.terms)
    query = build_query(args.terms) if searching else ""
    terms = T.query_terms(query) if searching else ()
    limit = args.limit if args.limit is not None else (
        DEFAULT_LIMIT_SEARCH if searching else DEFAULT_LIMIT_LIST)

    matches = None
    if searching:
        args.all_dirs = True
        try:
            matches = search_sessions(query, db=args.db, sessions_dir=args.sessions)
        except Exception as e:
            # Loud, and non-zero. The alternative -- grepping the raw logs -- cannot
            # match inside CJK words or identifiers, so it would answer "nothing
            # found" for things that were discussed at length.
            print(f"error: cannot search the index: {e}", file=sys.stderr)
            print("       the session list still works without it: kiro-resume",
                  file=sys.stderr)
            return 1

    rows = list_sessions(load_sessions(sessions_dir), cwd=os.getcwd(),
                         all_dirs=args.all_dirs, matches=matches, limit=limit)

    if not rows:
        if searching:
            print(f"No sessions matching: {query}")
            print('(several words mean all must appear; "quote" a phrase; '
                  'term* is a prefix)')
        else:
            print(f"No sessions for {os.getcwd()}")
            print("(--all-dirs for every directory, or add words to search content)")
        return 0

    display = format_rows(
        rows,
        cols=shutil.get_terminal_size((100, 24)).columns,
        show_cwd=args.all_dirs,
        searching=searching,
        terms=terms,
        # stderr, not stdout: stdout may be a pipe while the user still has a tty.
        color=sys.stderr.isatty())

    if searching:
        print(f"Sessions matching {query}:\n")

    picked = _choose(display)
    if picked is None:
        print("cancelled")
        return 0
    return _resume(rows[picked])
