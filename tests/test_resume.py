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


def sidecars(root, specs):
    """Write .json sidecars only -- no .jsonl -- and return the directory.

    Sidecar-only is the interesting shape: it is exactly what the index cannot
    see, since a sessions row is created while parsing the .jsonl.
    """
    os.makedirs(root, exist_ok=True)
    for spec in specs:
        with open(os.path.join(root, spec["session_id"] + ".json"), "w") as fh:
            json.dump(spec, fh)
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
    def test_reads_sidecars_without_any_jsonl(self):
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

    def test_subagent_sessions_never_appear(self):
        self.assertNotIn("sub1", self._ids(all_dirs=True))

    def test_filters_to_the_current_directory(self):
        self.assertEqual(self._ids(cwd="/w/a"), ["top1"])

    def test_all_dirs_shows_everything_top_level(self):
        self.assertEqual(set(self._ids(all_dirs=True)), {"top1", "top2"})

    def test_newest_first(self):
        self.assertEqual(self._ids(all_dirs=True), ["top2", "top1"])

    def test_limit_applies(self):
        self.assertEqual(len(self._ids(all_dirs=True, limit=1)), 1)

    def test_hits_filter_excludes_unmatched(self):
        self.assertEqual(self._ids(all_dirs=True, hits={"top1": 3}), ["top1"])

    def test_hit_band_sorts_ahead_of_recency(self):
        """An old session that matched a lot must not be buried by new near-misses."""
        hits = {"top1": R.BAND_THRESHOLD, "top2": 1}
        self.assertEqual(self._ids(all_dirs=True, hits=hits), ["top1", "top2"])

    def test_within_a_band_recency_still_wins(self):
        hits = {"top1": 1, "top2": 2}
        self.assertEqual(self._ids(all_dirs=True, hits=hits), ["top2", "top1"])

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

    def test_line_fits_the_terminal(self):
        for cols in (40, 60, 100, 200):
            line = R.format_rows(self.rows, cols=cols)[0]
            self.assertLessEqual(R.display_width(line), cols)

    def test_no_escape_codes_without_colour(self):
        self.assertNotIn("\033", R.format_rows(self.rows, cols=100)[0])

    def test_colour_is_opt_in(self):
        self.assertIn("\033", R.format_rows(self.rows, cols=100, color=True)[0])

    def test_hit_count_shown_only_when_asked(self):
        rows = R.list_sessions({"s": spec("s")}, all_dirs=True, hits={"s": 7})
        self.assertIn("7x", R.format_rows(rows, cols=100, show_hits=True)[0])
        self.assertNotIn("7x", R.format_rows(rows, cols=100, show_hits=False)[0])


class TestCountBySession(unittest.TestCase):
    """The aggregate exists to return every matching session, not the top N rows."""

    def test_counts_prose_hits_per_session(self):
        with F.Corpus(2) as c:
            I.update(db_path=c.db, sessions_dir=c.root, full=True)
            con, _ = Q.open_index(c.db, auto_update=False)
            counts = Q.count_by_session(con, "记忆")
            con.close()
            self.assertTrue(counts)
            self.assertTrue(all(isinstance(v, int) and v > 0 for v in counts.values()))

    def test_counts_tool_output_separately(self):
        with F.Corpus(1) as c:
            I.update(db_path=c.db, sessions_dir=c.root, full=True)
            con, _ = Q.open_index(c.db, auto_update=False)
            self.assertTrue(Q.count_by_session(con, "refused", tool_output=True))
            con.close()

    def test_absent_term_returns_empty(self):
        with F.Corpus(1) as c:
            I.update(db_path=c.db, sessions_dir=c.root, full=True)
            con, _ = Q.open_index(c.db, auto_update=False)
            self.assertEqual(Q.count_by_session(con, "zzzznotpresent"), {})
            con.close()

    def test_no_limit_so_every_matching_session_is_returned(self):
        """The bug this replaces: a global LIMIT let dominant sessions hide the rest."""
        with F.Corpus(6) as c:
            I.update(db_path=c.db, sessions_dir=c.root, full=True)
            con, _ = Q.open_index(c.db, auto_update=False)
            counts = Q.count_by_session(con, "记忆")
            capped = Q.search_prose(con, "记忆", limit=2)
            con.close()
            self.assertEqual(len(counts), 6)
            self.assertLess(len({r["session_id"] for r in capped}), len(counts))

    def test_cjk_is_split_before_matching(self):
        """Goes through build_match, so a substring of a CJK word still matches."""
        with F.Corpus(1) as c:
            I.update(db_path=c.db, sessions_dir=c.root, full=True)
            con, _ = Q.open_index(c.db, auto_update=False)
            self.assertTrue(Q.count_by_session(con, "忘机"))
            con.close()


class TestCountHits(unittest.TestCase):
    def test_merges_prose_and_tool_output(self):
        with F.Corpus(2) as c:
            I.update(db_path=c.db, sessions_dir=c.root, full=True)
            prose = R.count_hits("记忆", db=c.db, sessions_dir=c.root)
            self.assertTrue(prose)

    def test_builds_the_index_when_it_is_missing(self):
        """The index is a derived cache, so a cold one is not an error."""
        with F.Corpus(1) as c:
            self.assertFalse(os.path.exists(c.db))
            self.assertTrue(R.count_hits("记忆", db=c.db, sessions_dir=c.root))
            self.assertTrue(os.path.exists(c.db))


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
            proc = self._run("--sessions", root, "--db", db, "-s", "anything")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("cannot search the index", proc.stderr)

    def test_the_error_says_listing_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = sidecars(os.path.join(tmp, "cli"), [spec("a")])
            db = os.path.join(tmp, "blocked")
            os.makedirs(db)
            proc = self._run("--sessions", root, "--db", db, "-s", "anything")
            self.assertIn("kiro-resume", proc.stderr)

    def test_listing_never_touches_the_index(self):
        """No search, no index: the common case works with a missing cache."""
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
                             "-s", "zzzznotpresentanywhere")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("No sessions matching", proc.stdout)

    def test_removed_flags_are_rejected_loudly(self):
        """-a and the -l fallback are gone; old muscle memory must not run silently."""
        with tempfile.TemporaryDirectory() as tmp:
            root = sidecars(os.path.join(tmp, "cli"), [spec("a")])
            proc = self._run("--sessions", root, "-a")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("unrecognized arguments", proc.stderr)


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
        import fcntl
        import pty
        import select
        import struct
        import termios

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = sidecars(os.path.join(tmp.name, "cli"),
                        [spec("a", cwd="/", title="a listable session")])

        master, slave = pty.openpty()
        # openpty leaves the window 0x0, and a full-screen picker given no rows and
        # no columns draws nothing at all -- which would make this test pass or fail
        # for reasons unrelated to what it is checking.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["TERM"] = "xterm"
        proc = subprocess.Popen(
            [sys.executable, ENTRY, "--sessions", root, "-n", "3"],
            stdin=slave, stdout=slave, stderr=slave, cwd="/", env=env)
        os.close(slave)

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            os.close(master)
        self.addCleanup(cleanup)

        seen = b""
        deadline = time.time() + 5
        while time.time() < deadline and b"resume session" not in seen:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    seen += os.read(master, 4096)
                except OSError:
                    break

        self.assertIn(b"resume session", seen,
                      "the picker never rendered; fzf's UI is being swallowed")


if __name__ == "__main__":
    unittest.main(verbosity=2)




    unittest.main(verbosity=2)
