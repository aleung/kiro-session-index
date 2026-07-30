"""Unit tests for ksi.index -- parsing rules, exclusions, and incremental behaviour."""

import hashlib
import os
import sqlite3
import stat
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ksi import index as I
from ksi import text as T
from tests import fixtures as F


def _rows(con, sql, *params):
    """Sorted rows, NULL-safe (session logs legitimately contain NULLs)."""
    return sorted(con.execute(sql, params).fetchall(),
                  key=lambda row: tuple("" if v is None else str(v) for v in row))


def snapshot(db_path):
    """Content-only snapshot, ignoring volatile ids and timestamps.

    Rowids and tool_output ids are assigned in insertion order and legitimately
    differ between an incremental update and a full rebuild, so equivalence has to
    be judged on content.
    """
    con = sqlite3.connect(db_path)
    out = {}
    out["messages"] = _rows(con,
        "SELECT session_id, message_id, content_index, seq, kind, role,"
        " content_type, text, ts FROM messages")
    out["tool_calls"] = _rows(con,
        "SELECT tool_use_id, session_id, message_id, seq, name, purpose, path,"
        " input_json FROM tool_calls")
    out["tool_results"] = _rows(con,
        "SELECT r.tool_use_id, r.session_id, r.tool_name, r.status, o.sha1"
        " FROM tool_results r JOIN tool_output o ON o.id = r.output_id")
    out["tool_output"] = _rows(con, "SELECT sha1, output FROM tool_output")
    out["sessions"] = _rows(con,
        "SELECT session_id, cwd, project, title, created_at, updated_at,"
        " created_reason, parent_session_id, model, agent_name FROM sessions")
    # FTS content, expressed as citations so it is independent of rowids.
    out["fts_hits"] = _rows(con,
        "SELECT m.message_id, m.content_index FROM messages_fts f"
        " JOIN messages m ON m.id = f.rowid WHERE messages_fts MATCH ?",
        T.build_match("遗忘机制"))
    out["fts_total"] = con.execute("SELECT count(*) FROM messages_fts").fetchone()[0]
    out["tool_fts_total"] = con.execute(
        "SELECT count(*) FROM tool_output_fts").fetchone()[0]
    con.close()
    return out


def fts_search(db_path, query, tool=False):
    con = sqlite3.connect(db_path)
    table = "tool_output_fts" if tool else "messages_fts"
    n = con.execute(f"SELECT count(*) FROM {table} WHERE {table} MATCH ?",
                    (T.build_match(query),)).fetchone()[0]
    con.close()
    return n


