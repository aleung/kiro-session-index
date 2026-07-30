"""Incremental indexer for Kiro CLI session logs.

Strategy: the cursor is (mtime, size) per source file, deliberately coarse. When a
file changes, its ENTIRE session is deleted and re-parsed, so the result is always
byte-for-byte equivalent to a full rebuild. There is no byte offset to drift out of
sync and silently drop records.

Never writes to ~/.kiro/sessions -- the source is treated as read-only truth.
"""

import glob
import hashlib
import json
import os
import sqlite3
import time

from . import text as T

DEFAULT_SESSIONS = os.path.expanduser("~/.kiro/sessions/cli")
DEFAULT_DB = os.path.expanduser("~/.cache/kiro-session-index/index.db")
SCHEMA_VERSION = "2"

# FileRead output duplicates files that still exist on disk, where ripgrep searches
# them better and fresher. 12.76 MB of the 33.6 MB of tool output, for near-zero value.
SKIP_TOOLS = {"FileRead"}

# Record kinds that must never become messages:
#   Compaction -- messages_snapshot is a duplicate copy of earlier messages.
SKIP_KINDS = {"Compaction"}


def _sessions_dir():
    return os.environ.get("KSI_SESSIONS", DEFAULT_SESSIONS)


def _db_path():
    return os.environ.get("KSI_DB", DEFAULT_DB)


def _harden(path):
    """Owner-only permissions on the DB and its WAL sidecars.

    The sidecars matter: they hold recently written page data and are easy to forget.
    """
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass


def connect(db_path=None, create=True):
    db_path = db_path or _db_path()
    d = os.path.dirname(db_path)
    if create and d:
        os.makedirs(d, mode=0o700, exist_ok=True)
    elif not os.path.exists(db_path):
        raise FileNotFoundError(
            f"index not found at {db_path} -- run ksi-index to build it"
        )
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")  # safe: the index is a rebuildable cache
    if create:
        with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
            con.executescript(f.read())
        con.commit()
        _harden(db_path)
    return con


def _leaf_text(obj):
    """Collect every string leaf, joined by newline.

    Reads only results[tool_use_id], the internal encoding. The model-facing
    content[].toolResult carries the same payload in a different wrapper -- indexing
    both would double the corpus for zero extra recall.
    """
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(_leaf_text(v) for v in obj.values())
    if isinstance(obj, list):
        return "\n".join(_leaf_text(v) for v in obj)
    if obj is None or isinstance(obj, bool):
        return ""
    return str(obj)


def _tool_name(tool):
    kind = (tool or {}).get("kind")
    if isinstance(kind, dict):
        builtin = kind.get("BuiltIn")
        if isinstance(builtin, dict) and builtin:
            return next(iter(builtin))
        if isinstance(builtin, str):
            return builtin
        if kind:
            return next(iter(kind))
    if isinstance(kind, str):
        return kind
    return "Unknown"


def _result_status(result):
    if isinstance(result, dict):
        for k in ("Success", "Ok"):
            if k in result:
                return "success"
        for k in ("Error", "Err", "Failure"):
            if k in result:
                return "error"
    return None


