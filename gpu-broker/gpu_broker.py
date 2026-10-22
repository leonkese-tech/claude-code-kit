# -*- coding: utf-8 -*-
"""gpu_broker - let several heavy jobs share one consumer GPU without stepping on each other.

Typical sharing partners: a local LLM server, an image generator, a video model.
Each job asks the broker for a VRAM budget; the broker frees room (by calling
callbacks YOU supply), runs your block, and always puts things back afterwards.

    broker = Broker()
    with broker.budget(12000, "cover image",
                       make_room=[unload_llm],      # called in order until enough is free
                       restore=[reload_llm]) as ok: # always called when the block ends
        if ok:
            generate()

Rules this module enforces (each one came from a real outage):

  1. Restore ALWAYS runs (try/finally), also when the body raises. A GPU left
     in a swapped state after a crash is the failure this tool exists to prevent.
  2. Cheapest-to-undo callbacks go first in `make_room`; the order you pass is
     the order used. After every callback the free VRAM is measured again, and we
     stop as soon as there is enough.
  3. Keep headroom. Fitting "exactly" fails on fragmentation and transient peaks.
  4. Measure after things settle. Right after an unload the driver reports stale
     numbers, so `settle_sec` is waited before re-measuring.
  5. Only one job at a time swaps. The turn is an OS-level advisory file lock
     (msvcrt.locking on Windows, fcntl.flock on POSIX) on one permanent lock file,
     polled non-blockingly up to `lock_wait_sec`. The OS drops the lock when the
     holder process dies, so there is no stale-lock detection and no takeover
     step: that whole class of race cannot exist. A process-local lock (keyed by
     lock-file path) makes several Broker objects in one process exclude each other.
  6. The broker never kills or reconfigures other jobs by itself; it only calls
     the callbacks you give it. Do not poke the other jobs ad hoc around it.
  7. Logging and status failures must never kill the job. Unknown state means
     "do not block".
  8. A restore callback that fails must not prevent the remaining restores from
     running, and the failure is reported, not swallowed silently.

Limits: advisory (every job must use the broker); local filesystems only (OS
locks are unreliable on network shares); there is NO stale timeout, so a body
that never returns keeps the turn forever (optional `max_hold_sec` only logs a
warning in waiters).

Stdlib only. Everything time/GPU related is injectable, so it is testable
without a GPU. CLI: `python gpu_broker.py --status`.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if os.name == "nt":
    import msvcrt
else:
    import fcntl

DEFAULT_HEADROOM_MB = 1024
DEFAULT_LOCK_WAIT_SEC = 1800.0
DEFAULT_SETTLE_SEC = 3.0
LOCK_NAME = "gpu_broker.lock"
INFO_OFFSET = 16                 # owner info lives after the (Windows) locked byte at offset 0

_LOCAL_LOCKS = {}                # lock path -> threading.Lock (process-local exclusion)
_LOCAL_GUARD = threading.Lock()


def _local_lock(path):
    key = os.path.normcase(os.path.abspath(path))
    with _LOCAL_GUARD:
        lk = _LOCAL_LOCKS.get(key)
        if lk is None:
            lk = _LOCAL_LOCKS[key] = threading.Lock()
        return lk


def nvidia_smi_free_mb():
    """Default free-VRAM probe (MiB of GPU 0). Returns None if it cannot be read."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return None
        return float(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


def _default_log(msg):
    try:
        print(msg)
    except Exception:
        pass


class LockTimeout(Exception):
    pass


def _os_try_lock(fd):
    """Non-blocking exclusive lock. True = got it, False = held by someone else.
    Any other OSError propagates (lock mechanism unusable)."""
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as e:
            if e.errno in (13, 36):          # EACCES / EDEADLK = region already locked
                return False
            raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in (11, 13, 35):          # EAGAIN / EACCES / EWOULDBLOCK
            return False
        raise


def _os_unlock(fd):
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass                                  # closing the handle releases it anyway


class Broker:
    def __init__(self, free_vram_mb=None, lock_dir=None, clock=None, sleep=None,
                 headroom_mb=DEFAULT_HEADROOM_MB, lock_wait_sec=DEFAULT_LOCK_WAIT_SEC,
                 settle_sec=DEFAULT_SETTLE_SEC, log=None, poll_sec=None, max_hold_sec=None):
        self.free_vram_mb = free_vram_mb or nvidia_smi_free_mb
        self.lock_dir = lock_dir or os.path.join(tempfile.gettempdir(), "gpu_broker")
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.headroom_mb = headroom_mb
        self.lock_wait_sec = lock_wait_sec
        self.settle_sec = settle_sec
        self.log = log or _default_log
        self.poll_sec = poll_sec          # waiter polling interval; default derives from wait
        self.max_hold_sec = max_hold_sec  # only a log warning in waiters, never a takeover
        self.lock_path = os.path.join(self.lock_dir, LOCK_NAME)
        self.last_errors = []
        self._fd = None                   # set while THIS instance holds the OS lock
        self._local = None                # process-local lock held by this instance

    # ---- logging that can never raise -------------------------------------
    def _say(self, msg):
        try:
            self.log(msg)
        except Exception:
            pass

    def _free(self):
        try:
            v = self.free_vram_mb()
            return None if v is None else float(v)
        except Exception:
            return None

    # ---- lock --------------------------------------------------------------
    def _read_info(self):
        """Best-effort owner info written by the holder, or None. Informational only."""
        try:
            with open(self.lock_path, "rb") as f:
                f.seek(INFO_OFFSET)
                d = json.loads(f.read().decode("utf-8"))
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    def _write_info(self, fd, name):
        try:
            data = json.dumps({"name": name, "pid": os.getpid(),
                               "since": self.clock()}).encode("utf-8")
            os.ftruncate(fd, INFO_OFFSET)
            os.lseek(fd, INFO_OFFSET, os.SEEK_SET)
            os.write(fd, data)
        except Exception:
            pass

    def _try_acquire(self, name):
        """One non-blocking attempt. True = this instance holds the turn now."""
        local = _local_lock(self.lock_path)
        if not local.acquire(False):
            return False
        fd = None
        try:
            os.makedirs(self.lock_dir, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            got = _os_try_lock(fd)
        except OSError as e:
            # The OS lock mechanism itself is unusable here (e.g. a filesystem without
            # lock support). Do not block the job: fall back to process-local exclusion.
            self._say("[gpu] OS file lock unavailable (%s: %s); using in-process lock only"
                      % (type(e).__name__, e))
            self.last_errors.append(e)
            if fd is not None:
                os.close(fd)
            self._fd, self._local = None, local
            return True
        if not got:
            os.close(fd)
            local.release()
            return False
        self._fd, self._local = fd, local
        self._write_info(fd, name)
        return True

    def _release(self):
        fd, local = self._fd, self._local
        self._fd = self._local = None
        if fd is not None:
            try:
                os.ftruncate(fd, INFO_OFFSET)       # clear owner info before letting go
            except Exception:
                pass
            _os_unlock(fd)
            try:
                os.close(fd)
            except OSError as e:
                self.last_errors.append(e)
        if local is not None:
            local.release()

    def acquire(self, name, wait_sec=None):
        """Take the cross-process turn. Raises LockTimeout after wait_sec."""
        wait = self.lock_wait_sec if wait_sec is None else wait_sec
        t0 = self.clock()
        told = warned = False
        while True:
            if self._try_acquire(name):
                return
            info = self._read_info()
            holder = (info or {}).get("name")
            if self.clock() - t0 >= wait:
                raise LockTimeout("GPU turn not available after %.0fs (held by %r)"
                                  % (wait, holder))
            if not told:
                self._say("[gpu] waiting for turn; held by %r" % holder)
                told = True
            if (self.max_hold_sec and not warned and info
                    and self.clock() - float(info.get("since") or 0) > self.max_hold_sec):
                self._say("[gpu] warning: %r has held the turn for more than %.0fs "
                          "(not taking it over)" % (holder, self.max_hold_sec))
                warned = True
            self.sleep(self.poll_sec if self.poll_sec else min(2.0, max(0.05, wait / 20.0)))

    # ---- status ------------------------------------------------------------
    def _held_elsewhere(self):
        """True if someone holds the turn. Read-only in effect: takes the lock for an
        instant only if nobody holds it, then lets go."""
        local = _local_lock(self.lock_path)
        if not local.acquire(False):
            return True                       # a thread of this process holds it
        fd = None
        try:
            if not os.path.exists(self.lock_path):
                return False
            fd = os.open(self.lock_path, os.O_RDWR)
            if _os_try_lock(fd):
                _os_unlock(fd)
                return False
            return True
        except OSError:
            return False
        finally:
            if fd is not None:
                os.close(fd)
            local.release()

    def status(self):
        """Snapshot. Never blocks; shows owner info only while the lock is really held."""
        holder = None
        try:
            if self._held_elsewhere():
                info = self._read_info() or {}
                holder = {"name": info.get("name"), "pid": info.get("pid"),
                          "age_sec": (max(0.0, self.clock() - float(info["since"]))
                                      if info.get("since") else None)}
        except Exception:
            holder = None
        return {"free_mb": self._free(), "headroom_mb": self.headroom_mb, "holder": holder}

    # ---- the main entry ----------------------------------------------------
    def make_room_for(self, need_mb, make_room):
        """Call make_room callbacks in order until free >= need + headroom. Returns (ok, free)."""
        want = need_mb + self.headroom_mb
        free = self._free()
        if free is None:
            self._say("[gpu] free VRAM unknown; cannot verify (proceeding, ok=False)")
            return False, None
        if free >= want:
            return True, free
        for cb in make_room:
            self._say("[gpu] free %.0f MB < wanted %.0f MB; running %s"
                      % (free, want, getattr(cb, "__name__", "make_room")))
            try:
                cb()
            except Exception as e:
                self._say("[gpu] make_room callback failed: %s: %s" % (type(e).__name__, e))
                self.last_errors.append(e)
            if self.settle_sec:
                self.sleep(self.settle_sec)
            free = self._free()
            if free is None:
                return False, None
            if free >= want:
                return True, free
        return free >= want, free

    def budget(self, *a, **kw):
        return _Budget(self, *a, **kw)

    def _run_restore(self, restore):
        for cb in restore:
            try:
                cb()
            except Exception as e:                  # keep going: later restores still matter
                self._say("[gpu] restore callback failed: %s: %s" % (type(e).__name__, e))
                self.last_errors.append(e)


class _Budget:
    """Context manager returned by Broker.budget(). Yields True if the requested VRAM is
    available, else False (the body still runs; the caller decides whether to fall back
    to CPU or skip). Raises LockTimeout from __enter__ (body not run, nothing to restore)
    if the turn could not be obtained in time. One Broker object = one budget at a time."""

    def __init__(self, broker, need_mb, name="job", make_room=(), restore=(),
                 lock_wait_sec=None):
        self.b, self.need, self.name = broker, need_mb, name
        self.make_room, self.restore, self.wait = list(make_room), list(restore), lock_wait_sec

    def __enter__(self):
        b = self.b
        b.last_errors = []
        b.acquire(self.name, self.wait)
        try:
            ok, _ = b.make_room_for(self.need, self.make_room)
        except BaseException:
            try:
                b._run_restore(self.restore)      # undo whatever may have been done
            finally:
                b._release()
            raise
        if not ok:
            b._say("[gpu] could not free %d MB for %s" % (self.need, self.name))
        return ok

    def __exit__(self, *exc):
        try:
            self.b._run_restore(self.restore)
        finally:
            self.b._release()
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Show GPU broker status.")
    ap.add_argument("--status", action="store_true", default=True)
    ap.add_argument("--lock-dir")
    a = ap.parse_args(argv)
    print(json.dumps(Broker(lock_dir=a.lock_dir).status(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
