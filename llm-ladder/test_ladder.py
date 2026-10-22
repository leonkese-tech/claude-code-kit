import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ladder  # noqa: E402
from ladder import (Provider, InputTooLongError, complete, complete_ex, last_result,  # noqa: E402
                    looks_like_api_error, report_coverage, mostly_korean, parse_json_list)


def ok(text):
    return lambda p: text


def boom(p):
    raise RuntimeError("down")


class TestLadder(unittest.TestCase):
    def test_first_good_wins_and_later_not_called(self):
        calls = []
        def second(p):
            calls.append(p)
            return "B"
        r = complete_ex("q", [("cli", ok("A")), ("api", second)])
        self.assertEqual((r.text, r.rung), ("A", "cli"))
        self.assertEqual(calls, [])

    def test_falls_through_and_records_reasons(self):
        r = complete_ex("q", [("cli", boom), ("api", ok("  ")), ("local", ok("done"))])
        self.assertEqual(r.rung, "local")
        self.assertEqual([n for n, _ in r.failures], ["cli", "api"])
        self.assertIn("RuntimeError", r.failures[0][1])
        self.assertEqual(r.failures[1][1], "empty")
        self.assertIs(last_result(), r)

    def test_all_fail_returns_none_never_raises(self):
        self.assertIsNone(complete("q", [boom, ok(None), ok(123)]))
        self.assertIsNone(complete("q", []))

    def test_api_error_string_is_failure(self):
        for bad in ("API Error: 400", "  API Error (503) overloaded ", "Error: 429 too many requests"):
            self.assertTrue(looks_like_api_error(bad), bad)
        r = complete_ex("q", [("cli", ok("API Error: 400")), ("api", ok("real"))])
        self.assertEqual(r.text, "real")
        self.assertTrue(r.failures[0][1].startswith("api_error"))

    def test_api_error_classification_table(self):
        errors = [
            "API Error: 400", "API Error (503) overloaded", "Error: 429 too many requests",
            '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
            '{"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}',
            '{"error": {"type": "invalid_request_error", "message": "bad key"}}',
            "Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error', "
            "'message': 'Number of request tokens has exceeded your per-minute rate limit'}, "
            "'request_id': 'req_01'}",
            "Error code: 529", "Error code: 429 - {\"message\": \"slow down\"}",
            "anthropic.InternalServerError: 500 Internal server error",
            "anthropic.RateLimitError: 429 - rate limited",
            "openai.APIStatusError: 503", "Error: Overloaded", "Service Unavailable",
            "Error: 503: service unavailable", "Error: 500", "API Error: 500 Internal Server Error",
            "Credit balance is too low", "Your credit balance is too low to access the API. "
            "Please go to Plans & Billing.", "usage limit reached", "Claude AI usage limit reached|1760000000",
            "You've reached your usage limit", "Quota exceeded",
            "{'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}",
            '{\n  "type": "error",\n  "error": {"type": "api_error"}\n}',
            "HTTP 503 Service Unavailable", "HTTP/1.1 502 Bad Gateway",
            "429 Too Many Requests", "503 Service Unavailable",
            "RateLimitError: 429 slow down", "Internal Server Error: 500",
            "Overloaded", "Too Many Requests", "Rate limit exceeded", "overloaded_error",
        ]
        answers = [
            "Error handling means catching exceptions and recovering.",
            "Error codes are listed in the table below.",
            "Request failed, so I retried it and it worked.",
            "Rate limited", "Error", "Error: something odd happened",
            "The error 404 page is shown when a route is missing.",
            "HTTP is a protocol.", "404 pages are common.", "500 requests per second",
            '{"result": "ok"}', '{"error": "none"}', "{not json}",
            "Overloaded " + "word " * 80,
            "API Error: 400\nbut the rest of this answer is long and useful",
            "",
            # lint / data / prose that merely contains numbers or the word error
            '{"error":"E101","code":101}', '{"error": "bad", "message": "x"}',
            '{"error": {"message": "bad key"}}', '{"error": {"type": "x"}}',
            '{"type": "warning", "error": {"type": "x"}}',
            "{'error': 'E101', 'code': 101}", "{'type': 'ok'}",
            "Error: 500 rows processed", "Error: 429: 3 people attended",
            "Error code: 404 pages were indexed", "Error: 4000 items",
            "ValueError: invalid literal", "Error: 503 servers were rebooted",
            "Credit balance is a term used in accounting; here is how it works.",
            "Usage limits are covered in section 3.", "Service unavailable pages should be friendly.",
            "Overloaded " + "word " * 80, "Error code: 429 - " + "word " * 200,
        ]
        for text in errors:
            self.assertTrue(looks_like_api_error(text), text)
        for text in answers:
            self.assertFalse(looks_like_api_error(text), text)

    def test_real_answer_not_rejected_by_ladder(self):
        txt = "Error handling means catching exceptions and recovering."
        self.assertEqual(complete("q", [ok(txt)]), txt)
        r = complete_ex("q", [("a", ok("HTTP 503 Service Unavailable")), ("b", ok("fine"))])
        self.assertEqual((r.text, r.rung), ("fine", "b"))

    def test_long_answer_mentioning_error_is_fine(self):
        txt = "Here is how to handle API Error: 400 in your client.\nUse retries."
        self.assertFalse(looks_like_api_error(txt))
        self.assertEqual(complete("q", [ok(txt)]), txt)

    def test_allow_filters_by_name_and_tag(self):
        provs = [Provider("cli", ok("A"), tags=("cloud",)),
                 Provider("local", ok("L"), tags=("local",))]
        r = complete_ex("q", provs, allow={"local"})
        self.assertEqual((r.rung, r.skipped), ("local", ["cli"]))
        r = complete_ex("q", provs, allow=["cloud"])
        self.assertEqual(r.rung, "cli")
        self.assertIsNone(complete("q", provs, allow=set()))
        self.assertEqual(complete("q", provs, allow=lambda p: p.name == "local"), "L")

    def test_validate_hook(self):
        r = complete_ex("q", [("a", ok("这是中文回答，不是韩语")), ("b", ok("한국어로 된 답변입니다"))],
                        validate=mostly_korean)
        self.assertEqual(r.rung, "b")
        self.assertEqual(r.failures, [("a", "validation_failed")])
        r = complete_ex("q", [("a", ok("x"))], validate=lambda t: 1 / 0)
        self.assertIsNone(r.text)

    def test_mostly_korean(self):
        self.assertTrue(mostly_korean("안녕하세요 반갑습니다 hello"))
        self.assertFalse(mostly_korean("12345"))
        self.assertFalse(mostly_korean("English only answer"))
        self.assertFalse(mostly_korean(""))

    def test_no_silent_truncation(self):
        seen = []
        with self.assertRaises(InputTooLongError):
            complete("x" * 101, [lambda p: seen.append(p) or "a"], max_input_chars=100)
        self.assertEqual(seen, [])
        self.assertEqual(complete("x" * 100, [ok("a")], max_input_chars=100), "a")

    def test_provider_receives_full_prompt_unchanged(self):
        got = []
        complete("p" * 5000, [lambda p: got.append(p) or "a"])
        self.assertEqual(len(got[0]), 5000)

    def test_bad_provider_type(self):
        with self.assertRaises(TypeError):
            complete("q", [42])