class Indexer:
    def __init__(self, con):
        self.con = con
        self.next_msg = (
            con.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0] + 1
        )
        self.next_out = (
            con.execute("SELECT COALESCE(MAX(id), 0) FROM tool_output").fetchone()[0] + 1
        )
        self.sha_ids = dict(con.execute("SELECT sha1, id FROM tool_output"))

    # ---------- deletion ----------

    def drop_session(self, session_id):
        """Remove a session completely, including its contentless FTS rows.

        Contentless FTS tables reject DELETE unless created with
        contentless_delete=1 -- see schema.sql.
        """
        con = self.con
        ids = [r[0] for r in con.execute(
            "SELECT id FROM messages WHERE session_id=?", (session_id,))]
        con.executemany("DELETE FROM messages_fts WHERE rowid=?", ((i,) for i in ids))
        con.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        con.execute("DELETE FROM tool_calls WHERE session_id=?", (session_id,))
        con.execute("DELETE FROM tool_results WHERE session_id=?", (session_id,))
        con.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        con.execute("DELETE FROM files WHERE session_id=?", (session_id,))

    def gc_outputs(self):
        """Drop content-addressed payloads no tool_result references any more."""
        con = self.con
        orphans = [r[0] for r in con.execute(
            "SELECT o.id FROM tool_output o "
            "LEFT JOIN tool_results r ON r.output_id = o.id "
            "WHERE r.tool_use_id IS NULL"
        )]
        if not orphans:
            return 0
        con.executemany("DELETE FROM tool_output_fts WHERE rowid=?", ((i,) for i in orphans))
        con.executemany("DELETE FROM tool_output WHERE id=?", ((i,) for i in orphans))
        for i in orphans:
            self.sha_ids = {k: v for k, v in self.sha_ids.items() if v != i}
        return len(orphans)

    # ---------- insertion ----------

    def _output_id(self, payload):
        """Content-address a payload: index it once, reference it from many sessions."""
        sha = hashlib.sha1(payload.encode("utf8", "replace")).hexdigest()
        existing = self.sha_ids.get(sha)
        if existing is not None:
            return existing
        oid = self.next_out
        self.next_out += 1
        self.con.execute("INSERT INTO tool_output(id, sha1, output) VALUES(?,?,?)",
                         (oid, sha, payload))
        self.con.execute("INSERT INTO tool_output_fts(rowid, search_text) VALUES(?,?)",
                         (oid, T.split_index(payload)))
        self.sha_ids[sha] = oid
        return oid

    def index_sidecar(self, session_id, sidecar):
        """Session metadata from <session-id>.json.

        Reads only the few fields needed. Deliberately ignores
        session_state.conversation_metadata: metering_usage is always [] (there is no
        cost data), token counts are always 0, and turn.result duplicates message
        content. Coverage is a stable ~23%, so aggregates over it would mislead.
        """
        if not os.path.exists(sidecar):
            self.con.execute(
                "INSERT OR REPLACE INTO sessions(session_id) VALUES(?)", (session_id,))
            return
        try:
            with open(sidecar, encoding="utf8", errors="replace") as f:
                j = json.load(f)
        except (OSError, ValueError):
            self.con.execute(
                "INSERT OR REPLACE INTO sessions(session_id) VALUES(?)", (session_id,))
            return
        state = j.get("session_state") if isinstance(j.get("session_state"), dict) else {}
        model_info = (state.get("rts_model_state") or {}).get("model_info") or {}
        cwd = j.get("cwd") or ""
        self.con.execute(
            "INSERT OR REPLACE INTO sessions(session_id, cwd, project, title, created_at,"
            " updated_at, created_reason, parent_session_id, model, agent_name)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                j.get("session_id") or session_id,
                cwd,
                os.path.basename(cwd.rstrip("/")) or None,
                j.get("title"),
                j.get("created_at"),
                j.get("updated_at"),
                j.get("session_created_reason"),
                j.get("parent_session_id"),
                model_info.get("model_name"),
                state.get("agent_name"),
            ),
        )

    def index_file(self, path):
        """Parse one .jsonl into the database. Assumes the session was dropped first."""
        session_id = os.path.basename(path)[: -len(".jsonl")]
        self.index_sidecar(session_id, path[: -len(".jsonl")] + ".json")

        msgs, fts, calls, results = [], [], [], []
        seq = 0
        last_ts = None
        bad_lines = 0

        with open(path, encoding="utf8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    bad_lines += 1
                    continue
                kind = rec.get("kind")
                data = rec.get("data")
                if kind in SKIP_KINDS or not isinstance(data, dict):
                    continue

                meta = data.get("meta")
                if isinstance(meta, dict) and meta.get("timestamp"):
                    last_ts = meta["timestamp"]  # epoch SECONDS
                message_id = data.get("message_id")

                if kind == "Clear":
                    seq += 1
                    msgs.append((self.next_msg, session_id, message_id, 0, seq,
                                 "Clear", None, "clear", None, last_ts))
                    self.next_msg += 1
                    continue

                if kind == "ToolResults":
                    for tool_use_id, val in (data.get("results") or {}).items():
                        if not isinstance(val, dict):
                            continue
                        name = _tool_name(val.get("tool"))
                        if name in SKIP_TOOLS:
                            continue
                        payload = _leaf_text(val.get("result"))
                        if not payload.strip():
                            continue
                        results.append((tool_use_id, session_id, message_id, name,
                                        _result_status(val.get("result")), payload))
                    continue

                role = "user" if kind == "Prompt" else "assistant"
                for idx, item in enumerate(data.get("content") or []):
                    if not isinstance(item, dict):
                        continue
                    ctype = item.get("kind")
                    payload = item.get("data")
                    body = None

                    if ctype == "text" and isinstance(payload, str):
                        body = payload
                    elif ctype == "thinking" and isinstance(payload, dict):
                        # .signature is a cryptographic blob: never indexed, never stored.
                        body = payload.get("text")
                    elif ctype == "toolUse" and isinstance(payload, dict):
                        args = payload.get("input")
                        args = args if isinstance(args, dict) else {}
                        seq += 1
                        calls.append((
                            payload.get("toolUseId"), session_id, message_id, seq,
                            payload.get("name"),
                            args.get("__tool_use_purpose"),
                            args.get("path") or args.get("file_path"),
                            json.dumps(args, ensure_ascii=False),
                        ))
                        # Only the agent-written purpose is prose; file bodies in
                        # newStr/oldStr/content stay in input_json, unindexed.
                        body = args.get("__tool_use_purpose")

                    if not body:
                        continue
                    seq += 1
                    mid = self.next_msg
                    self.next_msg += 1
                    msgs.append((mid, session_id, message_id, idx, seq, kind, role,
                                 ctype, body, last_ts))
                    fts.append((mid, T.split_index(body)))

        con = self.con
        con.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)", msgs)
        con.executemany("INSERT INTO messages_fts(rowid, search_text) VALUES(?,?)", fts)
        con.executemany(
            "INSERT OR REPLACE INTO tool_calls VALUES(?,?,?,?,?,?,?,?)", calls)
        for tool_use_id, sid, message_id, name, status, payload in results:
            con.execute(
                "INSERT OR REPLACE INTO tool_results VALUES(?,?,?,?,?,?)",
                (tool_use_id, sid, message_id, name, status, self._output_id(payload)),
            )

        st = os.stat(path)
        con.execute(
            "INSERT OR REPLACE INTO files(path, session_id, mtime, size, indexed_at)"
            " VALUES(?,?,?,?,?)",
            (path, session_id, st.st_mtime, st.st_size,
             time.strftime("%Y-%m-%dT%H:%M:%S")),
        )
        return {"messages": len(msgs), "tool_calls": len(calls),
                "tool_results": len(results), "bad_lines": bad_lines}


