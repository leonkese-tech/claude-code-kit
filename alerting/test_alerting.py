import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alerting import Alerter  # noqa: E402

H = 3600.0
M = 60.0


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.json")
        self.clock = Clock()
        self.sent = []
        self.sender_ok = True
        self.a = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def sender(self, subject, body):
        self.sent.append((subject, body))
        return self.sender_ok

    def make(self, path=None):
        return Alerter(path or self.path, self.sender, self.clock)


class TestNotify(Base):
    def test_first_message_only_once(self):
        self.assertEqual(self.a.notify("k", "disk full", "b"), "first")
        for _ in range(5):
            self.clock.advance(5 * M)
            self.assertIsNone(self.a.notify("k", "disk full", "b"))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][0].startswith("[ALERT]"))

    def test_escalation_and_backoff(self):
        self.a.notify("k", "t", "b", escalate_after_min=60)
        self.clock.advance(61 * M)
        self.assertEqual(self.a.notify("k", "t", "b", escalate_after_min=60), "urgent")
        self.assertTrue(self.sent[-1][0].startswith("[URGENT]"))
        self.clock.advance(11 * H)
        self.assertIsNone(self.a.notify("k", "t", "b", escalate_after_min=60))
        self.clock.advance(1.1 * H)                      # 12h after: 1st reminder
        self.assertEqual(self.a.notify("k", "t", "b", escalate_after_min=60), "urgent")
        self.clock.advance(13 * H)                       # now gap is 24h
        self.assertIsNone(self.a.notify("k", "t", "b", escalate_after_min=60))
        self.clock.advance(11.5 * H)
        self.assertEqual(self.a.notify("k", "t", "b", escalate_after_min=60), "urgent")

    def test_escalate_zero_is_urgent_immediately(self):
        self.assertEqual(self.a.notify("k", "t", "b", escalate_after_min=0), "urgent")
        self.assertTrue(self.sent[0][0].startswith("[URGENT] t"))

    def test_no_escalation_when_none(self):
        self.a.notify("k", "t", "b")
        self.clock.advance(100 * H)
        self.assertIsNone(self.a.notify("k", "t", "b"))
        self.assertEqual(len(self.sent), 1)

    def test_caller_tags_are_stripped(self):
        self.a.notify("k", "[URGENT] [alert] real title", "b")
        self.assertEqual(self.sent[0][0], "[ALERT] real title")

    def test_failed_send_is_retried_next_time(self):
        self.sender_ok = False
        self.assertIsNone(self.a.notify("k", "t", "b"))
        self.sender_ok = True
        self.assertEqual(self.a.notify("k", "t", "b"), "first")

    def test_sender_exception_does_not_propagate(self):
        a = Alerter(self.path, lambda s, b: 1 / 0, self.clock)
        self.assertIsNone(a.notify("k", "t", "b"))


class TestResolve(Base):
    def test_three_way_result(self):
        self.assertEqual(self.a.resolve("nothing"), "none")
        self.a.notify("k", "t", "b")
        self.assertEqual(self.a.resolve("k", "t"), "closed")
        self.assertEqual(self.a.resolve("k", "t"), "none")

    def test_only_none_is_falsy(self):
        self.assertTrue("none")
        self.assertTrue("closed")
        self.assertFalse(None)

    def test_resolved_message_only_after_urgent(self):
        self.a.notify("k", "t", "b", escalate_after_min=10)
        self.a.resolve("k", "t")
        self.assertEqual(len(self.sent), 1)              # first only, nothing was urgent
        self.a.notify("k2", "t2", "b", escalate_after_min=10)
        self.clock.advance(20 * M)
        self.a.notify("k2", "t2", "b", escalate_after_min=10)
        self.a.resolve("k2", "t2", note="extra")
        self.assertIn("resolved", self.sent[-1][0])
        self.assertIn("extra", self.sent[-1][1])

    def test_reopen_sends_first_again(self):
        self.a.notify("k", "t", "b")
        self.a.resolve("k")
        self.clock.advance(H)
        self.assertEqual(self.a.notify("k", "t", "b"), "first")

    def test_resolve_none_when_state_unreadable(self):
        # a directory where the file should be -> OSError (not FileNotFoundError)
        os.makedirs(self.path)
        a = self.make()
        self.assertIsNone(a.resolve("k"))

    def test_resolve_none_when_cannot_persist(self):
        self.a.notify("k", "t", "b")
        # make every write fail: replace target is a directory-protected path
        import alerting
        real = alerting.os.replace

        def boom(*_a, **_k):
            raise PermissionError("nope")
        alerting.os.replace = boom
        orig_open = open

        def fail_open(file, mode="r", *a, **k):
            if "w" in mode and str(file) == self.path:
                raise PermissionError("nope")
            return orig_open(file, mode, *a, **k)
        try:
            import builtins
            builtins.open = fail_open
            r = self.a.resolve("k", "t")
        finally:
            builtins.open = orig_open
            alerting.os.replace = real
        self.assertIsNone(r)
        self.assertEqual(self.a.open_keys(), ["k"])      # still open on disk