class TestCoverage(unittest.TestCase):
    def test_logs_n_of_m_always(self):
        lines = []
        self.assertFalse(report_coverage("triage", 10, 9, logger=lines.append))
        self.assertEqual(lines, ["[triage] 9 of 10 answered"])

    def test_bad_when_zero_or_low(self):
        calls = []
        hook = lambda *a: calls.append(a)
        self.assertTrue(report_coverage("t", 37, 0, logger=lambda m: None, on_bad=hook))
        self.assertTrue(report_coverage("t", 10, 4, logger=lambda m: None, on_bad=hook))
        self.assertFalse(report_coverage("t", 10, 5, logger=lambda m: None, on_bad=hook))
        self.assertFalse(report_coverage("t", 0, 0, logger=lambda m: None, on_bad=hook))
        self.assertEqual(calls, [("t", 37, 0), ("t", 10, 4)])

    def test_hook_error_swallowed(self):
        self.assertTrue(report_coverage("t", 5, 0, logger=lambda m: None, on_bad=lambda *a: 1 / 0))


class TestParseJsonList(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(parse_json_list('[{"a":1},{"a":2}]'), [{"a": 1}, {"a": 2}])
        self.assertEqual(parse_json_list('{"items":[{"a":1}]}'), [{"a": 1}])
        self.assertEqual(parse_json_list('{"a":1}'), [{"a": 1}])
        self.assertEqual(parse_json_list('```json\n[{"a":1}]\n```'), [{"a": 1}])
        self.assertEqual(parse_json_list('[{"a":1},{"a":2},{"a"'), [{"a": 1}, {"a": 2}])
        self.assertIsNone(parse_json_list("no json"))
        self.assertIsNone(parse_json_list(""))


if __name__ == "__main__":
    unittest.main()
