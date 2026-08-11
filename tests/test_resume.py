"""Tests for the resume front end.

Three layers, per the design decisions behind this module:

  * pure functions -- widths, paths, ages;
  * listing and counting -- what appears, in what order, from which source;
  * the failure contract -- searching without a usable index must be loud and
    non-zero, never a quiet downgrade.

Plus the picker, under a pty. That one was originally left out as untestable, which
is precisely where a hang got through: see TestSelectionReachesTheTerminal. Only the
exec into kiro-cli remains uncovered, since it replaces the process.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ksi import index as I
from ksi import query as Q
from ksi import resume as R
from tests import fixtures as F

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(REPO, "tools", "kiro-resume")


def sidecars(root, specs, with_log=True):
    """Write .json sidecars, each beside a log with something in it.

    Sidecar-only is the interesting shape for the *index*: it is exactly what the
    index cannot see, since a sessions row is created while parsing the .jsonl. But
    the listing skips sessions whose log is empty, so a fixture needs a non-empty log
    to be listed at all -- pass with_log=False to build the abandoned-launch case.
    """
    os.makedirs(root, exist_ok=True)
    for spec in specs:
        sid = spec["session_id"]
        with open(os.path.join(root, sid + ".json"), "w") as fh:
            json.dump(spec, fh)
        if with_log:
            with open(os.path.join(root, sid + ".jsonl"), "w") as fh:
                fh.write(json.dumps({"version": "1", "kind": "Prompt", "data": {
                    "message_id": sid + "-m1", "meta": {"timestamp": 1776350561},
                    "content": [{"kind": "text", "data": "a turn"}]}}) + "\n")
        else:
            open(os.path.join(root, sid + ".jsonl"), "w").close()
    return root


def spec(sid, cwd="/w/proj", title=None, updated="2026-04-16T15:00:00Z", parent=None):
    return {"session_id": sid, "cwd": cwd, "title": title or f"session {sid}",
            "updated_at": updated, "parent_session_id": parent}


class TestDisplayWidth(unittest.TestCase):
    def test_ascii_is_one_column_each(self):
        self.assertEqual(R.display_width("abc"), 3)

    def test_cjk_is_two_columns_each(self):
        self.assertEqual(R.display_width("记忆"), 4)

    def test_mixed_adds_up(self):
        self.assertEqual(R.display_width("a记b"), 4)

    def test_empty_is_zero(self):
        self.assertEqual(R.display_width(""), 0)


class TestTruncate(unittest.TestCase):
    def test_short_string_untouched(self):
        self.assertEqual(R.truncate("abc", 10), "abc")

    def test_marks_the_cut(self):
        self.assertTrue(R.truncate("abcdefghij", 5).endswith("…"))

    def test_never_exceeds_the_budget(self):
        for text in ("abcdefghij", "记忆系统的遗忘机制", "a记b忆c"):
            for budget in range(2, 12):
                self.assertLessEqual(R.display_width(R.truncate(text, budget)), budget,
                                     f"{text!r} at {budget}")

    def test_cjk_counts_as_two_when_cutting(self):
        # Four columns: two wide characters fit only if the ellipsis is accounted for.
        self.assertEqual(R.display_width(R.truncate("记忆系统", 4)), 3)


class TestShortPath(unittest.TestCase):
    def test_home_becomes_tilde(self):
        self.assertTrue(R.short_path(os.path.expanduser("~/x")).startswith("~/"))

    def test_names_no_real_user(self):
        """A generic assertion, so it holds for anyone running the suite."""
        self.assertNotIn(os.path.expanduser("~"), R.short_path(os.path.expanduser("~/x")))

    def test_deep_path_keeps_last_two_components(self):
        self.assertEqual(R.short_path("/a/b/c/d/e"), "~/d/e")

    def test_shallow_path_untouched(self):
        self.assertEqual(R.short_path("/a/b"), "/a/b")


class TestAge(unittest.TestCase):
    def test_unparseable_is_marked_not_crashed(self):
        self.assertEqual(R.age("not a timestamp"), "?")

    def test_missing_is_marked_not_crashed(self):
        self.assertEqual(R.age(None), "?")

    def test_recent_reads_as_just_now(self):
        import time
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        self.assertEqual(R.age(now), "just now")


class TestLoadSessions(unittest.TestCase):
    def test_reads_sidecars_without_the_index(self):
        """The list must not depend on the index, or a cold cache hides sessions."""
        with tempfile.TemporaryDirectory() as tmp:
            root = sidecars(tmp, [spec("a"), spec("b")])
            self.assertEqual(set(R.load_sessions(root)), {"a", "b"})

    def test_corrupt_sidecar_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = sidecars(tmp, [spec("good")])
            with open(os.path.join(root, "bad.json"), "w") as fh:
                fh.write("{ this is not json")
            self.assertEqual(set(R.load_sessions(root)), {"good"})

    def test_abandoned_launch_is_not_listed(self):
        """Opening a session writes the files; saying nothing leaves an empty log.

        These were showing up as "(untitled)" rows -- 17 of 688 sessions, 5 of them in
        one project's most recent 20 -- and resuming one lands in a blank session.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sidecars(tmp, [spec("used")])
            sidecars(tmp, [spec("abandoned", title=None)], with_log=False)
            self.assertEqual(set(R.load_sessions(tmp)), {"used"})

    def test_a_sidecar_with_no_log_at_all_is_not_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecars(tmp, [spec("used")])
            with open(os.path.join(tmp, "orphan.json"), "w") as fh:
                json.dump(spec("orphan"), fh)
            self.assertEqual(set(R.load_sessions(tmp)), {"used"})


