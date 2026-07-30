"""Integration tests against the REAL corpus.

These are the acceptance criteria agreed during design. They are skipped when the
real session directory or a built index is absent, so the suite still runs on a
clean machine.

Deliberately assert only counts, timings and structural properties -- never the
content of any record.
"""

import glob
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ksi import index as I
from ksi import query as Q
from ksi import text as T

SESSIONS = I.DEFAULT_SESSIONS
DB = I.DEFAULT_DB
have_corpus = os.path.isdir(SESSIONS) and bool(
    glob.glob(os.path.join(SESSIONS, "*.jsonl")))
have_index = os.path.exists(DB)


@unittest.skipUnless(have_corpus and have_index,
                     "real corpus or built index not available")
class TestRealCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con, _ = Q.open_index(DB, auto_update=False)
        cls.source_files = sorted(glob.glob(os.path.join(SESSIONS, "*.jsonl")))

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def one(self, sql, *p):
        return self.con.execute(sql, p).fetchone()[0]

    # ---- coverage ----

    def test_every_session_file_is_indexed(self):
        indexed = self.one("SELECT count(*) FROM files")
        self.assertEqual(indexed, len(self.source_files))

    def test_sessions_row_per_session(self):
        self.assertEqual(self.one("SELECT count(*) FROM sessions"),
                         len(self.source_files))

    def test_index_is_not_stale(self):
        """No dormant session may be stale.

        The session running these tests is being appended to continuously, so it is
        expected to be stale within seconds of any index run. Only files untouched
        for a few minutes are held to the assertion.
        """
        cutoff = time.time() - 300
        dormant = [p for p in I.stale_files(self.con, SESSIONS)
                   if os.path.getmtime(p) < cutoff]
        self.assertEqual(dormant, [], "dormant sessions are stale -- run ksi-index")

    def test_messages_present_in_bulk(self):
        self.assertGreater(self.one("SELECT count(*) FROM messages"), 10000)

    def test_subagent_lineage_captured(self):
        self.assertGreater(
            self.one("SELECT count(*) FROM sessions"
                     " WHERE parent_session_id IS NOT NULL"), 0)

    # ---- the citation-anchor property this design relies on ----

    def test_message_id_is_unique_in_practice(self):
        """The reason ix_msg_citation is not a UNIQUE constraint: uniqueness is an
        upstream property, verified here rather than enforced destructively."""
        total = self.one(
            "SELECT count(*) FROM messages WHERE message_id IS NOT NULL")
        distinct = self.one(
            "SELECT count(*) FROM (SELECT DISTINCT message_id, content_index"
            " FROM messages WHERE message_id IS NOT NULL)")
        self.assertEqual(total, distinct)

    def test_citations_resolve(self):
        row = self.con.execute(
            "SELECT message_id, content_index FROM messages"
            " WHERE message_id IS NOT NULL LIMIT 1").fetchone()
        n = self.one("SELECT count(*) FROM messages"
                     " WHERE message_id=? AND content_index=?", row[0], row[1])
        self.assertEqual(n, 1)

    # ---- exclusions ----

    def test_no_fileread_results(self):
        self.assertEqual(
            self.one("SELECT count(*) FROM tool_results WHERE tool_name='FileRead'"), 0)

    def test_no_compaction_messages(self):
        self.assertEqual(
            self.one("SELECT count(*) FROM messages WHERE kind='Compaction'"), 0)

    def test_tool_output_is_deduplicated(self):
        outputs = self.one("SELECT count(*) FROM tool_output")
        results = self.one("SELECT count(*) FROM tool_results")
        self.assertLess(outputs, results,
                        "expected fewer unique payloads than results")

    def test_every_result_points_at_a_payload(self):
        self.assertEqual(self.one(
            "SELECT count(*) FROM tool_results r"
            " LEFT JOIN tool_output o ON o.id = r.output_id"
            " WHERE o.id IS NULL"), 0)

    def test_no_orphaned_payloads(self):
        self.assertEqual(self.one(
            "SELECT count(*) FROM tool_output o"
            " LEFT JOIN tool_results r ON r.output_id = o.id"
            " WHERE r.tool_use_id IS NULL"), 0)

    # ---- the five query classes ----

    def _timed(self, fn, *a, **kw):
        t0 = time.time()
        rows = fn(*a, **kw)
        return rows, (time.time() - t0) * 1000

    def test_cjk_query(self):
        rows, ms = self._timed(Q.search_prose, self.con, "记忆")
        self.assertGreater(len(rows), 0)
        self.assertLess(ms, 500, f"CJK query took {ms:.0f}ms")

    def test_multi_term_cjk_ascii_query(self):
        """Regression guard for the handoff's silent zero-result bug, on real data."""
        rows, ms = self._timed(Q.search_prose, self.con, "记忆 memory")
        self.assertGreater(len(rows), 0,
                           "multi-term CJK+ASCII returned nothing -- transform broke")
        self.assertLess(ms, 500)

    def test_english_query(self):
        rows, ms = self._timed(Q.search_prose, self.con, "pipeline")
        self.assertGreater(len(rows), 0)
        self.assertLess(ms, 500)

    def test_code_whole_token_query(self):
        rows, _ = self._timed(Q.search_prose, self.con, "package.json")
        self.assertGreater(len(rows), 0)

    def test_code_substring_via_like(self):
        rows, ms = self._timed(Q.search_like, self.con, "atabaseSyn")
        self.assertGreater(len(rows), 0)
        self.assertLess(ms, 2000, f"LIKE scan took {ms:.0f}ms")

    def test_tool_output_query(self):
        rows, ms = self._timed(Q.search_tool_output, self.con, "error")
        self.assertGreater(len(rows), 0)
        self.assertLess(ms, 1000)

    def test_structural_query(self):
        _, rows = Q.run_sql(
            self.con,
            "SELECT s.project, count(*) AS n FROM tool_calls t"
            " JOIN sessions s USING(session_id) WHERE t.path IS NOT NULL"
            " GROUP BY s.project ORDER BY n DESC", limit=10)
        self.assertGreater(len(rows), 0)

    # ---- snippets ----

    def test_cjk_snippets_are_readable(self):
        rows = Q.search_prose(self.con, "索引", limit=5)
        self.assertGreater(len(rows), 0)
        for r in rows:
            self.assertNotIn("索  引", r["snippet"],
                             "snippet shows split text -- must slice the original")

    def test_snippets_are_non_empty(self):
        rows = Q.search_prose(self.con, "memory", limit=5)
        self.assertTrue(all(r["snippet"].strip() for r in rows))

    # ---- the three silent failure modes ----

    def test_trap_contentless_snippet_returns_empty(self):
        got = self.con.execute(
            "SELECT snippet(messages_fts,0,'[',']','…',8) FROM messages_fts"
            " WHERE messages_fts MATCH ? LIMIT 5", (T.build_match("记忆"),)).fetchall()
        self.assertGreater(len(got), 0)
        self.assertTrue(all(not (r[0] or "").strip() for r in got),
                        "snippet() unexpectedly worked -- revisit the guard rationale")

    def test_trap_snippet_is_blocked(self):
        with self.assertRaises(Q.QueryError):
            Q.run_sql(self.con,
                      "SELECT snippet(messages_fts,0,'','','',5) FROM messages_fts")

    def test_trap_whole_query_as_phrase_would_fail(self):
        """Shows why per-term AND is required, using the real corpus.

        Asserts the durable property -- AND finds strictly more than the phrase form.
        Not that the phrase form finds zero: a corpus can contain the literal
        adjacent text (this one does, since these sessions discuss the bug itself).
        """
        broken = '"' + T.split_index("记忆 memory").strip() + '"'
        as_phrase = self.con.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?",
            (broken,)).fetchone()[0]
        correct = self.con.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?",
            (T.build_match("记忆 memory"),)).fetchone()[0]
        self.assertGreater(correct, as_phrase,
                           "per-term AND must find at least as much as the phrase form")
        self.assertGreater(correct, 0)

    # ---- hardening ----

    def test_index_is_owner_only(self):
        import stat as S
        for suffix in ("", "-wal", "-shm"):
            p = DB + suffix
            if os.path.exists(p):
                self.assertEqual(S.S_IMODE(os.stat(p).st_mode), 0o600, p)

    def test_index_lives_outside_any_git_repo(self):
        d = os.path.dirname(os.path.abspath(DB))
        while d != "/":
            self.assertFalse(os.path.isdir(os.path.join(d, ".git")),
                             f"index sits inside a git repo at {d}")
            d = os.path.dirname(d)

    def test_no_thinking_signatures_stored(self):
        """Signatures are long base64-ish blobs; none should exist in prose."""
        n = self.one("SELECT count(*) FROM messages"
                     " WHERE content_type='thinking' AND length(text) > 20000")
        self.assertEqual(n, 0)


@unittest.skipUnless(have_corpus, "real corpus not available")
class TestRealIncremental(unittest.TestCase):
    def test_repeated_update_is_cheap_and_idempotent(self):
        """Idempotency is checked on a DORMANT session.

        The session running these tests grows while they run, so comparing total row
        counts across two updates is racy -- a single refresh of a long session can
        add hundreds of rows. Pick a session whose file is not being written and
        assert its row count is untouched by a second update.
        """
        stats = I.update()
        self.assertLess(stats["elapsed_s"], 60)

        cutoff = time.time() - 3600
        dormant = next(
            (p for p in sorted(glob.glob(os.path.join(SESSIONS, "*.jsonl")))
             if os.path.getmtime(p) < cutoff), None)
        if dormant is None:
            self.skipTest("no dormant session to compare against")
        session_id = os.path.basename(dormant)[: -len(".jsonl")]

        def rows():
            con = sqlite3.connect(DB)
            try:
                return con.execute(
                    "SELECT count(*) FROM messages WHERE session_id=?",
                    (session_id,)).fetchone()[0]
            finally:
                con.close()

        before = rows()
        self.assertGreater(before, 0, "dormant session should already be indexed")
        I.update()
        self.assertEqual(rows(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
