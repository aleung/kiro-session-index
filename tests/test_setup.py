"""Tests for setup.sh -- the install must be self-contained.

The requirement being verified: after setup, the source repo is not used at runtime.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fixtures as F

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP = os.path.join(REPO, "setup.sh")

EXPECTED = [
    "SKILL.md",
    "scripts/ksi-index",
    "scripts/ksi-query",
    "scripts/ksi/__init__.py",
    "scripts/ksi/index.py",
    "scripts/ksi/query.py",
    "scripts/ksi/text.py",
    "scripts/ksi/schema.sql",
]


@unittest.skipUnless(shutil.which("bash"), "bash not available")
class TestSetup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.skill_dir = os.path.join(cls.tmp.name, "session-history")
        proc = subprocess.run(
            ["bash", SETUP, "--skill-dir", cls.skill_dir, "--no-index"],
            capture_output=True, text=True, cwd="/")
        cls.proc = proc
        if proc.returncode != 0:
            raise AssertionError(f"setup.sh failed:\n{proc.stdout}\n{proc.stderr}")
        cls.tools = os.path.join(cls.skill_dir, "scripts")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_setup_succeeds(self):
        self.assertEqual(self.proc.returncode, 0)

    def test_setup_reports_self_containment(self):
        self.assertIn("self-contained", self.proc.stdout)

    def test_all_files_installed(self):
        for rel in EXPECTED:
            self.assertTrue(os.path.exists(os.path.join(self.skill_dir, rel)),
                            f"missing {rel}")

    def test_entry_points_are_executable(self):
        for name in ("ksi-index", "ksi-query"):
            self.assertTrue(os.access(os.path.join(self.tools, name), os.X_OK), name)

    def test_no_reference_to_source_repo_anywhere(self):
        """The core requirement: nothing in the install points back at the repo."""
        offenders = []
        for root, _, files in os.walk(self.skill_dir):
            for name in files:
                p = os.path.join(root, name)
                with open(p, encoding="utf8", errors="replace") as fh:
                    if REPO in fh.read():
                        offenders.append(os.path.relpath(p, self.skill_dir))
        self.assertEqual(offenders, [])

    def test_placeholder_was_substituted(self):
        with open(os.path.join(self.skill_dir, "SKILL.md"), encoding="utf8") as fh:
            body = fh.read()
        self.assertNotIn("@TOOL_DIR@", body)
        self.assertIn("ksi-query", body)

    def test_frontmatter_survives_substitution(self):
        with open(os.path.join(self.skill_dir, "SKILL.md"), encoding="utf8") as fh:
            head = fh.read(400)
        self.assertTrue(head.startswith("---"))
        self.assertIn("name: session-history", head)

    def _run_installed(self, *args, corpus_root=None, db=None):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)  # prove no ambient path is doing the work
        if corpus_root:
            env["KSI_SESSIONS"] = corpus_root
        if db:
            env["KSI_DB"] = db
        return subprocess.run([os.path.join(self.tools, args[0])] + list(args[1:]),
                              capture_output=True, text=True, cwd="/", env=env)

    def test_installed_query_runs_from_unrelated_cwd(self):
        proc = self._run_installed("ksi-query", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ksi-query", proc.stdout)

    def test_installed_copy_indexes_and_searches_standalone(self):
        with F.Corpus(2) as c:
            built = self._run_installed("ksi-index", "--full",
                                        corpus_root=c.root, db=c.db)
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertIn("indexed 2 file(s)", built.stdout)

            found = self._run_installed("ksi-query", "记忆 系统",
                                        corpus_root=c.root, db=c.db)
            self.assertEqual(found.returncode, 0, found.stderr)
            self.assertIn("cite:", found.stdout)

    def test_installed_copy_enforces_the_snippet_guard(self):
        with F.Corpus(1) as c:
            self._run_installed("ksi-index", "--full", corpus_root=c.root, db=c.db)
            proc = self._run_installed(
                "ksi-query", "--sql",
                "SELECT snippet(messages_fts,0,'','','',5) FROM messages_fts",
                corpus_root=c.root, db=c.db)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("snip(", proc.stderr)

    def test_reinstall_removes_stale_files(self):
        stale = os.path.join(self.tools, "ksi", "obsolete_module.py")
        with open(stale, "w") as fh:
            fh.write("# left over from an older version\n")
        proc = subprocess.run(
            ["bash", SETUP, "--skill-dir", self.skill_dir, "--no-index"],
            capture_output=True, text=True, cwd="/")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(stale),
                         "reinstall must not leave stale modules behind")

    def test_no_bytecode_written_into_install_dir(self):
        """The install lives inside ~/.kiro, a git repo -- keep __pycache__ out of it."""
        with F.Corpus(1) as c:
            self._run_installed("ksi-index", "--full", corpus_root=c.root, db=c.db)
            self._run_installed("ksi-query", "记忆", corpus_root=c.root, db=c.db)
        found = [os.path.relpath(os.path.join(root, d), self.skill_dir)
                 for root, dirs, _ in os.walk(self.skill_dir)
                 for d in dirs if d == "__pycache__"]
        self.assertEqual(found, [])

    def test_rejects_unknown_option(self):
        proc = subprocess.run(["bash", SETUP, "--nonsense"],
                              capture_output=True, text=True, cwd="/")
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