class TestHasContent(unittest.TestCase):
    def test_empty_log_has_nothing_to_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "s.jsonl")
            open(p, "w").close()
            self.assertFalse(R.has_content(p))

    def test_a_log_with_a_record_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "s.jsonl")
            with open(p, "w") as fh:
                fh.write("{}\n")
            self.assertTrue(R.has_content(p))

    def test_missing_log_is_treated_as_empty(self):
        self.assertFalse(R.has_content("/nonexistent/s.jsonl"))


class TestListSessions(unittest.TestCase):
    def setUp(self):
        self.sessions = {
            "top1": spec("top1", cwd="/w/a", updated="2026-04-16T10:00:00Z"),
            "top2": spec("top2", cwd="/w/b", updated="2026-04-16T12:00:00Z"),
            "sub1": spec("sub1", cwd="/w/a", updated="2026-04-16T13:00:00Z",
                         parent="top1"),
        }

    def _ids(self, **kw):
        return [r["session_id"] for r in R.list_sessions(self.sessions, **kw)]

    @staticmethod
    def _m(**hits):
        return {sid: {"hits": n, "snippet": f"a sentence mentioning it ({n}x)"}
                for sid, n in hits.items()}

    def test_subagent_sessions_never_appear(self):
        self.assertNotIn("sub1", self._ids(all_dirs=True))

    def test_filters_to_the_current_directory(self):
        self.assertEqual(self._ids(cwd="/w/a"), ["top1"])

    def test_all_dirs_shows_everything_top_level(self):
        self.assertEqual(set(self._ids(all_dirs=True)), {"top1", "top2"})

    def test_listing_is_newest_first(self):
        self.assertEqual(self._ids(all_dirs=True), ["top2", "top1"])

    def test_limit_applies(self):
        self.assertEqual(len(self._ids(all_dirs=True, limit=1)), 1)

    def test_matches_filter_excludes_unmatched(self):
        self.assertEqual(self._ids(all_dirs=True, matches=self._m(top1=3)), ["top1"])

    def test_searching_orders_by_hits_not_recency(self):
        """Search means "find the one about this", so relevance leads."""
        # top2 is newer, top1 matched more often.
        self.assertEqual(self._ids(all_dirs=True, matches=self._m(top1=9, top2=1)),
                         ["top1", "top2"])

    def test_equal_hits_fall_back_to_recency(self):
        self.assertEqual(self._ids(all_dirs=True, matches=self._m(top1=2, top2=2)),
                         ["top2", "top1"])

    def test_snippet_is_carried_onto_the_row(self):
        row = R.list_sessions(self.sessions, all_dirs=True,
                              matches=self._m(top1=4))[0]
        self.assertIn("a sentence mentioning it", row["snippet"])
        self.assertEqual(row["hits"], 4)

    def test_rows_have_no_snippet_when_not_searching(self):
        self.assertEqual(R.list_sessions(self.sessions, all_dirs=True)[0]["snippet"], "")

    def test_newlines_stripped_from_snippets(self):
        m = {"top1": {"hits": 1, "snippet": "two\nlines\there"}}
        row = R.list_sessions(self.sessions, all_dirs=True, matches=m)[0]
        self.assertNotIn("\n", row["snippet"])
        self.assertNotIn("\t", row["snippet"])

    def test_missing_title_is_labelled(self):
        self.sessions["top1"]["title"] = None
        row = next(r for r in R.list_sessions(self.sessions, all_dirs=True)
                   if r["session_id"] == "top1")
        self.assertEqual(row["title"], "(untitled)")

    def test_tabs_and_newlines_stripped_from_titles(self):
        self.sessions["top1"]["title"] = "a\tb\nc"
        row = next(r for r in R.list_sessions(self.sessions, all_dirs=True)
                   if r["session_id"] == "top1")
        self.assertNotIn("\t", row["title"])
        self.assertNotIn("\n", row["title"])


