"""Unit tests for ksi.query -- the guarded single entry point."""

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ksi import index as I
from ksi import query as Q
from tests import fixtures as F


class QueryCase(unittest.TestCase):
    def setUp(self):
        self.c = F.Corpus(3)
        self.c.__enter__()
        I.update(db_path=self.c.db, sessions_dir=self.c.root, full=True)
        self.con, _ = Q.open_index(self.c.db, auto_update=False)

    def tearDown(self):
        self.con.close()
        self.c.__exit__()


class TestGuard(unittest.TestCase):
    def test_rejects_snippet(self):
        with self.assertRaises(Q.QueryError):
            Q.guard_sql("SELECT snippet(messages_fts,0,'[',']','…',5) FROM messages_fts")

    def test_rejects_highlight(self):
        with self.assertRaises(Q.QueryError):
            Q.guard_sql("SELECT highlight(messages_fts,0,'<','>') FROM messages_fts")

    def test_is_case_insensitive(self):
        with self.assertRaises(Q.QueryError):
            Q.guard_sql("select SNIPPET(x,0,'','','',5) from y")

    def test_tolerates_space_before_paren(self):
        with self.assertRaises(Q.QueryError):
            Q.guard_sql("SELECT snippet (x,0,'','','',5) FROM y")

    def test_error_names_the_alternative(self):
        with self.assertRaises(Q.QueryError) as ctx:
            Q.guard_sql("SELECT snippet(x,0,'','','',5) FROM y")
        self.assertIn("snip(", str(ctx.exception))

    def test_allows_ordinary_sql(self):
        sql = "SELECT session_id, title FROM sessions LIMIT 5"
        self.assertEqual(Q.guard_sql(sql), sql)

    def test_allows_snip_itself(self):
        sql = "SELECT snip(text, '记忆', 20) FROM messages"
        self.assertEqual(Q.guard_sql(sql), sql)

    def test_does_not_trip_on_similar_words(self):
        # 'snippets' as a column name is not a function call to snippet().
        sql = "SELECT snippets FROM t"
        self.assertEqual(Q.guard_sql(sql), sql)


class TestSnipUdf(QueryCase):
    def test_snip_is_registered(self):
        got = self.con.execute("SELECT snip('前面 索引的表现 后面', '索引', 6)").fetchone()[0]
        self.assertIn("索引", got)

    def test_snip_defaults_width(self):
        got = self.con.execute("SELECT snip('alpha beta gamma', 'beta')").fetchone()[0]
        self.assertIn("beta", got)

    def test_snip_handles_nulls(self):
        self.assertEqual(self.con.execute("SELECT snip(NULL, 'x')").fetchone()[0], "")

    def test_snip_usable_over_real_rows(self):
        rows = self.con.execute(
            "SELECT snip(text, ?, 20) FROM messages WHERE text IS NOT NULL LIMIT 3",
            ("遗忘",)).fetchall()
        self.assertTrue(all(isinstance(r[0], str) for r in rows))

    def test_contentless_snippet_would_have_been_empty(self):
        """Documents the trap the guard exists to prevent."""
        got = self.con.execute(
            "SELECT snippet(messages_fts,0,'[',']','…',8) FROM messages_fts"
            " WHERE messages_fts MATCH ? LIMIT 1", ('"遗 忘"',)).fetchall()
        self.assertTrue(all(not (r[0] or "").strip() for r in got))


