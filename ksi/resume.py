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

# The metadata column, carried down the left of both of an item's lines: when and how
# many on the first, which project on the second. Splitting it over two lines is what
# makes it affordable -- inline on one line the same three fields cost 37 columns, and
# they pushed the title to a different column on every row, since the project name's
# width varies. 19 columns fits 97.6% of project names in the corpus without truncation
# (12 alone would fit 74.6%), plus two of separation.
#
# What this buys is one content column with a stable left edge: the title and the
# sentence below it start at the same place on every row, which is what lets the list be
# read down rather than hunted through.
GUTTER = 21
PROJECT_CAP = GUTTER - 2

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

# Intensity marks one thing, hue marks the rest, and structure does the ranking.
#
# Dim is for the metadata gutter down the left -- when, how many, which project -- and
# nothing else. Everything that is content renders at the terminal's own brightness. Two
# other splits were tried on screen first and both failed: a bright title over a dim
# snippet puts the screen's largest contrast inside a single item, on the line you are
# *not* meant to read; dimming all of it instead reads as uniformly too faint. What makes
# the list scannable is the blank row between items, which is `_choose`'s job, not a
# brightness scale's.
#
# Three hues, each meaning one thing and appearing nowhere else: magenta the hit count,
# cyan the project, yellow the matched term. Magenta rather than green for the count
# because green and cyan are close enough to be confused in many themes, and those two
# sit one line apart in the same column.
#
# Only the dim attribute is used, never an explicit grey. A 256-colour ramp would give
# finer control -- and would let the snippet sit one step above the title, which dim
# cannot express -- but it assumes a dark background, and this is published code that
# has to survive a light theme.
#
# FG_DEFAULT is SGR 39, which resets the foreground colour and leaves intensity alone.
# That is what lets a coloured span be safe both inside a dim run and outside one -- the
# gutter relies on it to colour one field without ending the dim that covers the rest.
DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
YELLOW = "\033[33m"
FG_DEFAULT = "\033[39m"


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


def pad(s, width):
    """Cut to `width` display columns, then pad with spaces to exactly that many."""
    s = truncate(s, width)
    return s + " " * max(width - display_width(s), 0)


def gutter_width(cols):
    """Columns given to the metadata column on a terminal `cols` wide.

    It yields rather than squeezing the content: below about 45 columns there is not
    enough room for both, and a legible sentence beside a truncated project name beats
    a full project name beside four words.
    """
    return max(min(GUTTER, cols - LINENO_WIDTH - MIN_SNIPPET_ROOM), 0)


