"""Query interface for kiro-session-index.

This is the ONLY entry point for SQL against the index. Query *shape* stays
completely free (--sql takes arbitrary SQL), but the execution *path* is fixed here
so that three failure modes -- each of which fails silently -- become impossible or
loud:

  1. Unsplit CJK query      -> returns 0 rows, looks like "never discussed it".
                               Fixed: build_match() transforms every query.
  2. Stale index            -> recent sessions missing, no warning.
                               Fixed: staleness is checked before every query.
  3. FTS5 snippet()         -> returns '' on a contentless table, silently.
                               Fixed: rejected outright; snip() provided instead.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time

from . import index as I
from . import text as T

# snippet()/highlight() cannot work on a contentless FTS table -- they return empty
# strings rather than erroring. Search terms arrive as bind parameters and never
# appear in the SQL text, so matching on the SQL string cannot false-positive.
_BANNED = re.compile(r"(?i)\b(snippet|highlight)\s*\(")


class QueryError(Exception):
    pass


def _register_snip(con):
    def _snip(*args):
        if len(args) < 2:
            return ""
        body, query = args[0], args[1]
        width = int(args[2]) if len(args) > 2 and args[2] is not None else 40
        return T.snip(body or "", T.query_terms(query or ""), width)

    con.create_function("snip", -1, _snip)


def open_index(db_path=None, auto_update=True, sessions_dir=None):
    """Open the index, refreshing it first unless explicitly told not to."""
    db_path = db_path or I._db_path()
    refreshed = None
    if auto_update:
        refreshed = I.update(db_path=db_path, sessions_dir=sessions_dir)
    elif not os.path.exists(db_path):
        raise FileNotFoundError(
            f"no index at {db_path} and --no-update was given -- run ksi-index first")
    con = I.connect(db_path)
    con.row_factory = sqlite3.Row
    _register_snip(con)
    return con, refreshed


def guard_sql(sql):
    m = _BANNED.search(sql or "")
    if m:
        raise QueryError(
            f"{m.group(1)}() cannot be used: this index uses contentless FTS5 tables, "
            f"where it silently returns an empty string. Use snip(text, query [, width]) "
            f"instead -- it slices the original text and renders CJK readably."
        )
    return sql


# ---------------------------------------------------------------- searches

def search_prose(con, query, limit=10, width=40, session=None, project=None,
                 since=None, role=None):
    match = T.build_match(query)
    sql = [
        "SELECT m.message_id, m.content_index, m.role, m.content_type, m.ts, m.text,",
        "       s.session_id, s.title, s.project, s.created_at, s.agent_name",
        "FROM messages_fts f",
        "JOIN messages m ON m.id = f.rowid",
        "LEFT JOIN sessions s ON s.session_id = m.session_id",
        "WHERE messages_fts MATCH ?",
    ]
    params = [match]
    if session:
        sql.append("AND m.session_id = ?")
        params.append(session)
    if project:
        sql.append("AND s.project = ?")
        params.append(project)
    if since:
        sql.append("AND m.ts >= ?")
        params.append(since)
    if role:
        sql.append("AND m.role = ?")
        params.append(role)
    sql.append("ORDER BY rank LIMIT ?")
    params.append(limit)

    terms = T.query_terms(query)
    out = []
    for r in con.execute("\n".join(sql), params):
        out.append({
            "citation": f"{r['message_id']}:{r['content_index']}",
            "session_id": r["session_id"],
            "title": r["title"],
            "project": r["project"],
            "role": r["role"],
            "content_type": r["content_type"],
            "ts": r["ts"],
            "when": _fmt_ts(r["ts"]) or (r["created_at"] or "")[:10],
            "snippet": T.snip(r["text"], terms, width),
        })
    return out


def search_tool_output(con, query, limit=10, width=40, tool=None, status=None,
                       project=None):
    match = T.build_match(query)
    sql = [
        "WITH hits AS (",
        "  SELECT rowid AS oid, rank AS rk FROM tool_output_fts",
        "  WHERE tool_output_fts MATCH ? ORDER BY rk LIMIT ?",
        ")",
        "SELECT h.oid, o.output, r.tool_use_id, r.session_id, r.tool_name, r.status,",
        "       s.title, s.project, c.purpose, c.path, h.rk",
        "FROM hits h",
        "JOIN tool_output o ON o.id = h.oid",
        "JOIN tool_results r ON r.output_id = h.oid",
        "LEFT JOIN sessions s ON s.session_id = r.session_id",
        "LEFT JOIN tool_calls c ON c.tool_use_id = r.tool_use_id",
        "WHERE 1=1",
    ]
    params = [match, limit]
    if tool:
        sql.append("AND r.tool_name = ?")
        params.append(tool)
    if status:
        sql.append("AND r.status = ?")
        params.append(status)
    if project:
        sql.append("AND s.project = ?")
        params.append(project)
    sql.append("ORDER BY h.rk")

    terms = T.query_terms(query)
    out = []
    for r in con.execute("\n".join(sql), params):
        out.append({
            "session_id": r["session_id"],
            "title": r["title"],
            "project": r["project"],
            "tool": r["tool_name"],
            "status": r["status"],
            "purpose": r["purpose"],
            "path": r["path"],
            "snippet": T.snip(r["output"], terms, width),
        })
    return out


def search_like(con, needle, limit=10, width=40, tool_output=False, prefilter=None):
    """Exact substring scan. Costs a full scan (~30 ms on 10 MB) and needs no index.

    This is the fallback for code substrings: unicode61 indexes 'DatabaseSync' as one
    token, so 'abaseSy' cannot be found via MATCH. Pass --with TOKEN to prefilter on
    a whole word first, which brings a scan down to sub-millisecond.
    """
    like = f"%{needle}%"
    params = []
    if tool_output:
        select = ("SELECT o.output AS body, r.session_id, r.tool_name, s.title,"
                  " s.project, NULL AS message_id, NULL AS content_index")
        joins = ["FROM tool_output o",
                 "JOIN tool_results r ON r.output_id = o.id"]
        if prefilter:
            joins.append("JOIN tool_output_fts f ON f.rowid = o.id")
        joins.append("LEFT JOIN sessions s ON s.session_id = r.session_id")
        where = []
        if prefilter:
            where.append("tool_output_fts MATCH ?")
            params.append(T.build_match(prefilter))
        where.append("o.output LIKE ?")
        params.append(like)
    else:
        select = ("SELECT m.text AS body, m.session_id, NULL AS tool_name, s.title,"
                  " s.project, m.message_id, m.content_index")
        joins = ["FROM messages m"]
        if prefilter:
            joins.append("JOIN messages_fts f ON f.rowid = m.id")
        joins.append("LEFT JOIN sessions s ON s.session_id = m.session_id")
        where = []
        if prefilter:
            where.append("messages_fts MATCH ?")
            params.append(T.build_match(prefilter))
        where.append("m.text LIKE ?")
        params.append(like)

    sql = "\n".join([select] + joins + ["WHERE " + " AND ".join(where), "LIMIT ?"])
    params.append(limit)

    out = []
    for r in con.execute(sql, params):
        cite = (f"{r['message_id']}:{r['content_index']}"
                if r["message_id"] else None)
        out.append({
            "citation": cite,
            "session_id": r["session_id"],
            "title": r["title"],
            "project": r["project"],
            "tool": r["tool_name"],
            "snippet": T.snip(r["body"], [needle], width),
        })
    return out


def count_by_session(con, query, tool_output=False):
    """Hits per session for a full-text query, with no LIMIT: {session_id: n}.

    The search_* functions take a global LIMIT and order by rank, which is right
    when the answer is a list of snippets and wrong when the question is "which
    sessions mention this at all" -- a few dominant sessions eat the whole budget.
    Measured on a 986-session corpus: `pipeline` under LIMIT 300 surfaced 105 of
    the 189 sessions that actually match, `session` 44 of 119. A caller that needs
    the complete set has to aggregate rather than truncate.

    Counting also skips snippet extraction, so for that purpose this is cheaper
    than the search it replaces rather than an extra cost.
    """
    match = T.build_match(query)
    if tool_output:
        sql = ("SELECT r.session_id AS session_id, COUNT(*) AS n "
               "FROM tool_output_fts f "
               "JOIN tool_results r ON r.output_id = f.rowid "
               "WHERE tool_output_fts MATCH ? "
               "GROUP BY r.session_id")
    else:
        sql = ("SELECT m.session_id AS session_id, COUNT(*) AS n "
               "FROM messages_fts f "
               "JOIN messages m ON m.id = f.rowid "
               "WHERE messages_fts MATCH ? "
               "GROUP BY m.session_id")
    return {r["session_id"]: r["n"] for r in con.execute(sql, [match])}


def run_sql(con, sql, limit=200):
    guard_sql(sql)
    cur = con.execute(sql)
    rows = cur.fetchmany(limit)
    cols = [d[0] for d in cur.description] if cur.description else []
    return cols, [list(r) for r in rows]


def status(con, db_path=None, sessions_dir=None):
    db_path = db_path or I._db_path()
    one = lambda q: con.execute(q).fetchone()[0]
    size = sum(os.path.getsize(db_path + s)
               for s in ("", "-wal", "-shm") if os.path.exists(db_path + s))
    stale = I.stale_files(con, sessions_dir)
    return {
        "db": db_path,
        "db_size_mb": round(size / 1e6, 1),
        "built_at": (con.execute("SELECT value FROM meta WHERE key='built_at'")
                     .fetchone() or [None])[0],
        "sessions": one("SELECT count(*) FROM sessions"),
        "messages": one("SELECT count(*) FROM messages"),
        "tool_calls": one("SELECT count(*) FROM tool_calls"),
        "tool_results": one("SELECT count(*) FROM tool_results"),
        "unique_outputs": one("SELECT count(*) FROM tool_output"),
        "subagent_sessions": one(
            "SELECT count(*) FROM sessions WHERE parent_session_id IS NOT NULL"),
        "stale_files": len(stale),
    }


# ---------------------------------------------------------------- cli

def _fmt_ts(ts):
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (ValueError, OSError):
        return ""


def _clip(value, width=60):
    s = " ".join(str(value).split())
    return s if len(s) <= width else s[: width - 1] + "…"


def _print_rows(rows, keys):
    if not rows:
        print("(no matches)")
        return
    for i, r in enumerate(rows, 1):
        head = " | ".join(_clip(r.get(k)) for k in keys if r.get(k))
        print(f"\n{i}. {head}")
        if r.get("citation"):
            print(f"   cite: {r['citation']}")
        if r.get("snippet"):
            print(f"   {r['snippet']}")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="ksi-query",
        description="Search the local index of Kiro CLI session logs.")
    p.add_argument("query", nargs="?", help="full-text query over conversation prose")
    p.add_argument("-t", "--tool", action="store_true",
                   help="search tool output instead of prose")
    p.add_argument("-l", "--like", metavar="SUBSTR",
                   help="exact substring scan (for code substrings MATCH cannot find)")
    p.add_argument("--with", dest="prefilter", metavar="TOKEN",
                   help="whole-word FTS prefilter to speed up --like")
    p.add_argument("--sql", metavar="SQL", help="arbitrary SQL (guarded)")
    p.add_argument("--status", action="store_true", help="index freshness and counts")
    p.add_argument("-n", "--limit", type=int, default=10)
    p.add_argument("-w", "--width", type=int, default=40, help="snippet context width")
    p.add_argument("--session", help="restrict to one session_id")
    p.add_argument("--project", help="restrict to one project")
    p.add_argument("--role", choices=["user", "assistant"])
    p.add_argument("--tool-name", help="restrict tool-output search to one tool")
    p.add_argument("--tool-status", choices=["success", "error"])
    p.add_argument("--since", metavar="YYYY-MM-DD")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--no-update", action="store_true",
                   help="skip the freshness check (may return stale results)")
    p.add_argument("--db", help="index path (default ~/.cache/kiro-session-index)")
    p.add_argument("--sessions", help="source directory of .jsonl logs")
    args = p.parse_args(argv)

    since = None
    if args.since:
        try:
            since = int(time.mktime(time.strptime(args.since, "%Y-%m-%d")))
        except ValueError:
            p.error("--since must be YYYY-MM-DD")

    try:
        con, refreshed = open_index(args.db, auto_update=not args.no_update,
                                   sessions_dir=args.sessions)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if refreshed and refreshed["files_indexed"] and not args.json:
        print(f"[index updated: {refreshed['files_indexed']} file(s), "
              f"{refreshed['elapsed_s']}s]", file=sys.stderr)

    try:
        if args.status:
            st = status(con, args.db, args.sessions)
            print(json.dumps(st, indent=2, ensure_ascii=False) if args.json
                  else "\n".join(f"{k:20} {v}" for k, v in st.items()))
            return 0

        if args.sql:
            cols, rows = run_sql(con, args.sql, args.limit)
            if args.json:
                print(json.dumps([dict(zip(cols, r)) for r in rows],
                                 indent=2, ensure_ascii=False, default=str))
            else:
                print(" | ".join(cols))
                for r in rows:
                    print(" | ".join("" if v is None else str(v) for v in r))
            return 0

        if args.like:
            rows = search_like(con, args.like, args.limit, args.width,
                               tool_output=args.tool, prefilter=args.prefilter)
            keys = ["title", "project", "tool"]
        elif args.tool:
            if not args.query:
                p.error("a query is required")
            rows = search_tool_output(con, args.query, args.limit, args.width,
                                      tool=args.tool_name, status=args.tool_status,
                                      project=args.project)
            keys = ["title", "project", "tool", "status", "purpose", "path"]
        else:
            if not args.query:
                p.print_help()
                return 1
            rows = search_prose(con, args.query, args.limit, args.width,
                                session=args.session, project=args.project,
                                since=since, role=args.role)
            keys = ["when", "title", "project", "role", "content_type"]

        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            _print_rows(rows, keys)
        return 0
    except (QueryError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except sqlite3.Error as e:
        print(f"sqlite error: {e}", file=sys.stderr)
        return 2
    finally:
        con.close()