class TestSearchProse(QueryCase):
    def test_finds_cjk(self):
        self.assertGreater(len(Q.search_prose(self.con, "遗忘机制")), 0)

    def test_finds_multi_term_cjk_plus_ascii(self):
        """End-to-end guard for the handoff's silent zero-result bug."""
        self.assertGreater(len(Q.search_prose(self.con, "记忆 系统")), 0)

    def test_returns_citation(self):
        hit = Q.search_prose(self.con, "遗忘机制")[0]
        self.assertIn(":", hit["citation"])

    def test_snippet_is_readable_cjk(self):
        hit = Q.search_prose(self.con, "遗忘机制")[0]
        self.assertNotIn("遗  忘", hit["snippet"])

    def test_respects_limit(self):
        self.assertLessEqual(len(Q.search_prose(self.con, "记忆", limit=2)), 2)

    def test_session_filter(self):
        rows = Q.search_prose(self.con, "遗忘机制", session="sess0001")
        self.assertTrue(all(r["session_id"] == "sess0001" for r in rows))
        self.assertGreater(len(rows), 0)

    def test_project_filter(self):
        rows = Q.search_prose(self.con, "遗忘机制", project="proj1")
        self.assertTrue(all(r["project"] == "proj1" for r in rows))

    def test_role_filter(self):
        rows = Q.search_prose(self.con, "遗忘机制", role="user")
        self.assertTrue(all(r["role"] == "user" for r in rows))

    def test_since_filter_excludes_older(self):
        rows = Q.search_prose(self.con, "遗忘机制", since=4000000000)
        self.assertEqual(rows, [])

    def test_absent_term_returns_empty(self):
        self.assertEqual(Q.search_prose(self.con, "zzzznotpresentzzzz"), [])

    def test_empty_query_raises(self):
        with self.assertRaises(ValueError):
            Q.search_prose(self.con, "  ")

    def test_when_field_is_formatted(self):
        hit = Q.search_prose(self.con, "遗忘机制")[0]
        self.assertRegex(hit["when"], r"\d{4}-\d{2}-\d{2}")


class TestSearchToolOutput(QueryCase):
    def test_finds_command_output(self):
        self.assertGreater(len(Q.search_tool_output(self.con, "connection refused")), 0)

    def test_reports_every_session_that_saw_it(self):
        # Dedup stores the payload once but must not lose occurrences.
        rows = Q.search_tool_output(self.con, "connection refused", limit=10)
        self.assertEqual(len({r["session_id"] for r in rows}), 3)

    def test_includes_tool_name_and_purpose(self):
        hit = Q.search_tool_output(self.con, "connection refused")[0]
        self.assertEqual(hit["tool"], "ExecuteCmd")
        self.assertIn("decay", hit["purpose"])

    def test_status_filter(self):
        rows = Q.search_tool_output(self.con, "connection refused", status="success")
        self.assertGreater(len(rows), 0)
        rows = Q.search_tool_output(self.con, "connection refused", status="error")
        self.assertEqual(rows, [])

    def test_tool_filter(self):
        rows = Q.search_tool_output(self.con, "connection refused", tool="WebFetch")
        self.assertEqual(rows, [])

    def test_fileread_content_is_absent(self):
        self.assertEqual(
            Q.search_tool_output(self.con, "FILEREAD_PAYLOAD_MUST_BE_SKIPPED"), [])

    def test_session_filter(self):
        rows = Q.search_tool_output(self.con, "connection refused", session="sess0001")
        self.assertEqual([r["session_id"] for r in rows], ["sess0001"])


