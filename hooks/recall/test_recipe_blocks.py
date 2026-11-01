# @module: test_recipe_blocks
# @role: proves a damaged recipe file loses nothing silently - later recipes still compile, what no reader can see is named, and a failed promotion leaves the candidate in place
# @deps: compile_procedural (run as a script), skills/procedural/scripts/lint_procedural.py (run as a script)
# @used-by: CI, and by hand after touching the recipe format
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPILER = HERE / "compile_procedural.py"
LINT = HERE.parents[1] / "skills" / "procedural" / "scripts" / "lint_procedural.py"

FIRST = "---\nslug: first\ntrigger: first trigger\nkeywords: [a]\nstatus: active\nsource_sessions: [s1]\n---\n1. step\nWhy: because\n\n"
LOST = "slug: lost\ntrigger: lost trigger\nkeywords: [b]\nstatus: active\nsource_sessions: [s2]\n---\n1. step\nWhy: because\n\n"
LAST = "---\nslug: last\ntrigger: last trigger\nkeywords: [c]\nstatus: active\nsource_sessions: [s3]\n---\n1. step\nWhy: because\n"
NO_TRIGGER = "---\nslug: broken\nkeywords: [d]\nstatus: candidate\nsource_sessions: [s4]\n---\n1. step\nWhy: because\n"


class RecipeFile(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="recipes_")
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def run_script(self, *argv):
        return subprocess.run([sys.executable, *map(str, argv)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    def compile(self, text):
        (self.dir / "procedural.md").write_text(text, encoding="utf-8")
        result = self.run_script(COMPILER, "--dir", self.dir)
        compiled = json.loads((self.dir / "procedural_compiled.json").read_text(encoding="utf-8"))
        return [entry["name"] for entry in compiled], result.stdout

    def test_a_block_without_frontmatter_hides_nothing_after_it(self):
        names, _ = self.compile(FIRST + LOST + LAST)
        self.assertEqual(names, ["first", "last"])

    def test_a_recipe_no_reader_can_see_is_named(self):
        _, said = self.compile(FIRST + LOST + LAST)
        self.assertIn("lost", said)
        self.assertIn("[ERROR]", said)

    def test_a_file_of_the_wrong_shape_says_so_on_stdout(self):
        names, said = self.compile("# Procedural memory\n\n## a recipe in the wrong shape\nsteps\n")
        self.assertEqual(names, [])
        self.assertIn("0 active recipes", said)

    def test_a_good_file_raises_nothing(self):
        _, said = self.compile(FIRST + LAST)
        self.assertNotIn("[ERROR]", said)
        self.assertNotIn("[WARNING]", said)

    def test_a_candidate_that_would_not_compile_stays_a_candidate(self):
        candidates = self.dir / "procedural_candidates.md"
        candidates.write_text(NO_TRIGGER, encoding="utf-8")
        self.run_script(LINT, "--dir", self.dir, "auto-promote", "--min-sessions", "1")
        self.assertIn("slug: broken", candidates.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