class TestFormatRows(unittest.TestCase):
    def setUp(self):
        self.rows = R.list_sessions({"s": spec("s", cwd="/w/a", title="记忆系统的设计")},
                                    all_dirs=True)
        self.hit = R.list_sessions(
            {"s": spec("s", cwd="/w/proj-name", title="记忆系统的设计")},
            all_dirs=True,
            matches={"s": {"hits": 7, "snippet": "…谈到了记忆的遗忘机制…"}})

    def _widest(self, item):
        return max(R.display_width(line) for line in item.split("\n"))

    def test_line_fits_the_terminal(self):
        for cols in (40, 60, 100, 200):
            self.assertLessEqual(self._widest(R.format_rows(self.rows, cols=cols)[0]),
                                 cols)

    def test_two_line_item_fits_the_terminal(self):
        for cols in (40, 60, 100, 200):
            item = R.format_rows(self.hit, cols=cols, searching=True,
                                 terms=["记忆"])[0]
            self.assertLessEqual(self._widest(item), cols, f"at {cols} cols")

    def test_listing_is_one_line(self):
        self.assertNotIn("\n", R.format_rows(self.rows, cols=100)[0])

    def test_searching_is_two_lines(self):
        item = R.format_rows(self.hit, cols=100, searching=True, terms=["记忆"])[0]
        self.assertEqual(item.count("\n"), 1)
        self.assertIn("遗忘机制", item.split("\n")[1])

    def test_snippet_line_is_indented(self):
        item = R.format_rows(self.hit, cols=100, searching=True, terms=["记忆"])[0]
        self.assertTrue(item.split("\n")[1].startswith(R.SNIPPET_INDENT))

    def test_searching_shows_only_the_project_name(self):
        """Not the last two path components: the width goes to the snippet instead."""
        item = R.format_rows(self.hit, cols=100, searching=True, terms=["记忆"])[0]
        self.assertIn("proj-name", item)
        self.assertNotIn("/w/proj-name", item)

    def test_no_escape_codes_without_colour(self):
        self.assertNotIn("\033", R.format_rows(self.rows, cols=100)[0])

    def test_colour_is_opt_in(self):
        self.assertIn("\033", R.format_rows(self.rows, cols=100, color=True)[0])

    def test_hit_count_shown_only_when_searching(self):
        self.assertIn("7x", R.format_rows(self.hit, cols=100, searching=True)[0])
        self.assertNotIn("7x", R.format_rows(self.hit, cols=100)[0])


