# -*- coding: utf-8 -*-
"""Tests for code_map. Run: python -I -m unittest"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_map as cm  # noqa: E402

SAMPLE = '''"""Sample module for tests.

More text.
"""
MAX_RETRIES = 3
lower = 1


def fetch(url, *args, timeout=5, **kw):
    """Fetch a URL with retry."""
    for _ in range(MAX_RETRIES):
        pass
    return url


class Client:
    """A client."""

    def send(self, data):
        return fetch(data)

    async def close(self):
        pass
'''


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "sample.py")
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(SAMPLE)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_main(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cm.main(list(args))
        return rc, buf.getvalue()

    def test_skeleton_contents(self):
        text, err = cm.skeleton(self.p)
        self.assertIsNone(err)
        self.assertIn("Sample module for tests.", text)
        self.assertIn("def fetch(url, *args, timeout, **kw)", text)
        self.assertIn("class Client", text)
        self.assertIn("def send(self, data)", text)
        self.assertIn("async def close(self)", text)
        self.assertIn("MAX_RETRIES", text)
        self.assertNotIn("lower", text)           # only UPPER_CASE constants
        self.assertNotIn("for _ in range", text)  # bodies are not included

    def test_line_ranges_are_correct(self):
        text, _ = cm.skeleton(self.p)
        lines = SAMPLE.splitlines()
        start = next(i for i, l in enumerate(lines, 1) if l.startswith("def fetch"))
        self.assertIn("[%d-%d]" % (start, start + 4), text)

    def test_cli_file_prints_token_estimate(self):
        rc, out = self.run_main("--file", self.p)
        self.assertEqual(rc, 0)
        self.assertIn("tokens: full file", out)

    def test_skeleton_much_smaller_on_big_file(self):
        big = os.path.join(self.d, "big.py")
        body = "".join("def f%d(x):\n    '''doc %d'''\n%s    return x\n\n" % (i, i, "    y = x  # filler line\n" * 120)
                       for i in range(60))
        with open(big, "w") as f:
            f.write(body)
        text, _ = cm.skeleton(big)
        self.assertLess(cm.est_tokens(text) / cm.est_tokens(body), 0.05)

    def test_grep_finds_enclosing_def(self):
        hits = list(cm.grep("MAX_RETRIES", [self.d]))
        names = {h[2]["name"] if h[2] else None for h in hits}
        self.assertIn("fetch", names)
        self.assertIn(None, names)                # the module-level constant line

    def test_grep_prefers_innermost(self):
        hits = list(cm.grep("fetch(data)", [self.d]))
        self.assertEqual(hits[0][2]["name"], "send")

    def test_cli_grep_and_no_match(self):
        rc, out = self.run_main("--grep", "retry", self.d)
        self.assertEqual(rc, 0)
        self.assertIn("fetch", out)
        rc, out = self.run_main("--grep", "zzz_nothing", self.d)
        self.assertEqual(rc, 1)

    def test_syntax_error_is_reported_not_fatal(self):
        bad = os.path.join(self.d, "bad.py")
        with open(bad, "w") as f:
            f.write("def (:\n")
        rc, out = self.run_main("--file", bad)
        self.assertEqual(rc, 2)
        self.assertEqual(list(cm.grep("def", [bad]))[0][1], 1)   # grep survives

    def test_lines_range(self):
        rc, out = self.run_main("--file", self.p, "--lines", "5-6")
        self.assertEqual(rc, 0)
        self.assertIn("MAX_RETRIES = 3", out)

    def test_utf8_bom_and_non_ascii(self):
        p = os.path.join(self.d, "u.py")
        with open(p, "wb") as f:
            f.write(b"\xef\xbb\xbf" + '"""café module"""\ndef g():\n    pass\n'.encode("utf-8"))
        text, err = cm.skeleton(p)
        self.assertIsNone(err)
        self.assertIn("café", text)


if __name__ == "__main__":
    unittest.main()
