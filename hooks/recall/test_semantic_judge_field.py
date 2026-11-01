# @module: test_semantic_judge_field
# @role: proves the search result names a judge only when one answered, and that a judge outage leaves stdout parseable for the MCP server
# @deps: semantic (sibling module)
# @used-by: CI, and by hand after touching rerank or the JSON branch; `python3 hooks/recall/test_semantic_judge_field.py`
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import semantic  # noqa: E402

JUDGE = "some-judge"
HIT = {"score": 0.6, "path": "p.md", "name": "a-note", "summary": "s", "matched": [], "matched_in": "summary"}


def unreachable(_query, name, *_rest):
    semantic.RERANK_UNREACHABLE.append(name)


def timed_out(_query, name, *_rest):
    semantic.RERANK_ABANDONED.append(name)


def run(judge=lambda *_: 8.0, hits=(HIT,), configured=JUDGE, flags=("--rerank", "--json")):
    for name, value in vars(semantic).items():
        if name.startswith("RERANK_") and isinstance(value, list):
            value.clear()
    out = io.StringIO()
    with mock.patch.object(semantic, "RERANK_MODEL", configured), \
            mock.patch.object(semantic, "effective_model", return_value="bge-m3"), \
            mock.patch.object(semantic, "corpus_out_of_scope", return_value=None), \
            mock.patch.object(semantic, "search", return_value=[dict(h) for h in hits]), \
            mock.patch.object(semantic, "log_fires"), \
            mock.patch.object(semantic, "judge_once", side_effect=judge), \
            mock.patch.object(sys, "argv", ["semantic.py", "search", "q", *flags]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        semantic.main()
    return out.getvalue()


def run_json(**kwargs):
    return json.loads(run(**kwargs))


class WhoAnswered(unittest.TestCase):
    def test_the_judge_answered(self):
        found = run_json()
        self.assertEqual((found["judge"], found["judge_asked"], found["note"]), (JUDGE, JUDGE, None))

    def test_an_unreachable_judge_is_not_named_and_stdout_stays_json(self):
        found = run_json(judge=unreachable)
        self.assertEqual((found["judge"], found["unreachable"]), (None, 1))
        self.assertIn(JUDGE, found["note"])

    def test_a_timed_out_judge_is_not_named_and_stdout_stays_json(self):
        found = run_json(judge=timed_out)
        self.assertEqual((found["judge"], found["abandoned"]), (None, 1))

    def test_no_model_configured(self):
        found = run_json(configured="")
        self.assertIsNone(found["judge"])
        self.assertIn("CLAUDE_RERANK_MODEL", found["note"])

    def test_without_rerank_there_is_nothing_to_explain(self):
        found = run_json(flags=("--json",))
        self.assertEqual((found["judge"], found["note"]), (None, None))


class EmptyResults(unittest.TestCase):
    def test_a_rejection_names_who_rejected(self):
        found = run_json(judge=lambda *_: 1.0)
        self.assertEqual((found["verdict"], found["judge"]), ("rejected", JUDGE))

    def test_an_empty_index_is_not_a_rejection(self):
        found = run_json(hits=())
        self.assertEqual((found["verdict"], found["judge"]), ("no-index", None))


class PlainText(unittest.TestCase):
    def test_the_header_names_who_answered(self):
        self.assertIn(f"+ {JUDGE}", run(flags=("--rerank",)))


if __name__ == "__main__":
    unittest.main()