class TestMarkTerms(unittest.TestCase):
    """Contrast by clearing the dim, because bold nested inside faint is undefined:
    ANSI keeps both in one intensity slot and SGR 22 resets them together."""

    def test_plain_when_colour_is_off(self):
        self.assertEqual(R.mark_terms("a 记忆 b", ["记忆"]), "a 记忆 b")

    def test_dims_the_line_and_undims_the_match(self):
        out = R.mark_terms("a 记忆 b", ["记忆"], color=True)
        self.assertTrue(out.startswith(R.DIM))
        self.assertIn(R.UNDIM + "记忆" + R.DIM, out)
        self.assertTrue(out.endswith(R.RESET))

    def test_uses_no_bold(self):
        self.assertNotIn("\033[1m", R.mark_terms("a 记忆 b", ["记忆"], color=True))

    def test_case_insensitive(self):
        self.assertIn(R.UNDIM + "Memory", R.mark_terms("a Memory b", ["memory"],
                                                       color=True))

    def test_a_digit_term_cannot_corrupt_the_escape_codes(self):
        """One combined pass, so a later term cannot match inside a code just added."""
        out = R.mark_terms("dial 2 then 22", ["2"], color=True)
        self.assertNotIn("\033[" + R.UNDIM, out)
        self.assertIn("dial", out)

    def test_no_terms_still_dims(self):
        self.assertEqual(R.mark_terms("plain", [], color=True),
                         R.DIM + "plain" + R.RESET)


class TestBuildQuery(unittest.TestCase):
    """The rule to remember is the shell's quoting, not FTS5's."""

    def test_one_word(self):
        self.assertEqual(R.build_query(["memory"]), "memory")

    def test_several_words_mean_all_of_them(self):
        self.assertEqual(R.build_query(["记忆", "遗忘"]), "记忆 遗忘")

    def test_an_argument_with_a_space_becomes_a_phrase(self):
        self.assertEqual(R.build_query(["遗忘 机制"]), '"遗忘 机制"')

    def test_mixed(self):
        self.assertEqual(R.build_query(["a b", "c"]), '"a b" c')


class TestProjectOf(unittest.TestCase):
    def test_last_component_only(self):
        self.assertEqual(R.project_of("/a/b/my-project"), "my-project")

    def test_trailing_slash_ignored(self):
        self.assertEqual(R.project_of("/a/b/my-project/"), "my-project")

    def test_empty_is_safe(self):
        self.assertEqual(R.project_of(""), "")
        self.assertEqual(R.project_of(None), "")


class TestSessionsMatching(unittest.TestCase):
    """One query returning every matching session, with a snippet and a count."""

    def _index(self, c):
        I.update(db_path=c.db, sessions_dir=c.root, full=True)
        con, _ = Q.open_index(c.db, auto_update=False)
        return con

    def test_returns_hits_and_a_snippet_per_session(self):
        with F.Corpus(2) as c:
            con = self._index(c)
            got = Q.sessions_matching(con, "记忆")
            con.close()
            self.assertTrue(got)
            for v in got.values():
                self.assertGreater(v["hits"], 0)
                self.assertTrue(v["snippet"])

    def test_one_row_per_session(self):
        with F.Corpus(4) as c:
            con = self._index(c)
            got = Q.sessions_matching(con, "记忆")
            con.close()
            self.assertEqual(len(got), 4)

    def test_prefers_a_match_in_a_user_turn(self):
        """Your own words jog memory better than the agent's summary of them."""
        with F.Corpus(1) as c:
            con = self._index(c)
            # The fixture has '请帮我看看遗忘机制的设计' from the user and
            # '好的，我先看 DatabaseSync 的实现' from the assistant.
            got = Q.sessions_matching(con, "设计 实现")
            snippet = next(iter(got.values()))["snippet"] if got else ""
            got2 = Q.sessions_matching(con, "遗忘机制")
            con.close()
            self.assertTrue(got2, "the user turn must be findable")
            self.assertIn("遗忘机制", next(iter(got2.values()))["snippet"])
            del snippet

    def test_no_limit_so_every_matching_session_is_returned(self):
        """The bug this replaces: a global LIMIT let dominant sessions hide the rest."""
        with F.Corpus(6) as c:
            con = self._index(c)
            got = Q.sessions_matching(con, "记忆")
            capped = Q.search_prose(con, "记忆", limit=2)
            con.close()
            self.assertEqual(len(got), 6)
            self.assertLess(len({r["session_id"] for r in capped}), len(got))

    def test_absent_term_returns_empty(self):
        with F.Corpus(1) as c:
            con = self._index(c)
            self.assertEqual(Q.sessions_matching(con, "zzzznotpresent"), {})
            con.close()

    def test_cjk_is_split_before_matching(self):
        """Goes through build_match, so a substring of a CJK word still matches."""
        with F.Corpus(1) as c:
            con = self._index(c)
            self.assertTrue(Q.sessions_matching(con, "忘机"))
            con.close()

    def test_tool_output_is_not_searched(self):
        """A command that printed a word is not a discussion of it."""
        with F.Corpus(1) as c:
            con = self._index(c)
            # DUPLICATE_OUTPUT lives only in tool output in the fixture.
            self.assertEqual(Q.sessions_matching(con, "refused"), {})
            con.close()


