"""Validates skill/SKILL.md against the create-skill checklist.

These are objective rules, so they are enforced here rather than re-checked by hand.
The index tier (name + description) is the expensive one: it is loaded on every session
whether or not the skill activates, which is why the word and trigger limits are hard.
"""

import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(REPO, "skill", "SKILL.md")

MAX_DESC_WORDS = 50
MAX_BODY_TOKENS = 5000
MIN_TRIGGERS, MAX_TRIGGERS = 2, 5


def load():
    with open(SKILL, encoding="utf8") as fh:
        raw = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    assert m, "SKILL.md must start with a YAML frontmatter block"
    return m.group(1), m.group(2)


def field(frontmatter, key):
    """Read a top-level scalar, tolerating quotes. Avoids a pyyaml dependency."""
    m = re.search(rf"^{key}:\s*(.*)$", frontmatter, re.M)
    return m.group(1).strip().strip('"').strip("'") if m else None


def est_tokens(text):
    """CJK is roughly one token per character; latin roughly four characters."""
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff]", text))
    return cjk + (len(text) - cjk) / 4


class TestFrontmatter(unittest.TestCase):
    def setUp(self):
        self.fm, self.body = load()

    def test_name_matches_directory_convention(self):
        self.assertEqual(field(self.fm, "name"), "session-history")

    def test_name_is_kebab_case(self):
        self.assertRegex(field(self.fm, "name"), r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_description_present(self):
        self.assertTrue(field(self.fm, "description"))

    def test_description_within_word_budget(self):
        n = len(field(self.fm, "description").split())
        self.assertLessEqual(n, MAX_DESC_WORDS, f"description is {n} words")

    def test_description_is_a_routing_trigger(self):
        self.assertTrue(field(self.fm, "description").startswith("Load when"))

    def test_trigger_phrase_count(self):
        trig = re.findall(r"'([^']+)'", field(self.fm, "description"))
        self.assertGreaterEqual(len(trig), MIN_TRIGGERS)
        self.assertLessEqual(len(trig), MAX_TRIGGERS, f"{len(trig)} triggers")

    def test_includes_chinese_triggers(self):
        """The user works in Chinese; routing must fire on Chinese phrasing."""
        trig = re.findall(r"'([^']+)'", field(self.fm, "description"))
        self.assertTrue(any(re.search(r"[\u4e00-\u9fff]", t) for t in trig))

    def test_description_does_not_summarise_implementation(self):
        desc = field(self.fm, "description").lower()
        for leak in ("sqlite", "fts5", "contentless", "python"):
            self.assertNotIn(leak, desc, f"description leaks implementation: {leak}")


class TestBody(unittest.TestCase):
    def setUp(self):
        self.fm, self.body = load()

    def test_within_token_budget(self):
        n = est_tokens(self.body)
        self.assertLessEqual(n, MAX_BODY_TOKENS, f"body is ~{n:.0f} tokens")

    def test_gotchas_section_present(self):
        self.assertIn("## Gotchas", self.body)

    def test_gotchas_invites_appending(self):
        self.assertIn("Append here when the agent fails", self.body)

    def test_gotchas_are_not_empty(self):
        section = self.body.split("## Gotchas", 1)[1]
        bullets = [l for l in section.splitlines() if l.strip().startswith("- ")]
        self.assertGreaterEqual(len(bullets), 3)

    def test_contains_negative_instructions(self):
        n = len(re.findall(r"\b(Never|Do not|does not|cannot)\b", self.body))
        self.assertGreaterEqual(n, 3, "expected explicit negative guidance")

    def test_both_entry_points_documented(self):
        self.assertIn("ksi-query", self.body)
        self.assertIn("ksi-index", self.body)

    def test_tool_dir_placeholder_is_used(self):
        """setup.sh substitutes this; a hardcoded path would break relocation."""
        self.assertIn("@TOOL_DIR@", self.body)

    def test_no_absolute_home_paths_for_tools(self):
        """setup.sh substitutes @TOOL_DIR@; a literal /home/<user>/... path would
        mean the placeholder leaked someone's real install location instead."""
        self.assertNotRegex(self.body, r"/home/[^/\s]+/")

    def test_documents_the_snippet_guard(self):
        self.assertIn("snip(", self.body)
        self.assertIn("snippet()", self.body)

    def test_documents_the_citation_anchor(self):
        self.assertIn("message_id:content_index", self.body)

    def test_schema_lists_every_queryable_table(self):
        for table in ("sessions", "messages", "tool_calls", "tool_output",
                      "tool_results", "messages_fts", "tool_output_fts", "files"):
            self.assertIn(table, self.body, f"schema omits {table}")

    def test_schema_matches_actual_columns(self):
        """A drifted schema block sends the agent writing SQL against wrong columns."""
        import sqlite3
        import sys
        sys.path.insert(0, REPO)
        from tests import fixtures as F
        from ksi import index as I
        with F.Corpus(1) as c:
            I.update(db_path=c.db, sessions_dir=c.root, full=True)
            con = sqlite3.connect(c.db)
            try:
                for table in ("sessions", "messages", "tool_calls", "tool_output",
                              "tool_results", "files"):
                    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
                    for col in cols:
                        self.assertIn(col, self.body,
                                      f"{table}.{col} missing from documented schema")
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