class TestToolOutputFiltersBeforeLimit(unittest.TestCase):
    """Every filter narrows the candidates before the row cap, not after it.

    The bug: the cap picked the top-ranked outputs from the whole index and the
    filters then ran over those few. A rare match below them -- an error among
    many successes -- came back as zero rows with no truncation notice, which the
    skill tells the agent to read as "never happened".
    """

    # Short outputs outrank longer ones under bm25. Six noise outputs in sess0000
    # (proj0) fill any cap up to 6, so everything else ranks below them.
    NOISE = [(f"connection refused {i}", True) for i in range(6)]
    TARGET = "connection refused " + "while waiting for the server to come back " * 8

    def setUp(self):
        self.c = F.Corpus(3)
        self.c.__enter__()
        self.c.append_tool_results("sess0000", self.NOISE)
        self.c.append_tool_results("sess0001", [(self.TARGET, False)], tool="Grep")
        I.update(db_path=self.c.db, sessions_dir=self.c.root, full=True)
        self.con, _ = Q.open_index(self.c.db, auto_update=False)

    def tearDown(self):
        self.con.close()
        self.c.__exit__()

    def search(self, limit=3, **filters):
        return Q.search_tool_output(self.con, "connection refused", limit=limit,
                                    **filters)

    def test_fixture_ranks_noise_first(self):
        self.assertEqual({r["session_id"] for r in self.search()}, {"sess0000"})

    def test_status(self):
        rows = self.search(status="error")
        self.assertEqual([(r["session_id"], r["status"]) for r in rows],
                         [("sess0001", "error")])

    def test_tool(self):
        rows = self.search(tool="Grep")
        self.assertEqual([(r["session_id"], r["tool"]) for r in rows],
                         [("sess0001", "Grep")])

    def test_session(self):
        # sess0001 holds the shared corpus output and the target.
        rows = self.search(session="sess0001")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["session_id"] for r in rows}, {"sess0001"})

    def test_project(self):
        # proj1 is sess0001 alone.
        rows = self.search(project="proj1")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["project"] for r in rows}, {"proj1"})

    def test_limit_counts_result_rows(self):
        # One deduplicated output seen by three sessions is three rows, and the
        # cap must count those, or a full page would print no truncation notice.
        rows = Q.search_tool_output(self.con, "port 5480", limit=2)
        self.assertEqual(len(rows), 2)


class TestSearchLike(QueryCase):
    def test_finds_identifier_substring_that_match_cannot(self):
        # 'DatabaseSync' is one unicode61 token, so MATCH cannot find 'abaseSy'.
        self.assertEqual(Q.search_prose(self.con, "abaseSy"), [])
        self.assertGreater(len(Q.search_like(self.con, "abaseSy")), 0)

    def test_prefilter_narrows_and_still_finds(self):
        rows = Q.search_like(self.con, "abaseSy", prefilter="DatabaseSync")
        self.assertGreater(len(rows), 0)

    def test_prefilter_that_excludes_yields_nothing(self):
        rows = Q.search_like(self.con, "abaseSy", prefilter="zzzznotpresentzzzz")
        self.assertEqual(rows, [])

    def test_can_scan_tool_output(self):
        rows = Q.search_like(self.con, "onnection refus", tool_output=True)
        self.assertGreater(len(rows), 0)

    def test_returns_citation_for_prose(self):
        self.assertIsNotNone(Q.search_like(self.con, "abaseSy")[0]["citation"])

    def test_respects_limit(self):
        self.assertLessEqual(len(Q.search_like(self.con, "记忆", limit=1)), 1)


