# @module: test_compile_wiki_union
# @role: the gate under union mode - proves a namespace indexes its own notes from frontmatter, keeps
#        only the out-of-root entries from INDEX.md, and rebuilds when a note the list never named
#        changes, which is the exact failure that froze a second corpus for 26.7 days
# @deps: compile_wiki_index
# @used-by: run by hand after editing compile_target / namespace_targets
"""Each arm names the defect it would catch, and one of them is the defect that actually happened.

A manual membership list cannot report what it is missing. `needs_rebuild` walked only the entries
INDEX.md named, so a note nobody added was invisible twice - it never triggered a rebuild and it
never entered the corpus. Seven notes were newer than the index and the list knew none of them.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compile_wiki_index as cwi   # noqa: E402


def note(path: Path, tags: str, summary: str, body: str = "text") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntags: [%s]\nsummary: %s\n---\n\n%s\n" % (tags, summary, body),
                    encoding="utf-8")


class UnionTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="union_"))
        self.vault = self.tmp / "vault"
        self.outside = self.tmp / "personal"
        self.out = self.tmp / "index_compiled_ns.json"
        note(self.vault / "health" / "sleep.md", "сон, режим", "how sleep works")
        note(self.outside / "Книги" / "book.md", "книга", "a book that lives elsewhere")
        (self.vault / "INDEX.md").write_text(
            "# INDEX\n\n- [book](../personal/Книги/book.md) [книга, чтение]\n", encoding="utf-8")

    def compile(self):
        return cwi.compile_target(self.vault, self.vault / "INDEX.md", self.out, "union")

    def entries(self):
        return json.loads(self.out.read_text(encoding="utf-8"))

    def test_an_in_vault_note_is_indexed_without_being_listed(self):
        """The whole point. Nothing names sleep.md in INDEX.md, and it must still be in the corpus."""
        self.compile()
        self.assertIn("sleep", [e["name"] for e in self.entries()])

    def test_an_out_of_root_entry_survives_from_the_list(self):
        """The one job only the membership list can do: 46 of one corpus's 71 entries live outside."""
        self.compile()
        paths = [e["path"] for e in self.entries()]
        self.assertIn("../personal/Книги/book.md", paths)

    def test_a_note_both_sources_name_appears_once(self):
        (self.vault / "INDEX.md").write_text(
            "# INDEX\n\n- [sleep](health/sleep.md) [сон]\n"
            "- [book](../personal/Книги/book.md) [книга]\n", encoding="utf-8")
        self.compile()
        self.assertEqual([e["name"] for e in self.entries()].count("sleep"), 1)

    def test_an_UNLISTED_note_changing_triggers_a_rebuild(self):
        """The frozen-corpus defect, stated as a test. Under the old `index` source this returned 0
        and the corpus stayed 26.7 days old while its newest note was two days old."""
        self.compile()
        time.sleep(1.1)
        note(self.vault / "health" / "sleep.md", "сон, режим", "how sleep works", "rewritten")
        note(self.vault / "fitness" / "new.md", "спорт", "a note nobody listed")
        self.assertTrue(self.compile(), "a change the list cannot see must still rebuild")
        self.assertIn("new", [e["name"] for e in self.entries()])

    def test_nothing_changed_means_no_rebuild(self):
        self.compile()
        self.assertEqual(self.compile(), 0, "an unchanged corpus must not be rewritten")


class RedHalfTest(unittest.TestCase):
    """The old `index` source, run against the same tree: it must MISS what union catches."""

    def test_index_mode_cannot_see_an_unlisted_note(self):
        tmp = Path(tempfile.mkdtemp(prefix="union_red_"))
        vault = tmp / "vault"
        out = tmp / "index_compiled_ns.json"
        note(vault / "health" / "sleep.md", "сон", "listed nowhere")
        (vault / "INDEX.md").write_text("# INDEX\n\n- [other](other.md) [x]\n", encoding="utf-8")
        note(vault / "other.md", "x", "the only listed note")
        cwi.compile_target(vault, vault / "INDEX.md", out, "index")
        names = [e["name"] for e in json.loads(out.read_text(encoding="utf-8"))]
        self.assertNotIn("sleep", names, "if index mode saw it, this test no longer proves anything")


if __name__ == "__main__":
    unittest.main()