class TestFlapping(Base):
    def test_flapping_batched_to_one_per_day(self):
        sent_counts = []
        for i in range(8):
            self.a.notify("k", "t", "b", escalate_after_min=0)
            self.clock.advance(2 * M)
            self.a.resolve("k", "t")
            self.clock.advance(2 * M)
            sent_counts.append(len(self.sent))
        # first cycles are allowed through, later ones are held
        self.assertLess(len(self.sent), 8)
        n_before = len(self.sent)
        for _ in range(3):
            self.a.notify("k", "t", "b", escalate_after_min=0)
            self.clock.advance(2 * M)
            self.a.resolve("k", "t")
            self.clock.advance(2 * M)
        self.assertEqual(len(self.sent), n_before)
        self.clock.advance(25 * H)
        self.assertEqual(self.a.notify("k", "t", "b", escalate_after_min=0), "urgent")
        self.assertIn("flapping", self.sent[-1][1])

    def test_long_outage_is_not_flapping(self):
        for _ in range(3):
            self.a.notify("k", "t", "b")
            self.clock.advance(2 * M)
            self.a.resolve("k")
        self.clock.advance(H)
        self.a.notify("k", "t", "b", escalate_after_min=45)
        self.clock.advance(46 * M)
        self.assertEqual(self.a.notify("k", "t", "b", escalate_after_min=45), "urgent")