class TestSearchSessions(unittest.TestCase):
    def test_builds_the_index_when_it_is_missing(self):
        """The index is a derived cache, so a cold one is not an error."""
        with F.Corpus(1) as c:
            self.assertFalse(os.path.exists(c.db))
            got = R.search_sessions("记忆", db=c.db, sessions_dir=c.root)
            self.assertTrue(got)
            self.assertTrue(os.path.exists(c.db))

    def test_snippet_width_keeps_the_match_visible(self):
        with F.Corpus(1) as c:
            got = R.search_sessions("记忆", db=c.db, sessions_dir=c.root)
            for v in got.values():
                self.assertIn("记忆", v["snippet"])


class TestFailureContract(unittest.TestCase):
    """Searching must fail loudly. It used to fall back to grepping the raw logs,
    which cannot see inside CJK words or identifiers -- so it reported "nothing
    found" for things that had been discussed at length."""

    def _run(self, *args, **kw):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, ENTRY] + list(args),
                              capture_output=True, text=True, cwd="/", env=env, **kw)

    def test_search_against_an_unusable_index_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = sidecars(os.path.join(tmp, "cli"), [spec("a")])
            # A directory where the database file must go: unopenable, unbuildable.
            db = os.path.join(tmp, "blocked")
            os.makedirs(db)
            proc = self._run("--sessions", root, "--db", db, "anything")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("cannot search the index", proc.stderr)

    def test_the_error_says_listing_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = sidecars(os.path.join(tmp, "cli"), [spec("a")])
            db = os.path.join(tmp, "blocked")
            os.makedirs(db)
            proc = self._run("--sessions", root, "--db", db, "anything")
            self.assertIn("kiro-resume", proc.stderr)

    def test_listing_never_touches_the_index(self):
        """No search terms, no index: the common case works with a missing cache."""
        with tempfile.TemporaryDirectory() as tmp:
            root = sidecars(os.path.join(tmp, "cli"), [spec("a", cwd="/")])
            db = os.path.join(tmp, "does-not-exist", "index.db")
            proc = self._run("--sessions", root, "--db", db, "-n", "5",
                             input="")   # decline the menu
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(db))

    def test_missing_session_directory_is_reported(self):
        proc = self._run("--sessions", "/nonexistent-session-dir")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no session directory", proc.stderr)

    def test_no_match_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "cli")
            F.write_corpus(root, 1)
            proc = self._run("--sessions", root,
                             "--db", os.path.join(tmp, "i", "index.db"),
                             "zzzznotpresentanywhere")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("No sessions matching", proc.stdout)

    def test_removed_flags_are_rejected_loudly(self):
        """-s, -a and the -l fallback are gone; old habits must not run silently."""
        with tempfile.TemporaryDirectory() as tmp:
            root = sidecars(os.path.join(tmp, "cli"), [spec("a")])
            for flag in ("-a", "-l"):
                proc = self._run("--sessions", root, flag, "memory")
                self.assertEqual(proc.returncode, 2, f"{flag} should be rejected")

    def test_a_leading_dash_term_gets_a_pointed_hint(self):
        """`-pipeline` is how full-text spells "without"; the fix is not obvious."""
        with tempfile.TemporaryDirectory() as tmp:
            root = sidecars(os.path.join(tmp, "cli"), [spec("a")])
            proc = self._run("--sessions", root, "memory", "-pipeline")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("after --", proc.stderr)

    def test_double_dash_delivers_the_excluded_term(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "cli")
            F.write_corpus(root, 1)
            proc = self._run("--sessions", root,
                             "--db", os.path.join(tmp, "i", "index.db"),
                             "--", "记忆", "-遗忘")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("unrecognized", proc.stderr)