def raw_dump(db_path):
    """Every stored string in the database, for leak assertions."""
    con = sqlite3.connect(db_path)
    chunks = []
    for (name,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"):
        try:
            cur = con.execute(f"SELECT * FROM '{name}'")
        except sqlite3.Error:
            continue
        for row in cur:
            chunks.append(" ".join("" if v is None else str(v) for v in row))
    con.close()
    return "\n".join(chunks)


class IndexerCase(unittest.TestCase):
    def setUp(self):
        self.c = F.Corpus(3)
        self.c.__enter__()
        self.stats = I.update(db_path=self.c.db, sessions_dir=self.c.root, full=True)

    def tearDown(self):
        self.c.__exit__()

    def q(self, sql, *params):
        con = sqlite3.connect(self.c.db)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def one(self, sql, *params):
        return self.q(sql, *params)[0][0]


class TestBasicIndexing(IndexerCase):
    def test_all_sessions_indexed(self):
        self.assertEqual(self.one("SELECT count(*) FROM sessions"), 3)
        self.assertEqual(self.stats["files_indexed"], 3)

    def test_files_table_tracks_each_source(self):
        self.assertEqual(self.one("SELECT count(*) FROM files"), 3)

    def test_prose_is_searchable(self):
        self.assertGreater(fts_search(self.c.db, "遗忘机制"), 0)

    def test_fts_row_count_matches_indexed_messages(self):
        # Clear markers carry no text and must not occupy an FTS row.
        with_text = self.one("SELECT count(*) FROM messages WHERE text IS NOT NULL")
        self.assertEqual(self.one("SELECT count(*) FROM messages_fts"), with_text)

    def test_session_metadata_is_captured(self):
        row = self.q("SELECT project, agent_name, model, parent_session_id,"
                     " created_reason FROM sessions WHERE session_id='sess0001'")[0]
        self.assertEqual(row[0], "proj1")
        self.assertEqual(row[1], "test-agent")
        self.assertEqual(row[2], "claude-test")
        self.assertEqual(row[3], "sess0000")
        self.assertEqual(row[4], "subagent")

    def test_tool_call_purpose_and_path_recorded(self):
        row = self.q("SELECT name, purpose FROM tool_calls"
                     " WHERE tool_use_id='sess0000-t1'")[0]
        self.assertEqual(row[0], "shell")
        self.assertIn("decay mechanism", row[1])

    def test_purpose_is_full_text_searchable(self):
        # __tool_use_purpose is agent-written intent: the highest-value target.
        self.assertGreater(fts_search(self.c.db, "decay mechanism"), 0)


class TestExclusions(IndexerCase):
    def test_thinking_signature_never_stored(self):
        self.assertNotIn(F.SIG, raw_dump(self.c.db))

    def test_thinking_text_is_kept(self):
        self.assertGreater(fts_search(self.c.db, "记忆系统"), 0)

    def test_fileread_output_is_skipped(self):
        self.assertNotIn("FILEREAD_PAYLOAD_MUST_BE_SKIPPED", raw_dump(self.c.db))
        self.assertEqual(
            self.one("SELECT count(*) FROM tool_results WHERE tool_name='FileRead'"), 0)

    def test_compaction_snapshot_is_skipped(self):
        # A duplicate copy of earlier messages: indexing it would double hits.
        self.assertNotIn("COMPACTION_DUPLICATE_TEXT", raw_dump(self.c.db))

    def test_turn_result_duplicate_is_ignored(self):
        # session_state.conversation_metadata is not read at all.
        self.assertNotIn("TURN_RESULT_DUPLICATE", raw_dump(self.c.db))

    def test_written_file_body_is_not_full_text_indexed(self):
        needle = "BODY_OF_A_WRITTEN_FILE_SHOULD_NOT_BE_SEARCHABLE"
        self.assertEqual(fts_search(self.c.db, needle), 0)

    def test_written_file_body_is_still_retrievable_from_input_json(self):
        blob = self.one("SELECT input_json FROM tool_calls"
                        " WHERE tool_use_id='sess0000-t1'")
        self.assertIn("BODY_OF_A_WRITTEN_FILE_SHOULD_NOT_BE_SEARCHABLE", blob)


class TestDeduplication(IndexerCase):
    def test_identical_output_stored_once(self):
        sha = hashlib.sha1(F.DUPLICATE_OUTPUT.encode()).hexdigest()
        self.assertEqual(
            self.one("SELECT count(*) FROM tool_output WHERE sha1=?", sha), 1)

    def test_but_referenced_from_every_session(self):
        """Dedup must not cost recall: all three occurrences stay discoverable."""
        n = self.one(
            "SELECT count(*) FROM tool_results r JOIN tool_output o"
            " ON o.id = r.output_id WHERE o.sha1=?",
            hashlib.sha1(F.DUPLICATE_OUTPUT.encode()).hexdigest())
        self.assertEqual(n, 3)

    def test_tool_output_searchable(self):
        self.assertGreater(fts_search(self.c.db, "connection refused", tool=True), 0)

    def test_double_encoding_not_indexed_twice(self):
        # results[tid] and content[].toolResult carry the same payload.
        self.assertEqual(fts_search(self.c.db, "connection refused", tool=True), 1)


class TestCitationAnchor(IndexerCase):
    def test_citation_is_unique(self):
        total = self.one("SELECT count(*) FROM messages WHERE message_id IS NOT NULL")
        distinct = self.one(
            "SELECT count(*) FROM (SELECT DISTINCT message_id, content_index"
            " FROM messages WHERE message_id IS NOT NULL)")
        self.assertEqual(total, distinct)

    def test_citation_survives_reindexing(self):
        before = set(self.q("SELECT message_id, content_index FROM messages"
                            " WHERE message_id IS NOT NULL"))
        I.update(db_path=self.c.db, sessions_dir=self.c.root, full=True)
        after = set(self.q("SELECT message_id, content_index FROM messages"
                           " WHERE message_id IS NOT NULL"))
        self.assertEqual(before, after)


class TestTimestamps(IndexerCase):
    def test_prompt_timestamp_stored_as_epoch_seconds(self):
        ts = self.one("SELECT ts FROM messages WHERE message_id='sess0000-m1'")
        self.assertEqual(ts, 1776350561)

    def test_assistant_timestamp_carried_forward(self):
        # AssistantMessage records carry no timestamp of their own.
        ts = self.one("SELECT ts FROM messages WHERE message_id='sess0000-m2'"
                      " AND content_type='text'")
        self.assertEqual(ts, 1776350561)


class TestClearMarker(IndexerCase):
    def test_clear_recorded_as_boundary(self):
        self.assertEqual(self.one(
            "SELECT count(*) FROM messages WHERE content_type='clear'"), 3)

    def test_clear_carries_no_text(self):
        self.assertIsNone(self.one(
            "SELECT text FROM messages WHERE content_type='clear' LIMIT 1"))


class TestIncremental(IndexerCase):
    def test_unchanged_corpus_is_a_no_op(self):
        stats = I.update(db_path=self.c.db, sessions_dir=self.c.root)
        self.assertEqual(stats["files_indexed"], 0)

    def test_appended_session_is_picked_up(self):
        self.c.append_line("sess0001", "新增的一句话 appended")
        stats = I.update(db_path=self.c.db, sessions_dir=self.c.root)
        self.assertEqual(stats["files_indexed"], 1)
        self.assertGreater(fts_search(self.c.db, "新增的一句话"), 0)

    def test_incremental_equals_full_rebuild(self):
        """The core correctness guarantee of the per-file cursor design."""
        self.c.append_line("sess0002", "增量与全量必须一致 equivalence")
        I.update(db_path=self.c.db, sessions_dir=self.c.root)
        incremental = snapshot(self.c.db)

        rebuilt = self.c.db + ".full"
        I.update(db_path=rebuilt, sessions_dir=self.c.root, full=True)
        full = snapshot(rebuilt)

        for key in sorted(full):
            self.assertEqual(incremental[key], full[key],
                             f"incremental differs from full rebuild in {key}")

    def test_no_duplicate_rows_after_repeated_updates(self):
        before = self.one("SELECT count(*) FROM messages")
        for _ in range(3):
            I.update(db_path=self.c.db, sessions_dir=self.c.root)
        self.assertEqual(self.one("SELECT count(*) FROM messages"), before)

    def test_reindex_after_append_does_not_duplicate(self):
        self.c.append_line("sess0000")
        I.update(db_path=self.c.db, sessions_dir=self.c.root)
        n1 = self.one("SELECT count(*) FROM messages WHERE session_id='sess0000'")
        self.c.append_line("sess0000", "第二次追加 second")
        I.update(db_path=self.c.db, sessions_dir=self.c.root)
        n2 = self.one("SELECT count(*) FROM messages WHERE session_id='sess0000'")
        self.assertEqual(n2, n1 + 1)

    def test_vanished_session_is_removed(self):
        os.remove(os.path.join(self.c.root, "sess0002.jsonl"))
        stats = I.update(db_path=self.c.db, sessions_dir=self.c.root)
        self.assertEqual(stats["sessions_removed"], 1)
        self.assertEqual(self.one(
            "SELECT count(*) FROM messages WHERE session_id='sess0002'"), 0)
        self.assertEqual(self.one("SELECT count(*) FROM sessions"), 2)

    def test_orphaned_output_is_garbage_collected(self):
        for sid in ("sess0000", "sess0001", "sess0002"):
            os.remove(os.path.join(self.c.root, f"{sid}.jsonl"))
        I.update(db_path=self.c.db, sessions_dir=self.c.root)
        self.assertEqual(self.one("SELECT count(*) FROM tool_output"), 0)
        self.assertEqual(self.one("SELECT count(*) FROM tool_output_fts"), 0)

    def test_contentless_fts_delete_actually_works(self):
        """Guards the contentless_delete=1 requirement: plain contentless FTS
        rejects DELETE, which would break the whole incremental design."""
        before = self.one("SELECT count(*) FROM messages_fts")
        os.remove(os.path.join(self.c.root, "sess0002.jsonl"))
        I.update(db_path=self.c.db, sessions_dir=self.c.root)
        self.assertLess(self.one("SELECT count(*) FROM messages_fts"), before)

    def test_stale_detection_reports_changed_files(self):
        con = sqlite3.connect(self.c.db)
        self.assertEqual(I.stale_files(con, self.c.root), [])
        con.close()
        self.c.append_line("sess0001")
        con = sqlite3.connect(self.c.db)
        self.assertEqual(len(I.stale_files(con, self.c.root)), 1)
        con.close()


class TestHardening(IndexerCase):
    def test_database_is_owner_only(self):
        mode = stat.S_IMODE(os.stat(self.c.db).st_mode)
        self.assertEqual(mode, 0o600, f"expected 0600, got {oct(mode)}")

    def test_wal_sidecars_are_owner_only(self):
        for suffix in ("-wal", "-shm"):
            p = self.c.db + suffix
            if os.path.exists(p):
                mode = stat.S_IMODE(os.stat(p).st_mode)
                self.assertEqual(mode, 0o600, f"{suffix} is {oct(mode)}")

    def test_parent_directory_is_owner_only(self):
        mode = stat.S_IMODE(os.stat(os.path.dirname(self.c.db)).st_mode)
        self.assertEqual(mode, 0o700, f"expected 0700, got {oct(mode)}")

    def test_source_logs_are_not_modified(self):
        before = {f: os.stat(os.path.join(self.c.root, f)).st_mtime
                  for f in os.listdir(self.c.root)}
        I.update(db_path=self.c.db, sessions_dir=self.c.root, full=True)
        after = {f: os.stat(os.path.join(self.c.root, f)).st_mtime
                 for f in os.listdir(self.c.root)}
        self.assertEqual(before, after)


class TestSchemaVersion(IndexerCase):
    def test_current_version_recorded(self):
        self.assertEqual(
            self.one("SELECT value FROM meta WHERE key='schema_version'"),
            I.SCHEMA_VERSION)

    def test_version_mismatch_forces_rebuild(self):
        con = sqlite3.connect(self.c.db)
        con.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
        con.commit()
        con.close()
        stats = I.update(db_path=self.c.db, sessions_dir=self.c.root)
        self.assertTrue(stats["schema_rebuild"])
        self.assertEqual(stats["files_indexed"], 3)

    def test_matching_version_does_not_rebuild(self):
        stats = I.update(db_path=self.c.db, sessions_dir=self.c.root)
        self.assertFalse(stats["schema_rebuild"])
        self.assertEqual(stats["files_indexed"], 0)

    def test_unreadable_database_is_rebuilt(self):
        with open(self.c.db, "wb") as f:
            f.write(b"not a sqlite file")
        stats = I.update(db_path=self.c.db, sessions_dir=self.c.root)
        self.assertTrue(stats["schema_rebuild"])
        self.assertEqual(self.one("SELECT count(*) FROM sessions"), 3)


class TestHelpers(unittest.TestCase):
    def test_leaf_text_flattens_nested_payloads(self):
        got = I._leaf_text({"Success": {"items": [{"Text": "a"}, {"Json": {"b": "c"}}]}})
        self.assertIn("a", got)
        self.assertIn("c", got)

    def test_leaf_text_skips_none_and_bool(self):
        self.assertEqual(I._leaf_text({"x": None, "y": True}).strip(), "")

    def test_tool_name_from_builtin(self):
        self.assertEqual(
            I._tool_name({"kind": {"BuiltIn": {"ExecuteCmd": {}}}}), "ExecuteCmd")

    def test_tool_name_from_string(self):
        self.assertEqual(I._tool_name({"kind": "Custom"}), "Custom")

    def test_tool_name_unknown(self):
        self.assertEqual(I._tool_name({}), "Unknown")
        self.assertEqual(I._tool_name(None), "Unknown")

    def test_result_status(self):
        self.assertEqual(I._result_status({"Success": {}}), "success")
        self.assertEqual(I._result_status({"Error": {}}), "error")
        self.assertIsNone(I._result_status("plain"))


class TestMalformedInput(unittest.TestCase):
    def test_unparseable_lines_are_counted_not_fatal(self):
        with F.Corpus(1) as c:
            with open(os.path.join(c.root, "sess0000.jsonl"), "a",
                      encoding="utf8") as f:
                f.write("{not valid json\n\n")
            stats = I.update(db_path=c.db, sessions_dir=c.root, full=True)
            self.assertEqual(stats["bad_lines"], 1)
            self.assertGreater(stats["messages"], 0)

    def test_missing_sidecar_still_indexes_session(self):
        with F.Corpus(1) as c:
            os.remove(os.path.join(c.root, "sess0000.json"))
            I.update(db_path=c.db, sessions_dir=c.root, full=True)
            con = sqlite3.connect(c.db)
            self.assertEqual(
                con.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            self.assertGreater(
                con.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            con.close()

    def test_corrupt_sidecar_is_tolerated(self):
        with F.Corpus(1) as c:
            with open(os.path.join(c.root, "sess0000.json"), "w") as f:
                f.write("{broken")
            I.update(db_path=c.db, sessions_dir=c.root, full=True)
            con = sqlite3.connect(c.db)
            self.assertEqual(
                con.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            con.close()

    def test_empty_corpus_produces_empty_index(self):
        with F.Corpus(0) as c:
            stats = I.update(db_path=c.db, sessions_dir=c.root, full=True)
            self.assertEqual(stats["files_indexed"], 0)
            con = sqlite3.connect(c.db)
            self.assertEqual(
                con.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