class TestState(Base):
    def test_corrupt_state_not_silent(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        a = self.make()
        self.assertEqual(a.notify("k", "t", "b"), "first")
        subjects = [s for s, _ in self.sent]
        self.assertTrue(any("corrupt" in s for s in subjects))
        quarantined = [n for n in os.listdir(self.tmp.name) if ".corrupt-" in n]
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(a.open_keys(), ["k"])

    def test_non_object_top_level_is_corrupt(self):
        with open(self.path, "w") as f:
            f.write("[1,2]")
        self.make().notify("k", "t", "b")
        self.assertTrue(any("corrupt" in s for s, _ in self.sent))

    def _corrupt(self, text="{not json"):
        with open(self.path, "w") as f:
            f.write(text)

    def test_resolve_on_corrupt_state_returns_none_and_sends_no_resolved(self):
        self._corrupt()
        a = self.make()
        self.assertIsNone(a.resolve("k", title="t"))
        self.assertFalse(any("resolved" in s for s, _ in self.sent))
        self.assertTrue(any("corrupt" in s for s, _ in self.sent))   # but the human is told

    def test_corrupt_notice_failure_is_surfaced_and_retried(self):
        self._corrupt()
        logs = []
        a = Alerter(self.path, self.sender, self.clock, logger=logs.append)
        self.sender_ok = False
        a.notify("k", "t", "b")
        self.assertIn("not delivered", a.last_failure)
        self.assertTrue(any("not delivered" in m for m in logs))
        self.sent.clear()
        self.sender_ok = True
        self.clock.advance(5 * M)
        a.notify("k", "t", "b")
        corrupt_msgs = [s for s, _ in self.sent if "corrupt" in s]
        self.assertEqual(len(corrupt_msgs), 1)               # retried, delivered
        self.sent.clear()
        a.notify("k", "t", "b")
        a.resolve("zzz")
        self.assertFalse(any("corrupt" in s for s, _ in self.sent))   # and not repeated
        self.assertFalse(os.path.exists(self.path + ".notice"))

    def test_corrupt_notice_retried_by_a_new_process(self):
        self._corrupt()
        self.sender_ok = False
        self.make().notify("k", "t", "b")
        self.sent.clear()
        self.sender_ok = True
        self.make().resolve("nothing")                       # fresh instance, same state path
        self.assertTrue(any("corrupt" in s for s, _ in self.sent))

    def test_failed_quarantine_does_not_repeat_notice_every_call(self):
        self._corrupt()
        a = self.make()
        a._quarantine = lambda: ""                           # e.g. file locked on Windows
        for _ in range(50):
            self.assertIsNone(a.resolve("k", title="t"))
            self.clock.advance(1 * M)
        self.assertEqual(len([s for s, _ in self.sent if "corrupt" in s]), 1)
        b = self.make()                                      # new process, same state path
        b._quarantine = lambda: ""
        self.assertIsNone(b.resolve("k"))
        self.assertEqual(len([s for s, _ in self.sent if "corrupt" in s]), 1)
        self.clock.advance(25 * H)                           # still corrupt a day later
        self.assertIsNone(a.resolve("k"))
        self.assertEqual(len([s for s, _ in self.sent if "corrupt" in s]), 2)

    def test_failed_quarantine_with_undelivered_notice_does_not_double_send(self):
        self._corrupt()
        a = self.make()
        a._quarantine = lambda: ""
        self.sender_ok = False
        a.resolve("k")
        self.sender_ok = True
        a.resolve("k")                                       # retry delivers once
        a.resolve("k")
        # attempts: 1 failed + 1 delivered by the retry; the corrupt branch stays throttled
        self.assertEqual(len([s for s, _ in self.sent if "corrupt" in s]), 2)

    def test_two_corruptions_in_one_second_keep_both_quarantine_files(self):
        for i in range(2):
            self._corrupt("{bad %d" % i)
            self.make().notify("k%d" % i, "t", "b")   # same fake clock value both times
        names = [n for n in os.listdir(self.tmp.name) if ".corrupt-" in n]
        self.assertEqual(len(names), 2)

    def test_unreadable_state_not_overwritten(self):
        self.a.notify("keep", "t", "b")
        a = self.make()
        orig = a._read
        a._read = lambda: (None, "unreadable", "locked")
        a.notify("other", "t", "b")
        a._read = orig
        self.assertEqual(a.open_keys(), ["keep"])        # 'keep' survived, 'other' not written

    def test_open_keys_reports_status(self):
        meta = {}
        self.assertEqual(self.a.open_keys(meta=meta), [])
        self.assertEqual(meta["primary"], "none")
        self.a.notify("db:x", "t", "b")
        self.a.notify("net:y", "t", "b")
        self.assertEqual(self.a.open_keys("db:"), ["db:x"])

    def test_stale_keys_expire(self):
        self.a.notify("old", "t", "b")
        self.clock.advance(8 * 86400)
        self.a.notify("new", "t", "b")                   # triggers a save + sweep
        self.assertEqual(self.a.open_keys(), ["new"])

    def test_live_key_not_expired(self):
        self.a.notify("k", "t", "b")
        for _ in range(10):
            self.clock.advance(86400)
            self.a.notify("k", "t", "b")                 # keeps last_seen_at fresh
        self.assertEqual(self.a.open_keys(), ["k"])

    def test_long_escalation_keeps_key_alive(self):
        self.a.notify("weekly", "t", "b", escalate_after_min=10 * 1440)
        self.clock.advance(8 * 86400)
        self.a.notify("other", "t", "b")
        self.assertIn("weekly", self.a.open_keys())

    def test_unsavable_state_still_alerts_but_throttled(self):
        bad = os.path.join(self.tmp.name, "no", "such")
        os.makedirs(os.path.join(self.tmp.name, "no"))
        open(os.path.join(self.tmp.name, "no", "such"), "w").close()   # file where dir needed
        a = self.make(os.path.join(bad, "state.json"))
        self.assertEqual(a.notify("k", "t", "b"), "first")
        self.assertTrue(self.sent[0][0].startswith("[URGENT]"))
        for _ in range(20):                              # frequent poller
            self.clock.advance(3 * M)
            a.notify("k", "t", "b")
        self.assertEqual(len(self.sent), 1)
        self.clock.advance(13 * H)
        self.assertEqual(a.notify("k", "t", "b"), "first")
        self.assertEqual(len(self.sent), 2)

    def test_state_file_is_valid_json_after_use(self):
        import json
        self.a.notify("k", "t", "b")
        with open(self.path, encoding="utf-8") as f:
            self.assertIn("k", json.load(f))
        self.assertEqual([n for n in os.listdir(self.tmp.name) if ".tmp-" in n], [])


if __name__ == "__main__":
    unittest.main()