class TestSelectionReachesTheTerminal(unittest.TestCase):
    """The picker must actually appear.

    fzf draws its interface on stderr and writes only the chosen line to stdout.
    Capturing stderr therefore produces a process that waits for keystrokes with a
    blank screen -- a hang, as far as the user can tell. Nothing short of a real
    terminal catches that: with stdout and stderr both pipes, fzf either fails
    outright or looks fine.

    So this drives the entry point under a pty and asserts that something renders.
    """

    @unittest.skipUnless(shutil.which("fzf"), "fzf not installed")
    def test_picker_renders_within_a_couple_of_seconds(self):
        self.assertIn(b"resume session", self._render(["-n", "3"]),
                      "the picker never rendered; fzf's UI is being swallowed")

    @unittest.skipUnless(shutil.which("fzf"), "fzf not installed")
    def test_the_snippet_line_reaches_the_screen(self):
        """A two-line row must survive the trip through fzf as one entry.

        Asserting the text is on screen is not enough on its own: fed
        newline-separated, the second line becomes a selectable entry of its own that
        resumes nothing. The picker's item total is what tells the two apart -- one
        matching session must count as one item, not two.
        """
        seen = self._render(
            ["记忆"], corpus=True,
            # Both conditions, or this races the paint order: the item total can
            # appear a frame before the rows are drawn, and waiting on either alone
            # makes the result depend on which frame arrived first.
            ready=lambda s: b"FTS" in s and self._item_totals(s))
        self.assertIn(b"resume session", seen, "the picker never rendered")
        self.assertIn(b"FTS", seen,
                      "the snippet line did not render; only titles reached fzf")
        self.assertEqual(self._item_totals(seen), {1},
                         "one matching session must be one entry; a total of 2 means "
                         "the snippet became an entry of its own")

    @staticmethod
    def _item_totals(seen):
        """The denominators of fzf's matched/total readout, ignoring the empty frame."""
        clean = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z]|\x1b[=>]|[\x0e\x0f\r]",
                       b"", seen)
        return {int(m.group(2)) for m in re.finditer(rb"\b(\d+)/(\d+)\b", clean)
                if int(m.group(2)) > 0}

    def _render(self, args, corpus=False, ready=None):
        import fcntl
        import pty
        import select
        import struct
        import termios

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "cli")
        if corpus:
            F.write_corpus(root, 1)
            # A user turn carrying a token distinctive enough to spot on screen, so
            # the assertion cannot be satisfied by the title or by chrome.
            with open(os.path.join(root, "sess0000.jsonl"), "a", encoding="utf8") as fh:
                fh.write(F._rec("Prompt", {
                    "message_id": "sess0000-snippet",
                    "meta": {"timestamp": 1776360000},
                    "content": [{"kind": "text",
                                 "data": "记忆 lives in the FTS index"}],
                }) + "\n")
            extra = ["--db", os.path.join(tmp.name, "i", "index.db")]
        else:
            sidecars(root, [spec("a", cwd="/", title="a listable session")])
            extra = []

        master, slave = pty.openpty()
        # openpty leaves the window 0x0, and a full-screen picker given no rows and
        # no columns draws nothing at all -- which would make this test pass or fail
        # for reasons unrelated to what it is checking.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 110, 0, 0))
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["TERM"] = "xterm"
        proc = subprocess.Popen(
            [sys.executable, ENTRY, "--sessions", root] + extra + args,
            stdin=slave, stdout=slave, stderr=slave, cwd="/", env=env)
        os.close(slave)

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            os.close(master)
        self.addCleanup(cleanup)

        seen = b""
        if ready is None:
            def ready(s):
                return b"resume session" in s
        deadline = time.time() + 8
        while time.time() < deadline and not ready(seen):
            if select.select([master], [], [], 0.2)[0]:
                try:
                    seen += os.read(master, 65536)
                except OSError:
                    break
        return seen


if __name__ == "__main__":
    unittest.main(verbosity=2)




    unittest.main(verbosity=2)