class TestRunSql(QueryCase):
    def test_returns_columns_and_rows(self):
        cols, rows, truncated = Q.run_sql(self.con, "SELECT session_id FROM sessions")
        self.assertEqual(cols, ["session_id"])
        self.assertEqual(len(rows), 3)
        self.assertFalse(truncated)

    def test_enforces_limit(self):
        _, rows, truncated = Q.run_sql(self.con, "SELECT id FROM messages", limit=2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(truncated)

    def test_full_result_is_not_flagged_truncated(self):
        # Exactly the row count under a matching cap: complete, not truncated.
        n = self.con.execute("SELECT count(*) FROM sessions").fetchone()[0]
        _, rows, truncated = Q.run_sql(
            self.con, "SELECT session_id FROM sessions", limit=n)
        self.assertEqual(len(rows), n)
        self.assertFalse(truncated)

    def test_guard_applies(self):
        with self.assertRaises(Q.QueryError):
            Q.run_sql(self.con, "SELECT snippet(messages_fts,0,'','','',5)"
                                " FROM messages_fts")

    def test_structural_query_works(self):
        cols, rows, _ = Q.run_sql(
            self.con,
            "SELECT s.title, t.name, t.path FROM tool_calls t"
            " JOIN sessions s USING(session_id) ORDER BY s.created_at")
        self.assertEqual(len(rows), 3)

    def test_lineage_query_works(self):
        _, rows, _ = Q.run_sql(
            self.con, "SELECT session_id FROM sessions"
                      " WHERE parent_session_id IS NOT NULL")
        self.assertEqual(len(rows), 2)


class TestStatus(QueryCase):
    def test_reports_counts(self):
        st = Q.status(self.con, self.c.db, self.c.root)
        self.assertEqual(st["sessions"], 3)
        self.assertEqual(st["subagent_sessions"], 2)
        self.assertEqual(st["stale_files"], 0)

    def test_detects_staleness(self):
        self.c.append_line("sess0000")
        st = Q.status(self.con, self.c.db, self.c.root)
        self.assertEqual(st["stale_files"], 1)


class TestFreshness(unittest.TestCase):
    def test_open_index_refreshes_when_stale(self):
        with F.Corpus(2) as c:
            I.update(db_path=c.db, sessions_dir=c.root, full=True)
            c.append_line("sess0000", "刚刚说的话 justsaid")
            con, refreshed = Q.open_index(c.db, auto_update=True, sessions_dir=c.root)
            try:
                self.assertEqual(refreshed["files_indexed"], 1)
                self.assertGreater(len(Q.search_prose(con, "刚刚说的话")), 0)
            finally:
                con.close()

    def test_no_update_leaves_index_stale(self):
        with F.Corpus(2) as c:
            I.update(db_path=c.db, sessions_dir=c.root, full=True)
            c.append_line("sess0000", "还没索引 notyet")
            con, refreshed = Q.open_index(c.db, auto_update=False)
            try:
                self.assertIsNone(refreshed)
                self.assertEqual(Q.search_prose(con, "还没索引"), [])
            finally:
                con.close()

    def test_missing_index_without_update_raises(self):
        with F.Corpus(1) as c:
            with self.assertRaises(FileNotFoundError):
                Q.open_index(c.db + ".absent", auto_update=False)

    def test_missing_index_is_built_on_demand(self):
        with F.Corpus(2) as c:
            self.assertFalse(os.path.exists(c.db))
            con, refreshed = Q.open_index(c.db, auto_update=True, sessions_dir=c.root)
            try:
                self.assertEqual(refreshed["files_indexed"], 2)
                self.assertGreater(len(Q.search_prose(con, "遗忘机制")), 0)
            finally:
                con.close()


class TestCli(unittest.TestCase):
    def setUp(self):
        self.c = F.Corpus(2)
        self.c.__enter__()
        I.update(db_path=self.c.db, sessions_dir=self.c.root, full=True)
        self.base = ["--db", self.c.db, "--sessions", self.c.root]

    def tearDown(self):
        self.c.__exit__()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = Q.main(list(args) + self.base)
        return code, out.getvalue(), err.getvalue()

    def test_search_succeeds(self):
        code, out, _ = self.run_cli("遗忘机制")
        self.assertEqual(code, 0)
        self.assertIn("cite:", out)

    def test_json_output_is_valid(self):
        import json
        code, out, _ = self.run_cli("遗忘机制", "--json")
        self.assertEqual(code, 0)
        self.assertIsInstance(json.loads(out), list)

    def test_guard_exits_nonzero(self):
        code, _, err = self.run_cli("--sql",
                                    "SELECT snippet(messages_fts,0,'','','',5)"
                                    " FROM messages_fts")
        self.assertEqual(code, 2)
        self.assertIn("snip(", err)

    def test_status_exits_zero(self):
        code, out, _ = self.run_cli("--status")
        self.assertEqual(code, 0)
        self.assertIn("sessions", out)

    def test_no_query_prints_help(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("usage", out.lower())

    def test_no_match_is_reported_not_crashed(self):
        code, out, _ = self.run_cli("zzzznotpresentzzzz")
        self.assertEqual(code, 0)
        self.assertIn("no matches", out)

    def test_like_mode(self):
        code, out, _ = self.run_cli("--like", "abaseSy")
        self.assertEqual(code, 0)
        self.assertIn("DatabaseSync", out)

    def test_tool_mode(self):
        code, out, _ = self.run_cli("-t", "connection refused")
        self.assertEqual(code, 0)
        self.assertIn("ExecuteCmd", out)

    def test_tool_mode_honours_session(self):
        import json
        code, out, _ = self.run_cli("-t", "connection refused", "--json",
                                    "--session", "sess0001")
        self.assertEqual(code, 0)
        self.assertEqual({r["session_id"] for r in json.loads(out)}, {"sess0001"})

    def test_bad_since_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.run_cli("记忆", "--since", "not-a-date")

    def test_sql_mode_prints_rows(self):
        code, out, _ = self.run_cli("--sql", "SELECT count(*) AS n FROM sessions")
        self.assertEqual(code, 0)
        self.assertIn("2", out)


class TestCliLimit(unittest.TestCase):
    """The default limit is mode-aware, and a capped result says so.

    The handoff bug: --sql inherited the prose default of 10 and silently dropped
    the rest, so an aggregate or exploratory query looked like an incomplete index.
    A corpus of 8 sessions yields 40 message rows -- more than any mode's default
    cap of 10 -- so a bare row dump exposes both halves of the bug.
    """

    def setUp(self):
        self.c = F.Corpus(8)
        self.c.__enter__()
        I.update(db_path=self.c.db, sessions_dir=self.c.root, full=True)
        self.base = ["--db", self.c.db, "--sessions", self.c.root, "--no-update"]
        self.total = self.con_count()

    def con_count(self):
        con, _ = Q.open_index(self.c.db, auto_update=False)
        try:
            return con.execute("SELECT count(*) FROM messages").fetchone()[0]
        finally:
            con.close()

    def tearDown(self):
        self.c.__exit__()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = Q.main(list(args) + self.base)
        return code, out.getvalue(), err.getvalue()

    def _data_rows(self, out):
        # First stdout line is the column header; the rest are data rows.
        lines = [l for l in out.splitlines() if l != ""]
        return lines[1:]

    def test_sql_default_is_not_the_prose_ten(self):
        self.assertGreater(self.total, 10)  # guard: the corpus can expose the bug
        code, out, _ = self.run_cli("--sql", "SELECT id FROM messages")
        self.assertEqual(code, 0)
        self.assertEqual(len(self._data_rows(out)), self.total)

    def test_sql_capped_reports_truncation_on_stderr(self):
        code, out, err = self.run_cli("-n", "2", "--sql", "SELECT id FROM messages")
        self.assertEqual(code, 0)
        self.assertEqual(len(self._data_rows(out)), 2)
        self.assertIn("truncated", err.lower())

    def test_sql_uncapped_says_nothing_about_truncation(self):
        code, _, err = self.run_cli("--sql", "SELECT id FROM messages LIMIT 3")
        self.assertEqual(code, 0)
        self.assertNotIn("truncated", err.lower())

    def test_sql_exactly_at_limit_is_not_flagged(self):
        # N rows under -n N is complete, not truncated: fetching N+1 finds no extra.
        code, _, err = self.run_cli("-n", str(self.total),
                                    "--sql", "SELECT id FROM messages")
        self.assertEqual(code, 0)
        self.assertNotIn("truncated", err.lower())

    def test_sql_json_stdout_stays_a_clean_list_when_truncated(self):
        import json
        code, out, err = self.run_cli("-n", "2", "--json",
                                      "--sql", "SELECT id FROM messages")
        self.assertEqual(code, 0)
        parsed = json.loads(out)  # stdout must remain valid JSON
        self.assertEqual(len(parsed), 2)
        self.assertIn("truncated", err.lower())

    def test_prose_capped_reports_truncation(self):
        # Every synthetic session contains the phrase, so 8 sessions > default 10? no;
        # cap explicitly at 1 to force the notice.
        code, _, err = self.run_cli("-n", "1", "遗忘机制")
        self.assertEqual(code, 0)
        self.assertIn("truncated", err.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
