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
    "scripts/kiro-resume",
    "scripts/ksi-index",
    "scripts/ksi-query",
    "scripts/ksi/__init__.py",
    "scripts/ksi/index.py",
    "scripts/ksi/query.py",
    "scripts/ksi/text.py",
    "scripts/ksi/schema.sql",
]

# Which tools setup.sh puts on PATH. ksi-index is deliberately absent: every query
# refreshes the index, so a manual rebuild is rare.
EXPECTED_LINKS = ["kiro-resume", "ksi-query"]


@unittest.skipUnless(shutil.which("bash"), "bash not available")
class TestSetup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.skill_dir = os.path.join(cls.tmp.name, "session-history")
        # Never the real ~/bin: this suite installs and reinstalls repeatedly.
        cls.bin_dir = os.path.join(cls.tmp.name, "bin")
        proc = subprocess.run(
            ["bash", SETUP, "--skill-dir", cls.skill_dir,
             "--bin-dir", cls.bin_dir, "--no-index"],
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
        for name in ("ksi-index", "ksi-query", "kiro-resume"):
            self.assertTrue(os.access(os.path.join(self.tools, name), os.X_OK), name)

    def test_path_links_created(self):
        for name in EXPECTED_LINKS:
            link = os.path.join(self.bin_dir, name)
            self.assertTrue(os.path.islink(link), f"{name} is not a symlink")
            self.assertEqual(os.path.realpath(link),
                             os.path.realpath(os.path.join(self.tools, name)))

    def test_index_tool_is_not_linked(self):
        """Deliberate: refreshing happens on every query, so ksi-index stays unlinked."""
        self.assertFalse(os.path.exists(os.path.join(self.bin_dir, "ksi-index")))

    def test_linked_tool_runs_through_the_symlink(self):
        """The symlink is what makes `import ksi` resolve from a PATH directory.

        CPython resolves the link before computing sys.path[0], so the entry point
        finds the package beside its real location rather than beside the link.
        """
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        proc = subprocess.run([os.path.join(self.bin_dir, "kiro-resume"), "--help"],
                              capture_output=True, text=True, cwd="/", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_link_replaces_a_pre_existing_plain_file(self):
        """Installing over a hand-written script of the same name must win."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            victim = os.path.join(bin_dir, "kiro-resume")
            with open(victim, "w") as fh:
                fh.write("#!/bin/sh\necho an older hand-written copy\n")
            proc = subprocess.run(
                ["bash", SETUP, "--skill-dir", os.path.join(tmp, "skill"),
                 "--bin-dir", bin_dir, "--no-index"],
                capture_output=True, text=True, cwd="/")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.path.islink(victim))

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
            ["bash", SETUP, "--skill-dir", self.skill_dir,
             "--bin-dir", self.bin_dir, "--no-index"],
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


class TestInvocation(unittest.TestCase):
    """setup.sh must behave identically however it is started.

    `sh setup.sh` bypasses the shebang, and /bin/sh is dash on this system: it has no
    `set -o pipefail` and no ${BASH_SOURCE}, so the script re-execs itself under bash.
    """

    def _install(self, argv):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        skill_dir = os.path.join(tmp.name, "session-history")
        proc = subprocess.run(
            argv + ["--skill-dir", skill_dir,
                    "--bin-dir", os.path.join(tmp.name, "bin"), "--no-index"],
            capture_output=True, text=True, cwd=REPO)
        installed = sorted(
            os.path.relpath(os.path.join(r, f), skill_dir)
            for r, _, fs in os.walk(skill_dir) for f in fs)
        return proc, installed

    def test_sh_invocation_succeeds(self):
        proc, installed = self._install(["sh", "setup.sh"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("pipefail", proc.stderr)
        self.assertEqual(sorted(EXPECTED), installed)

    @unittest.skipUnless(shutil.which("dash"), "dash not available")
    def test_dash_invocation_succeeds(self):
        proc, installed = self._install(["dash", "setup.sh"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(EXPECTED), installed)

    def test_bash_invocation_succeeds(self):
        proc, installed = self._install(["bash", "setup.sh"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(EXPECTED), installed)

    def test_direct_execution_succeeds(self):
        proc, installed = self._install([os.path.join(REPO, "setup.sh")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(EXPECTED), installed)

    def test_all_shells_produce_identical_installs(self):
        _, a = self._install(["sh", "setup.sh"])
        _, b = self._install(["bash", "setup.sh"])
        self.assertEqual(a, b)

    def test_help_works_under_sh(self):
        proc = subprocess.run(["sh", "setup.sh", "--help"],
                              capture_output=True, text=True, cwd=REPO)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--skill-dir", proc.stdout)
        self.assertIn("--bin-dir", proc.stdout)
        self.assertIn("--no-index", proc.stdout)
        self.assertNotIn("#", proc.stdout)  # comment markers stripped

    def test_unknown_option_rejected_under_sh(self):
        proc = subprocess.run(["sh", "setup.sh", "--nonsense"],
                              capture_output=True, text=True, cwd=REPO)
        self.assertEqual(proc.returncode, 2)

    def test_reexec_guard_is_posix_parseable(self):
        """The guard runs before any bash-only construct, so dash must parse it."""
        with open(SETUP, encoding="utf8") as fh:
            lines = fh.read().splitlines()
        guard = next(i for i, l in enumerate(lines) if "BASH_VERSION" in l)
        pipefail = next(i for i, l in enumerate(lines) if "pipefail" in l
                        and l.strip().startswith("set "))
        self.assertLess(guard, pipefail,
                        "the bash guard must precede `set -o pipefail`")


if __name__ == "__main__":
    unittest.main(verbosity=2)
