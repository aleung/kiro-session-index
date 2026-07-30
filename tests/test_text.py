"""Unit tests for ksi.text -- the transforms that make CJK searchable."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ksi import text as T


class TestHasCjk(unittest.TestCase):
    def test_detects_chinese(self):
        self.assertTrue(T.has_cjk("记忆"))

    def test_detects_mixed(self):
        self.assertTrue(T.has_cjk("docker容器"))

    def test_rejects_ascii_only(self):
        self.assertFalse(T.has_cjk("DatabaseSync"))

    def test_handles_empty(self):
        self.assertFalse(T.has_cjk(""))
        self.assertFalse(T.has_cjk(None))


class TestSplitIndex(unittest.TestCase):
    def test_splits_every_cjk_char(self):
        self.assertEqual(T.split_index("记忆").split(), ["记", "忆"])

    def test_leaves_ascii_intact(self):
        # Code must tokenize normally, so ASCII may not be touched.
        self.assertEqual(T.split_index("getUserName()"), "getUserName()")

    def test_mixed_keeps_ascii_together(self):
        self.assertEqual(T.split_index("docker容器").split(), ["docker", "容", "器"])

    def test_empty_is_safe(self):
        self.assertEqual(T.split_index(""), "")
        self.assertEqual(T.split_index(None), "")


class TestBuildMatch(unittest.TestCase):
    def test_single_cjk_term_becomes_phrase(self):
        self.assertEqual(T.build_match("记忆"), '"记 忆"')

    def test_ascii_term_is_quoted(self):
        self.assertEqual(T.build_match("memory"), '"memory"')

    def test_multi_term_uses_AND_not_one_phrase(self):
        """The handoff's appendix wrapped whole queries in one phrase.

        That silently returned zero rows for any multi-word query. This is the
        regression guard for it: the operator must be AND, and the two terms must
        stay separate expressions.
        """
        got = T.build_match("记忆 memory")
        self.assertEqual(got, '"记 忆" AND "memory"')
        self.assertIn(" AND ", got)
        self.assertNotEqual(got, '"记 忆 memory"')

    def test_three_terms_all_anded(self):
        self.assertEqual(T.build_match("docker 容器 部署"),
                         '"docker" AND "容 器" AND "部 署"')

    def test_quoted_group_is_a_phrase(self):
        self.assertEqual(T.build_match('"full text search"'), '"full text search"')

    def test_quoted_cjk_group_is_split_as_phrase(self):
        self.assertEqual(T.build_match('"遗忘机制"'), '"遗 忘 机 制"')

    def test_prefix_star_is_preserved(self):
        self.assertEqual(T.build_match("token*"), '"token"*')

    def test_negation_becomes_NOT(self):
        got = T.build_match("memory -pipeline")
        self.assertEqual(got, '"memory" NOT "pipeline"')

    def test_embedded_quote_is_escaped(self):
        # A stray quote must not break out of the FTS string literal.
        self.assertEqual(T.build_match('say"hi'), '"say""hi"')

    def test_empty_query_raises(self):
        # Must never silently produce an expression that matches everything.
        with self.assertRaises(ValueError):
            T.build_match("")
        with self.assertRaises(ValueError):
            T.build_match("   ")

    def test_bare_dash_is_not_treated_as_negation(self):
        self.assertEqual(T.build_match("-"), '"-"')


class TestQueryTerms(unittest.TestCase):
    def test_returns_raw_untransformed_terms(self):
        self.assertEqual(T.query_terms("记忆 memory"), ["记忆", "memory"])

    def test_strips_prefix_star(self):
        self.assertEqual(T.query_terms("token*"), ["token"])

    def test_drops_negated_terms(self):
        self.assertEqual(T.query_terms("memory -pipeline"), ["memory"])

    def test_keeps_quoted_phrase_whole(self):
        self.assertEqual(T.query_terms('"full text"'), ["full text"])


class TestSnip(unittest.TestCase):
    def test_centres_on_the_match(self):
        body = "x" * 100 + "TARGET" + "y" * 100
        got = T.snip(body, ["TARGET"], width=10)
        self.assertIn("TARGET", got)
        self.assertTrue(got.startswith("…"))
        self.assertTrue(got.endswith("…"))
        self.assertLess(len(got), 40)

    def test_cjk_snippet_is_readable(self):
        """The whole reason snip() exists: FTS5 snippet() would render CJK as
        '知  识  库' because the index stores split text."""
        body = "这个大小下,索引的表现怎样？后面还有很多内容"
        got = T.snip(body, ["索引"], width=8)
        self.assertIn("索引的表现", got)
        self.assertNotIn("索  引", got)

    def test_case_insensitive_match(self):
        self.assertIn("DatabaseSync", T.snip("uses DatabaseSync here", ["databasesync"]))

    def test_collapses_newlines(self):
        got = T.snip("line one\nline two\rline three", ["two"], width=20)
        self.assertNotIn("\n", got)
        self.assertNotIn("\r", got)

    def test_falls_back_to_head_when_term_absent(self):
        got = T.snip("some body text here", ["missing"], width=5)
        self.assertTrue(got.startswith("some"))

    def test_picks_earliest_matching_term(self):
        body = "alpha then beta"
        self.assertIn("alpha", T.snip(body, ["beta", "alpha"], width=3))

    def test_empty_text_is_safe(self):
        self.assertEqual(T.snip("", ["x"]), "")
        self.assertEqual(T.snip(None, ["x"]), "")

    def test_accepts_bare_string_term(self):
        self.assertIn("beta", T.snip("alpha beta", "beta", width=4))


if __name__ == "__main__":
    unittest.main(verbosity=2)