def stale_files(con, sessions_dir=None):
    """Source files that are new or changed since they were indexed."""
    sessions_dir = sessions_dir or _sessions_dir()
    known = {p: (m, s) for p, m, s in
             con.execute("SELECT path, mtime, size FROM files")}
    out = []
    for path in sorted(glob.glob(os.path.join(sessions_dir, "*.jsonl"))):
        try:
            st = os.stat(path)
        except OSError:
            continue
        prev = known.get(path)
        if prev is None or abs(prev[0] - st.st_mtime) > 1e-6 or prev[1] != st.st_size:
            out.append(path)
    return out


def vanished_sessions(con, sessions_dir=None):
    sessions_dir = sessions_dir or _sessions_dir()
    present = set(glob.glob(os.path.join(sessions_dir, "*.jsonl")))
    return [(p, s) for p, s in con.execute("SELECT path, session_id FROM files")
            if p not in present]


def _schema_outdated(db_path):
    """True if an existing index was built by a different schema version.

    Schema changes cannot be applied to an existing file by CREATE ... IF NOT EXISTS,
    so a version mismatch forces a rebuild rather than leaving a subtly wrong index.
    """
    if not os.path.exists(db_path):
        return False
    try:
        con = sqlite3.connect(db_path)
        row = con.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        con.close()
    except sqlite3.Error:
        return True
    return (row[0] if row else None) != SCHEMA_VERSION


def update(db_path=None, sessions_dir=None, full=False, verbose=False):
    """Bring the index in line with the source directory. Returns a summary dict."""
    db_path = db_path or _db_path()
    sessions_dir = sessions_dir or _sessions_dir()
    started = time.time()
    rebuilt_for_schema = False
    if not full and _schema_outdated(db_path):
        full = rebuilt_for_schema = True
    if full:
        # Rebuilding by removing the file is safer than DELETE FROM on contentless
        # FTS tables, and guarantees the result is a clean build.
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except FileNotFoundError:
                pass
    con = connect(db_path)
    try:
        idx = Indexer(con)
        targets = stale_files(con, sessions_dir)
        gone = vanished_sessions(con, sessions_dir)

        for _, session_id in gone:
            idx.drop_session(session_id)

        totals = {"messages": 0, "tool_calls": 0, "tool_results": 0, "bad_lines": 0}
        for path in targets:
            session_id = os.path.basename(path)[: -len(".jsonl")]
            idx.drop_session(session_id)
            stats = idx.index_file(path)
            for k in totals:
                totals[k] += stats[k]
            if verbose:
                print(f"  indexed {session_id} "
                      f"({stats['messages']} msgs, {stats['tool_results']} results)")

        removed = idx.gc_outputs()
        con.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)",
                    (SCHEMA_VERSION,))
        con.execute("INSERT OR REPLACE INTO meta VALUES('built_at', ?)",
                    (time.strftime("%Y-%m-%dT%H:%M:%S"),))
        con.commit()

        if targets:
            con.execute("INSERT INTO messages_fts(messages_fts) VALUES('optimize')")
            con.execute("INSERT INTO tool_output_fts(tool_output_fts) VALUES('optimize')")
            con.execute("ANALYZE")
            con.commit()
    finally:
        con.close()
    _harden(db_path)

    return {
        "files_indexed": len(targets),
        "sessions_removed": len(gone),
        "outputs_gc": removed,
        "schema_rebuild": rebuilt_for_schema,
        "elapsed_s": round(time.time() - started, 2),
        **totals,
    }