def snippet_room(cols):
    """Display columns the content column has to fill, on a terminal `cols` wide.

    The metadata gutter, and the line number the numbered-menu path prefixes to an
    item -- charged to both of its lines, so the two stay aligned with each other.
    """
    return max(cols - LINENO_WIDTH - gutter_width(cols), MIN_SNIPPET_ROOM)


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
    """Colour the matched terms, leaving everything else exactly as it is.

    Three things this deliberately does not do, each having been tried and rejected by
    looking at the result.

    It does not brighten the match. Un-dimming made it as bright as the title, and a
    term can occur three or four times in a sentence, so it scattered bright spots at
    unpredictable positions along the line.

    It does not dim the line it sits in. The snippet used to be dim under a
    normal-intensity title, which put the screen's largest contrast between the two
    lines of a single item -- pulling the eye off the line that says why the session
    matched and onto the one that is only the opening prompt truncated.

    And it does not dim everything instead, which was the next thing tried: with no
    contrast left the whole list read as too faint. What actually made the list
    scannable was separating the items (see _choose) rather than ranking them by
    brightness. So intensity now says one thing only -- the metadata gutter is dim, the
    content is not -- and hue is what marks meaning.

    The span closes with SGR 39 (default foreground), which resets the colour without
    touching intensity, so this is safe to use inside a dim run as well as outside one.

    One pass over a combined pattern, not one pass per term: replacing term by term
    would let a later term match the digits inside an escape code already inserted.
    """
    if not color:
        return text
    wanted = [t for t in dict.fromkeys(terms) if t]
    if not wanted:
        return text
    pattern = re.compile("|".join(re.escape(t) for t in wanted), re.IGNORECASE)
    return pattern.sub(lambda m: YELLOW + m.group(0) + FG_DEFAULT, text)



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

    Intensity says one thing: the metadata gutter on the left -- age and hit count --
    is dim, and the content is not. Hue says the rest: cyan for "which project", yellow
    for "this is why it matched".

    Getting here took ruling out both ends. Ranking by brightness within an item does
    not work, because a list applies it to every row: twenty bright titles are a wall
    rather than a landmark, and a bright title above a dim snippet puts the screen's
    largest contrast inside one item, on the wrong line of it. Dimming everything
    instead reads as too faint. What separates the items is the blank row between them
    (see _choose), which is a structural job, and once structure does it there is
    nothing left for brightness to do.

    The current row is marked by fzf's background band, which is enough on its own once
    its bold is off -- and it works here because the content carries no dim of its own
    for fzf's `fg+` to lose against.
    """
    if color:
        dim, cyan, magenta, fg, reset = DIM, CYAN, MAGENTA, FG_DEFAULT, RESET
    else:
        dim = cyan = magenta = fg = reset = ""

    avail = cols - LINENO_WIDTH   # the caller prefixes a line number: 'NN  '
    out = []
    for r in rows:
        if searching:
            out.append(_search_row(r, cols, terms, color,
                                   dim, cyan, magenta, fg, reset))
            continue
        # Listing has no sentence to show, so it stays one line and spends the width
        # on a fuller path than the project name searching can afford.
        used = 10
        item = f"{dim}{r['age']:<8s}  {reset}"
        if show_cwd:
            shown = short_path(r["cwd"]) + "  "
            used += display_width(shown)
            item += f"{cyan}{shown}{reset}"
        out.append(item + truncate(r["title"], max(avail - used, 10)))
    return out


def _search_row(r, cols, terms, color, dim, cyan, magenta, fg, reset):
    """One matching session as two lines: metadata gutter left, content right.

    The gutter carries when and how many on the first line and which project on the
    second, so the content column keeps a single left edge on both -- the title and
    the sentence that says why the session matched start at the same column, on every
    row. Inline on one line these three fields cost 37 columns and, because a project
    name's width varies, put the title in a different place on each row.
    """
    gut = gutter_width(cols)
    room = snippet_room(cols)

    # The hit count is the one field with a hue, because it is the sort key: rows come
    # ordered by it, and telling it from the date beside it should not need reading.
    # fg, not reset, closes it -- the dim has to survive to the end of the gutter.
    hits = f"{r['hits']}x"
    # Age padded to a fixed width, or the count lands in a different column on every
    # row -- '120d ago  14x' against '35d ago  12x' -- and a ragged column of numbers
    # is exactly what a hue was added to stop you having to read.
    when = f"{r['age']:<8s}  "
    spent = display_width(when) + display_width(hits)
    top = (when + magenta + hits + fg + " " * max(gut - spent, 0)
           if spent <= gut else pad(when + hits, gut))

    item = f"{dim}{top}{reset}" + truncate(r["title"], room)
    if r.get("snippet"):
        where = pad(truncate(project_of(r["cwd"]), PROJECT_CAP), gut)
        item += ("\n" + f"{dim}{cyan}{where}{reset}"
                 + mark_terms(window(r["snippet"], terms, room), terms, color))
    return item


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
        # A blank row between items, which reverses part of bdf17e2. That commit
        # dropped --gap to buy density -- 10 sessions visible in a 24-row terminal
        # instead of 7 -- on the argument that the indent and the dim already said
        # which line belonged to which session. They do, read one item at a time; what
        # they do not do is let you *scan*, because 20 items with no separation is one
        # block of text and the eye finds no edges in it. Density was the wrong thing
        # to spend the row on.
        # --gap-line= is what keeps it a blank row: --gap=1 alone draws a dashed rule
        # in fzf 0.67, which is the form bdf17e2 rejected and rightly so.
        proc = subprocess.run(
            ["fzf", "--ansi", "--read0", "--no-sort",
             "--gap=1", "--gap-line=",
             "--prompt=resume session> ",
             "--header=Select a session to resume (Esc to cancel)",
             # The current item is marked by its background band alone. fzf also bolds
             # it by default, which is a third intensity on top of the two this list
             # already uses, and it lands on text whose weight is carrying meaning --
             # so the current row stops looking like the other rows just as you are
             # scanning past it. Measured on the wire: the current item's own line goes
             # from `1;38;5;254;48;5;236` to `38;5;254;48;5;236`, keeping the band; its
             # snippet line goes from `1;2;...` -- bold nested inside faint, which ANSI
             # leaves undefined -- to a plain `2`.
             # `--highlight-line` is not this. It widens the band and keeps the bold.
             "--color=fg+:regular",
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
            # The line number is charged to both lines, or the gutter on the second
            # one starts four columns left of the first and the item stops being a
            # block. snippet_room already leaves room for it.
            print(" " * LINENO_WIDTH + rest)
        print()          # separated like the fzf path, for the same reason
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
