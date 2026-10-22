import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # needed under python -I
import apply_rules as ar

RULES = ar.load_rules()


class DatasetTests(unittest.TestCase):
    def test_rule_count_and_fields(self):
        self.assertGreaterEqual(len(RULES), 20)
        for r in RULES:
            for k in ("id", "wrong", "corrected", "pattern", "replacement", "rationale", "counter_examples", "examples"):
                self.assertIn(k, r, r["id"])
            self.assertTrue(r["examples"], r["id"])

    def test_every_wrong_form_is_fixed(self):
        for r in RULES:
            for ex in r["examples"]:
                with self.subTest(rule=r["id"], text=ex["wrong"]):
                    self.assertEqual(ar.apply(ex["wrong"], RULES), ex["fixed"])

    def test_every_counter_example_is_unchanged_by_all_rules(self):
        for r in RULES:
            for neg in r["counter_examples"]:
                with self.subTest(rule=r["id"], text=neg):
                    self.assertEqual(ar.apply(neg, RULES), neg)

    def test_no_private_markers(self):
        with open(os.path.join(ar.HERE, "rules.jsonl"), encoding="utf-8") as f:
            raw = f.read()
        for bad in ("@", "http", "Users", "OneDrive"):
            self.assertNotIn(bad, raw)


class ProtectionTests(unittest.TestCase):
    def check(self, text):
        self.assertEqual(ar.apply(text, RULES), text)

    def test_protected_spans_are_untouched(self):
        self.check("문의: 몇일 걸리나요")                 # key : value line
        self.check("[몇일] 안내")                          # [tag]
        self.check('그는 "몇일 걸린다" 라고 썼다')           # quoted
        self.check("someone-몇일@example.com 으로 회신")   # e-mail containing a wrong-looking form

    def test_straight_single_quotes_are_protected(self):
        self.check("그는 '금새 올께' 라고 썼다")
        self.check("'몇일 걸린다'라고 했다")           # Hangul particle right after the quote
        self.check("(예: '몇일') 참고")

    def test_inner_apostrophe_inside_single_quotes(self):
        self.check("'don't 몇일 do it'")
        self.check("그는 'it's 몇일' 라고 썼다")
        self.assertEqual(ar.apply("'it's 몇일' 그리고 몇일", RULES), "'it's 몇일' 그리고 며칠")

    def test_apostrophe_edge_cases_unchanged(self):
        self.assertEqual(ar.apply("그 사람's 몇일", RULES), "그 사람's 며칠")
        self.assertEqual(ar.apply("'90년대 몇일", RULES), "'90년대 며칠")
        self.assertEqual(ar.apply("5 o'clock 몇일", RULES), "5 o'clock 며칠")
        self.assertEqual(ar.apply("rock 'n' roll 몇일", RULES), "rock 'n' roll 며칠")
        self.assertEqual(ar.apply("'몇일" + chr(10) + "몇일'", RULES), "'며칠" + chr(10) + "며칠'")

    def test_corner_bracket_quotes_are_protected(self):
        self.check("「몇일 걸린다」 라고 썼다")
        self.check("『몇일 걸린다』 라고 썼다")

    def test_apostrophes_do_not_open_a_quote(self):
        self.assertEqual(ar.apply("Don't ask, 몇일 걸리나요", RULES), "Don't ask, 며칠 걸리나요")
        self.assertEqual(ar.apply("it's 몇일 and that's 몇일", RULES),
                         "it's 며칠 and that's 며칠")
        self.assertEqual(ar.apply("the users' 몇일 기록", RULES), "the users' 며칠 기록")

    def test_quote_protection_does_not_leak_outside(self):
        self.assertEqual(ar.apply("몇일 뒤 '몇일' 그리고 몇일", RULES),
                         "며칠 뒤 '몇일' 그리고 며칠")

    def test_outside_is_still_fixed_next_to_protected(self):
        self.assertEqual(ar.apply("SKU-12 갯수 확인", RULES), "SKU-12 개수 확인")
        self.assertEqual(ar.apply("https://example.com/a 를 보고 몇일 뒤 연락", RULES),
                         "https://example.com/a 를 보고 며칠 뒤 연락")

    def test_idempotent(self):
        t = "내일 보내드릴께요. 몇일 걸립니까"
        once = ar.apply(t, RULES)
        self.assertEqual(ar.apply(once, RULES), once)


if __name__ == "__main__":
    unittest.main()
