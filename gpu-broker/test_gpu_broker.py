# -*- coding: utf-8 -*-
"""Tests for gpu_broker using fakes only. Run: python -I -m unittest"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gpu_broker as gb  # noqa: E402


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeGpu:
    """Free VRAM that grows when 'unload' is called."""
    def __init__(self, free):
        self.free = free
        self.calls = []

    def __call__(self):
        return self.free

    def unloader(self, name, gain):
        def cb():
            self.calls.append(name)
            self.free += gain
        cb.__name__ = name
        return cb


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = FakeClock()
        self.msgs = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def broker(self, gpu, **kw):
        kw.setdefault("headroom_mb", 500)
        kw.setdefault("settle_sec", 0)
        kw.setdefault("lock_wait_sec", 10)
        return gb.Broker(free_vram_mb=gpu, lock_dir=self.dir, clock=self.clock,
                         sleep=self.clock.sleep, log=self.msgs.append, **kw)


class TestBudget(Base):
    def test_enough_vram_no_callbacks(self):
        gpu = FakeGpu(10000)
        un = gpu.unloader("unload", 5000)
        restored = []
        with self.broker(gpu).budget(4000, "a", make_room=[un],
                                     restore=[lambda: restored.append(1)]) as ok:
            self.assertTrue(ok)
        self.assertEqual(gpu.calls, [])
        self.assertEqual(restored, [1])          # restore still runs

    def test_make_room_in_order_stops_when_enough(self):
        gpu = FakeGpu(2000)
        a, b = gpu.unloader("first", 3000), gpu.unloader("second", 9000)
        with self.broker(gpu).budget(4000, "a", make_room=[a, b]) as ok:
            self.assertTrue(ok)
        self.assertEqual(gpu.calls, ["first"])   # 2000+3000 >= 4500 -> second not needed

    def test_not_enough_even_after_all(self):
        gpu = FakeGpu(1000)
        a = gpu.unloader("first", 500)
        with self.broker(gpu).budget(8000, "a", make_room=[a]) as ok:
            self.assertFalse(ok)                 # body still runs, caller decides

    def test_failing_make_room_is_reported_not_fatal(self):
        gpu = FakeGpu(1000)

        def bad():
            raise RuntimeError("boom")
        good = gpu.unloader("good", 9000)
        b = self.broker(gpu)
        with b.budget(4000, "a", make_room=[bad, good]) as ok:
            self.assertTrue(ok)
        self.assertEqual(len(b.last_errors), 1)

    def test_restore_on_exception(self):
        gpu = FakeGpu(1000)
        order = []
        b = self.broker(gpu)
        with self.assertRaises(ValueError):
            with b.budget(4000, "a", make_room=[gpu.unloader("u", 9000)],
                          restore=[lambda: order.append("r1"), lambda: order.append("r2")]):
                raise ValueError("body failed")
        self.assertEqual(order, ["r1", "r2"])
        self.assertFalse(b._held_elsewhere())            # turn released too

    def test_restore_runs_when_make_room_machinery_raises(self):
        order = []
        calls = [0]

        def probe():
            calls[0] += 1
            if calls[0] > 1:
                raise KeyboardInterrupt()               # not an Exception: escapes _free
            return 100
        b = self.broker(probe, settle_sec=0)
        with self.assertRaises(KeyboardInterrupt):
            with b.budget(4000, "a", make_room=[lambda: None],
                          restore=[lambda: order.append("r")]):
                self.fail("body must not run")
        self.assertEqual(order, ["r"])
        self.assertFalse(b._held_elsewhere())

    def test_one_failing_restore_does_not_skip_others(self):
        gpu = FakeGpu(10000)
        order = []

        def bad():
            raise RuntimeError("x")
        b = self.broker(gpu)
        with b.budget(100, "a", restore=[bad, lambda: order.append("ok")]):
            pass
        self.assertEqual(order, ["ok"])
        self.assertEqual(len(b.last_errors), 1)

    def test_unknown_free_vram_does_not_block(self):
        b = self.broker(lambda: None)
        with b.budget(100, "a", make_room=[lambda: None]) as ok:
            self.assertFalse(ok)                 # cannot verify, but body runs

    def test_probe_exception_treated_as_unknown(self):
        def probe():
            raise OSError("no nvidia-smi")
        with self.broker(probe).budget(100, "a") as ok:
            self.assertFalse(ok)


class TestLock(Base):
    def test_same_process_brokers_exclude_each_other(self):
        b1, b2 = self.broker(FakeGpu(9999)), self.broker(FakeGpu(9999))
        with b1.budget(1, "first"):
            with self.assertRaises(gb.LockTimeout):
                with b2.budget(1, "second", lock_wait_sec=2):
                    self.fail("body must not run")
        self.assertGreaterEqual(self.clock() - 1000.0, 2)       # it did wait
        with b2.budget(1, "second") as ok:                      # free again afterwards
            self.assertTrue(ok)

    def test_threads_exclude_each_other(self):
        inside, overlap = [0], []

        def run():
            b = gb.Broker(free_vram_mb=lambda: 9999, lock_dir=self.dir, settle_sec=0,
                          lock_wait_sec=20, poll_sec=0.005, log=lambda m: None)
            for _ in range(5):
                with b.budget(1, "t"):
                    inside[0] += 1
                    if inside[0] > 1:
                        overlap.append(1)
                    time.sleep(0.002)
                    inside[0] -= 1
        ts = [threading.Thread(target=run) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(overlap, [])

    def test_lock_released_after_normal_exit_and_file_is_permanent(self):
        b = self.broker(FakeGpu(9999))
        with b.budget(1, "me"):
            self.assertTrue(b._held_elsewhere())
        self.assertFalse(b._held_elsewhere())
        self.assertEqual(os.listdir(self.dir), [gb.LOCK_NAME])

    def test_release_after_body_error_frees_turn(self):
        b = self.broker(FakeGpu(9999))
        with self.assertRaises(KeyError):
            with b.budget(1, "me"):
                raise KeyError("x")
        with b.budget(1, "again") as ok:
            self.assertTrue(ok)

    def test_leftover_info_in_unlocked_file_is_not_a_holder(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, gb.LOCK_NAME), "wb") as f:
            f.write(b"\0" * gb.INFO_OFFSET + json.dumps(
                {"name": "dead", "pid": 1, "since": 1}).encode())
        b = self.broker(FakeGpu(9999))
        self.assertIsNone(b.status()["holder"])
        with b.budget(1, "me") as ok:                    # no stale logic needed
            self.assertTrue(ok)

    def test_garbage_lock_file_content_is_harmless(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, gb.LOCK_NAME), "wb") as f:
            f.write(b"{not json \xff\xfe" * 10)
        with self.broker(FakeGpu(9999)).budget(1, "me") as ok:
            self.assertTrue(ok)

    def test_max_hold_sec_only_warns(self):
        h, w = self.broker(FakeGpu(9999)), self.broker(FakeGpu(9999), max_hold_sec=5)
        with h.budget(1, "slow"):
            self.clock.t += 100
            with self.assertRaises(gb.LockTimeout):
                with w.budget(1, "waiter", lock_wait_sec=2):
                    pass
        self.assertTrue(any("warning" in m and "slow" in m for m in self.msgs))

    def test_unusable_os_lock_falls_back_to_process_lock(self):
        b = self.broker(FakeGpu(9999))
        orig = gb._os_try_lock

        def boom(fd):
            raise OSError(45, "locks not supported")
        gb._os_try_lock = boom
        try:
            with b.budget(1, "me") as ok:
                self.assertTrue(ok)
            self.assertEqual(len(b.last_errors), 1)
        finally:
            gb._os_try_lock = orig


class TestStatus(Base):
    def test_status_idle(self):
        b = self.broker(FakeGpu(7000))
        s = b.status()
        self.assertEqual(s["free_mb"], 7000)
        self.assertIsNone(s["holder"])
        self.assertFalse(os.path.exists(b.lock_path))     # status creates nothing

    def test_status_shows_holder(self):
        b = self.broker(FakeGpu(7000))
        with b.budget(1, "me"):
            h = b.status()["holder"]
            self.assertEqual((h["name"], h["pid"]), ("me", os.getpid()))

    def test_log_failure_never_kills_job(self):
        def bad_log(_):
            raise UnicodeEncodeError("x", "", 0, 1, "y")
        b = gb.Broker(free_vram_mb=FakeGpu(1000), lock_dir=self.dir, clock=self.clock,
                      sleep=self.clock.sleep, log=bad_log, settle_sec=0)
        with b.budget(4000, "a", make_room=[lambda: None]) as ok:
            self.assertFalse(ok)


# ---- multi-process tests (real processes, fake probes, no GPU) ---------------------

def _worker_contend(lock_dir, marker, start_at, rounds):
    broker = gb.Broker(free_vram_mb=lambda: 99999, lock_dir=lock_dir, settle_sec=0,
                       lock_wait_sec=20, poll_sec=0.005, log=lambda m: None)
    while time.time() < start_at:
        time.sleep(0.0005)
    for _ in range(rounds):
        with broker.budget(1, "w%d" % os.getpid()):
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                print("OVERLAP")
                return 3
            os.close(fd)
            time.sleep(0.004)
            os.remove(marker)
    return 0


def _worker_hold(lock_dir):
    broker = gb.Broker(free_vram_mb=lambda: 99999, lock_dir=lock_dir, settle_sec=0,
                       log=lambda m: None)
    with broker.budget(1, "holder"):
        print("held", flush=True)
        time.sleep(120)
    return 0


def _spawn(*args):
    return subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "--worker"]
                            + [str(a) for a in args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class TestMultiProcess(Base):
    def test_simultaneous_start_is_mutually_exclusive(self):
        trials, workers = 16, 8
        for trial in range(trials):
            lock_dir = os.path.join(self.dir, "t%d" % trial)
            marker = os.path.join(self.dir, "in_use%d" % trial)
            start_at = time.time() + 0.6
            procs = [_spawn("contend", lock_dir, marker, start_at, 1) for _ in range(workers)]
            for p in procs:
                out, err = p.communicate(timeout=60)
                self.assertEqual(p.returncode, 0, "trial %d: out=%r err=%r" % (trial, out, err))
                self.assertNotIn("OVERLAP", out)
            self.assertFalse(os.path.exists(marker))
            self.assertEqual(os.listdir(lock_dir), [gb.LOCK_NAME])

    def _wait_held(self, p):
        line = p.stdout.readline()
        self.assertEqual(line.strip(), "held", "worker said %r" % line)

    def _reap(self, p):
        p.kill()
        p.wait(timeout=10)
        p.stdout.close()
        p.stderr.close()

    def _broker(self, lock_dir, **kw):
        return gb.Broker(free_vram_mb=lambda: 99999, lock_dir=lock_dir, settle_sec=0,
                         lock_wait_sec=10, poll_sec=0.02, log=self.msgs.append, **kw)

    def test_live_holder_in_other_process_blocks_then_kill_frees_promptly(self):
        lock_dir = os.path.join(self.dir, "k1")
        p = _spawn("hold", lock_dir)
        try:
            self._wait_held(p)
            b = self._broker(lock_dir)
            self.assertEqual(b.status()["holder"]["name"], "holder")
            with self.assertRaises(gb.LockTimeout):
                with b.budget(1, "blocked", lock_wait_sec=0.3):
                    self.fail("must not run while a live process holds the turn")
            p.kill()                                    # hard kill while holding
            p.wait(timeout=10)
            t0 = time.time()
            with b.budget(1, "taker") as ok:
                self.assertTrue(ok)
            self.assertLess(time.time() - t0, 3.0)      # prompt, no stale timeout
            self.assertIsNone(b.status()["holder"])
        finally:
            self._reap(p)

    def test_terminate_while_holding_also_frees(self):
        lock_dir = os.path.join(self.dir, "k2")
        p = _spawn("hold", lock_dir)
        try:
            self._wait_held(p)
            p.terminate()
            p.wait(timeout=10)
            with self._broker(lock_dir).budget(1, "taker") as ok:
                self.assertTrue(ok)
        finally:
            self._reap(p)

    def test_lock_file_deleted_while_held(self):
        """Documented limitation. Windows refuses to delete a file that is open, so the
        lock cannot be pulled away. POSIX allows unlink: the holder simply continues
        (nothing crashes), but a newcomer creates a fresh file and is NOT excluded."""
        b = self._broker(os.path.join(self.dir, "d"))
        with b.budget(1, "me") as ok:
            self.assertTrue(ok)
            try:
                os.remove(b.lock_path)
                removed = True
            except OSError:
                removed = False
            if os.name == "nt":
                self.assertFalse(removed)
            else:
                self.assertTrue(removed)
        # holder left cleanly either way and the broker is reusable
        with b.budget(1, "again") as ok:
            self.assertTrue(ok)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        mode = sys.argv[2]
        if mode == "contend":
            sys.exit(_worker_contend(sys.argv[3], sys.argv[4], float(sys.argv[5]),
                                     int(sys.argv[6])))
        sys.exit(_worker_hold(sys.argv[3]))
    unittest.main()
