# gpu-broker

Share one consumer GPU between several heavy jobs (a local LLM server, an image
generator, a video model) without them fighting over VRAM. Single file, stdlib only.

```python
from gpu_broker import Broker

broker = Broker()   # free VRAM via nvidia-smi; lock files in the temp dir

with broker.budget(12000, "cover image",
                   make_room=[unload_llm],        # tried in order, re-measured after each
                   restore=[reload_llm]) as ok:   # ALWAYS called, even if the body raises
    if ok:
        generate_image()
    else:
        generate_on_cpu_or_skip()
```

`need_mb` is in MiB. `ok` is `False` when the room could not be made (or free VRAM
could not be read); the body still runs and you decide the fallback.

## What it guarantees

- **Restore always runs.** `try/finally`; one failing restore does not skip the rest.
  Failures are collected in `broker.last_errors` instead of being swallowed silently.
- **Cheapest first.** `make_room` callbacks run in the order you pass them, and
  free VRAM is re-measured after each one; it stops as soon as there is enough.
  Put the cheap-to-undo ones (unload a model that reloads in seconds) before the
  expensive ones (restart a server).
- **Headroom.** `need_mb + headroom_mb` (default 1024) must be free, to survive
  fragmentation and transient peaks.
- **Settle time.** `settle_sec` (default 3) is waited after each callback, because
  drivers report stale numbers right after an unload.
- **One swapper at a time (OS advisory lock).** The turn is an exclusive,
  non-blocking OS lock on one permanent file (`<lock_dir>/gpu_broker.lock`):
  `msvcrt.locking` on Windows, `fcntl.flock` on POSIX, polled until `lock_wait_sec`
  (default 30 min, then `LockTimeout`). The file is never deleted by contenders.
  The OS releases the lock when the holder process exits or is killed, so a dead
  holder needs no "stale lock" handling and there is no takeover step (the race
  class of the earlier file-publish design cannot occur). A process-local lock keyed
  by the lock path makes several `Broker` objects (and threads) in one process
  exclude each other too. The holder writes `{name, pid, since}` into the file
  (after byte 16) for `status()` only; it is informational and best effort.
- **No stale timeout.** A body that never returns keeps the turn forever. Optional
  `max_hold_sec` makes waiters log one warning when the holder is older than that; it
  never takes the turn over.
- **Never blocks on the unknown.** If the VRAM probe fails, `ok` is `False` but the
  job is not stopped; logging errors never propagate.
- **The broker does not touch other jobs.** It only calls callbacks you pass in.
  Do not unload/restart partner jobs ad hoc elsewhere; route it through here.

## Design advice (learned the hard way)

- When restoring a big model, check there is room *first*. Reloading a large model
  on top of a job that still holds VRAM spills into system RAM and can freeze the
  machine. A `restore` callback that decides "not enough room, retry later" is
  better than one that forces it.
- Do not put interactive, seconds-long calls behind hours-long batch jobs in the
  same lock; use a separate `lock_dir` per class of work.

## API

- `Broker(free_vram_mb=None, lock_dir=None, clock=None, sleep=None, headroom_mb=1024,
  lock_wait_sec=1800, settle_sec=3, log=None, poll_sec=None, max_hold_sec=None)`
- `Broker.budget(need_mb, name, make_room=(), restore=(), lock_wait_sec=None)`
- `Broker.status()` -> `{"free_mb", "headroom_mb", "holder": {name, pid, age_sec} | None}`
  (`holder` is set only while the OS lock is really held; it never changes the lock)
- `python gpu_broker.py --status [--lock-dir DIR]`

## Tests

`python -I -m unittest` (fakes only: no GPU, no network; about 12 s). Includes
multi-process tests with real child processes: 16 trials of 8 workers started at the
same instant contend for one lock directory (O_EXCL marker per body) and must never
overlap, and afterwards the lock directory holds only the one permanent lock file; a
holder that is hard-killed (or terminated) mid-budget lets the next worker in
promptly; threads and several `Broker` objects in one process exclude each other.

Limits:
- Advisory: every job must go through the broker.
- Local filesystems only. OS file locks are unreliable on network shares; keep
  `lock_dir` on a local disk.
- Deleting the lock file while it is held: Windows refuses (the file is open), so the
  lock cannot be pulled away. On POSIX the unlink succeeds; the holder carries on
  unharmed, but a newcomer then creates a fresh file and is no longer excluded. Do not
  delete the lock file.
- No stale timeout (see above); a hung body blocks everyone until its process ends.
- GPU 0 only by default (pass your own `free_vram_mb` for others).
- If the OS lock call itself fails (filesystem without lock support), the broker logs
  it, appends to `last_errors`, and falls back to process-local exclusion only.

## POSIX note

On POSIX the lock is `flock`, which belongs to the open file description: if a holder forks and the child keeps the descriptor open, the lock outlives the parent. Close the descriptor in children (or use `close_fds`). The POSIX path has been read but not run by the author.
