# @module: test_compile_judge_schema
# @role: proves both /compile judge calls ask for a real JSON Schema, a window past ollama's silent 4096, and the IPv4 literal
# @deps: compile_judge (sibling)
# @used-by: CI, and by hand after touching the judge payloads
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.pop("CLAUDE_EMBED_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import compile_judge  # noqa: E402


def payload_of(reply, call):
    seen = []

    def fake_urlopen(req, timeout=0):
        seen.append(req)
        return io.BytesIO(json.dumps({"response": reply}).encode("utf-8"))

    with mock.patch.object(compile_judge.urllib.request, "urlopen", fake_urlopen):
        call()
    return seen[0].full_url, json.loads(seen[0].data)


class JudgeCalls(unittest.TestCase):
    def assert_constrained(self, url, payload, *required):
        self.assertIn("127.0.0.1", url)
        self.assertIsInstance(payload["format"], dict, "format must be a JSON Schema, not the word json")
        self.assertEqual(payload["format"]["required"], list(required))
        self.assertGreaterEqual(payload["options"]["num_ctx"], 32768)

    def test_the_verdict_call(self):
        reply = '{"action": "reject", "target": "", "reason": "x"}'
        with mock.patch.object(compile_judge, "retrieve", return_value=([], "")):
            url, payload = payload_of(reply, lambda: compile_judge.judge_one(("idea", "label", "body"), "code"))
        self.assert_constrained(url, payload, "action", "target", "reason")

    def test_the_second_look_call(self):
        note = Path(__file__)
        url, payload = payload_of('{"holds": false, "why": "x"}',
                                  lambda: compile_judge.body_holds_item({"path": str(note)}, "an item"))
        self.assert_constrained(url, payload, "holds", "why")


if __name__ == "__main__":
    unittest.main()
