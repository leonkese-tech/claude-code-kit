"""Deduplicating alert state machine (stdlib only).

One problem == one *key*.  The first ``notify`` for a key sends a message; later
calls for the same key stay silent until either

* the problem has been open longer than ``escalate_after_min`` -> one URGENT
  message, then reminders that back off (``repeat_h`` hours, then
  ``repeat_long_h`` hours), or
* it is resolved -> ``resolve`` clears the key and (if the problem had been
  escalated and a title is given) sends a "resolved" message.

Design rules kept from production use:

* The caller never puts severity tags in titles; this module adds them.
* ``resolve`` returns three distinguishable results: ``'closed'``, ``'none'``
  (nothing was open) and ``None`` (could not persist, or the state file was
  unreadable or corrupt so "was it open?" is unknown).  Only ``None`` is falsy.  Compare with ``== 'closed'``, not truthiness.
* A *corrupt* state file is quarantined (renamed) and reported; it is never
  silently treated as "no alerts".  The notice about it is retried on later calls
  until the sender accepts it (kept in ``<state>.notice`` and in memory) and
  ``last_failure`` says so meanwhile.  An *unreadable* file is left untouched and
  nothing is written that round (so other open keys are not lost).
* Open/close cycles faster than ``flap_window_min`` ("flapping") are batched
  into at most one message per ``flap_mail_h`` hours; the batch is announced in
  the message body, never hidden.
* When state cannot be saved, the alert is still sent (as urgent, so it is not
  filtered away) but throttled in memory so a frequent poller cannot flood.
* Keys that were not re-reported for ``stale_days`` are expired on save.

The state file is JSON, written atomically (temp file + ``os.replace``) with a
direct-write fallback for platforms where the replace is refused.
"""

import itertools
import json
import os
import time

FLAP = "__flap__"
_QCOUNTER = itertools.count()

DEFAULT_TAG_NORMAL = "[ALERT]"
DEFAULT_TAG_URGENT = "[URGENT]"


def _fmt_duration(mins):
    mins = float(mins)
    if mins < 90:
        return "%d min" % round(mins)
    if mins < 36 * 60:
        return "%.1f h" % (mins / 60.0)
    return "%.1f days" % (mins / 1440.0)


class Alerter:
    """Alert state machine.

    sender(subject, body) -> bool   injected delivery function (True = delivered).
    clock() -> float                seconds since epoch; inject a fake in tests.
    """

    def __init__(self, state_path, sender, clock=time.time, *,
                 tag_normal=DEFAULT_TAG_NORMAL, tag_urgent=DEFAULT_TAG_URGENT,
                 repeat_h=12.0, repeat_long_h=24.0,
                 flap_window_min=30.0, flap_min_cycles=2, flap_mail_h=24.0,
                 stale_days=7.0, stale_grace_days=3.0, stale_max_days=30.0,
                 corrupt_notice_h=24.0, logger=None):
        self.state_path = str(state_path)
        self.sender = sender
        self.clock = clock
        self.tag_normal = tag_normal
        self.tag_urgent = tag_urgent
        self.repeat_h = float(repeat_h)
        self.repeat_long_h = float(repeat_long_h)
        self.flap_window_min = float(flap_window_min)
        self.flap_min_cycles = int(flap_min_cycles)
        self.flap_mail_h = float(flap_mail_h)
        self.stale_days = float(stale_days)
        self.stale_grace_days = float(stale_grace_days)
        self.stale_max_days = float(stale_max_days)
        self.corrupt_notice_h = float(corrupt_notice_h)
        self.logger = logger
        self._last_notice_at = 0.0   # when the corruption notice was last delivered
        self._degraded_sent = {}     # key -> (last_send_ts, count) while state is unsavable
        self.last_failure = ""
        self._pending_notice = None  # (subject, body) of an undelivered corruption notice

    # ------------------------------------------------------------------ utils
    def _log(self, msg):
        if self.logger:
            try:
                self.logger(msg)
            except Exception:
                pass

    def _send(self, subject, body):
        try:
            return bool(self.sender(subject, body))
        except Exception as e:           # a broken sender must never break the caller
            self._log("sender raised %r" % (e,))
            return False

    def strip_tags(self, title):
        """Remove severity tags a caller may have put at the front of a title."""
        t = (title or "").strip()
        tags = [x.lower() for x in (self.tag_normal, self.tag_urgent) if x]
        changed = True
        while changed:
            changed = False
            for tag in tags:
                if t.lower().startswith(tag):
                    t = t[len(tag):].lstrip()
                    changed = True
        return t

    # ------------------------------------------------------------ persistence
    def _read(self):
        """-> (dict|None, status, reason); status in ok|none|unreadable|corrupt."""
        try:
            with open(self.state_path, "rb") as f:
                raw = f.read()           # short-lived handle, parse after closing
        except FileNotFoundError:
            return None, "none", ""
        except OSError as e:
            return None, "unreadable", repr(e)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            return None, "corrupt", repr(e)
        if not isinstance(data, dict):
            return None, "corrupt", "top level is %s, not an object" % type(data).__name__
        return data, "ok", ""

    def _quarantine(self):
        # whole seconds are not unique: add pid + a counter so two corruptions never collide
        dst = "%s.corrupt-%d-%d-%d" % (self.state_path, int(self.clock()), os.getpid(),
                                       next(_QCOUNTER))
        try:
            os.replace(self.state_path, dst)
            return dst
        except OSError:
            return ""

    # The "state was corrupt" notice must not be fire-and-forget: if the sender fails,
    # it is kept (memory + a sidecar file) and retried on the next call.
    def _notice_path(self):
        return self.state_path + ".notice"

    def _deliver_notice(self, notice):
        if self._send(*notice):
            self._pending_notice = None
            now = self.clock()
            self._last_notice_at = now
            try:        # sidecar keeps only the delivery time (throttle), not a pending retry
                with open(self._notice_path(), "w", encoding="utf-8") as f:
                    json.dump({"sent_at": now}, f)
            except OSError:
                pass
            return True
        self._pending_notice = notice
        try:
            with open(self._notice_path(), "w", encoding="utf-8") as f:
                json.dump({"subject": notice[0], "body": notice[1]}, f)
        except OSError as e:
            self._log("could not store pending corruption notice: %r" % (e,))
        self.last_failure = "state corrupt notice not delivered (will retry)"
        self._log(self.last_failure)
        return False

    def _retry_pending_notice(self):
        notice = self._pending_notice
        if notice is None:
            try:
                with open(self._notice_path(), encoding="utf-8") as f:
                    d = json.load(f)
                notice = (str(d["subject"]), str(d["body"]))     # no subject = delivered marker
            except (OSError, ValueError, KeyError, TypeError):
                return
        self._deliver_notice(notice)

    def _notice_recent(self):
        """True if a corruption notice was delivered less than corrupt_notice_h ago
        (remembered in memory and in the .notice sidecar)."""
        last = self._last_notice_at
        try:
            with open(self._notice_path(), encoding="utf-8") as f:
                last = max(last, float(json.load(f).get("sent_at") or 0))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return bool(last) and 0 <= self.clock() - last < self.corrupt_notice_h * 3600.0

    def _forget_notice(self):
        self._last_notice_at = 0.0
        try:
            with open(self._notice_path(), encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "subject" not in d:     # delivered marker only
                os.remove(self._notice_path())
        except (OSError, ValueError):
            pass

    def _load(self, meta):
        self._retry_pending_notice()
        data, how, why = self._read()
        meta["primary"] = how
        if how == "unreadable":
            self.last_failure = "state unreadable: %s" % why
            self._log("state file unreadable (%s); leaving it untouched this round" % why)
        elif how == "corrupt":
            moved = self._quarantine()
            self.last_failure = "state corrupt: %s" % why
            self._log("state file corrupt (%s); quarantined to %r, history lost" % (why, moved))
            if not moved and self._notice_recent():
                # Same unresolved corrupt file (quarantine failed): do not repeat the notice
                # on every call.
                self._log("corruption notice already sent recently; not repeating")
                return dict(data or {})
            # Not silent: tell a human that history was lost (retried until delivered).
            self._deliver_notice((
                "%s alert state file was corrupt" % self.tag_urgent,
                "The alert state file could not be parsed (%s).\n"
                "It was moved aside to: %s\n"
                "Open-problem and escalation history was reset; some problems may be "
                "announced again as new." % (why, moved or "(could not move)")))
        else:
            self._forget_notice()        # file is fine (or gone): a later corruption is new
        return dict(data or {})

    def _keep_days(self, escalate_after_min):
        if escalate_after_min is None:
            return None
        need = float(escalate_after_min) / 1440.0 + self.stale_grace_days
        if need <= self.stale_days:
            return None
        return min(need, self.stale_max_days)

    def _sweep(self, st, now):
        for k in list(st):
            rec = st[k]
            if k == FLAP or not isinstance(rec, dict):
                continue
            limit = max(self.stale_days, float(rec.get("keep_days") or 0))
            seen = float(rec.get("last_seen_at") or rec.get("last_sent_at")
                         or rec.get("first_at") or 0)
            if seen and now - seen > limit * 86400.0:
                st.pop(k, None)

    def _save(self, st, meta):
        """True if the state is durably stored.  Never writes after an unreadable read."""
        if meta.get("primary") == "unreadable":
            return False
        self._sweep(st, self.clock())
        payload = json.dumps(st, ensure_ascii=False, indent=1)
        d = os.path.dirname(os.path.abspath(self.state_path))
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        tmp = "%s.tmp-%d" % (self.state_path, os.getpid())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.state_path)
            return True
        except OSError as e:
            self.last_failure = "atomic replace failed: %r" % (e,)
            try:
                os.remove(tmp)
            except OSError:
                pass
        try:                                      # fallback: direct write
            with open(self.state_path, "w", encoding="utf-8") as f:
                f.write(payload)
            return True
        except OSError as e:
            self.last_failure = "direct write failed: %r" % (e,)
            self._log(self.last_failure)
            return False

    # --------------------------------------------------------------- flapping
    def _flap_rec(self, st, key, now):
        box = st.get(FLAP)
        if not isinstance(box, dict):
            box = {}
            st[FLAP] = box
        fl = box.get(key) or {}
        horizon = self.flap_mail_h * 3600.0
        fl["cycles"] = [float(t) for t in (fl.get("cycles") or []) if now - float(t) <= horizon]
        for k in [k for k, v in box.items()
                  if k != key and not (v or {}).get("cycles")
                  and now - float((v or {}).get("last_mail_at") or 0) > horizon]:
            box.pop(k, None)
        box[key] = fl
        return fl

    def _flap_tidy(self, st, key):
        box = st.get(FLAP)
        if not isinstance(box, dict):
            return
        fl = box.get(key) or {}
        if not fl.get("cycles") and not fl.get("held") and not fl.get("last_mail_at"):
            box.pop(key, None)
        if not box:
            st.pop(FLAP, None)

    def _flap_hold(self, fl, now, mins):
        if len(fl.get("cycles") or []) < self.flap_min_cycles:
            return False                          # not flapping yet
        if mins > self.flap_window_min:
            return False                          # this one is long-lived: tell people
        return (now - float(fl.get("last_mail_at") or 0)) / 3600.0 < self.flap_mail_h

    def _flap_head(self, fl):
        n, held = len(fl.get("cycles") or []), int(fl.get("held") or 0)
        if n < self.flap_min_cycles and not held:
            return ""
        return ("Note: this problem opened and closed %d times in the last %d hours (flapping).\n"
                "Repeats are batched to one message per day; %d message(s) were held.\n\n"
                % (n, int(self.flap_mail_h), held))

    # ----------------------------------------------------------------- notify
    def notify(self, key, title, body, escalate_after_min=None):
        """Send at most one message for this problem key.

        Returns 'first' / 'urgent' when a message was sent, else None.
        escalate_after_min=0 means urgent from the first message;
        None means never escalate.
        """
        title = self.strip_tags(title)
        meta = {}
        st = self._load(meta)
        now = self.clock()
        rec = st.get(key) or {}
        first_at = float(rec.get("first_at") or 0)
        sent_first = bool(rec.get("sent_first"))
        sent_urgent = bool(rec.get("sent_urgent"))
        last_at = float(rec.get("last_sent_at") or 0)

        kd = self._keep_days(escalate_after_min)
        if kd is None:
            rec.pop("keep_days", None)
        else:
            rec["keep_days"] = kd
        if not first_at:
            first_at = now
            rec["first_at"] = first_at
        mins = (now - first_at) / 60.0

        esc = escalate_after_min is not None and mins >= float(escalate_after_min)
        kind = None
        if not sent_first:
            kind = "urgent" if esc else "first"
        elif esc and not sent_urgent:
            kind = "urgent"
        elif sent_urgent:
            gap = self.repeat_h if int(rec.get("urgent_count") or 1) < 2 else self.repeat_long_h
            if (now - last_at) / 3600.0 >= gap:
                kind = "urgent"

        if not kind:
            rec["last_seen_at"] = now
            st[key] = rec
            self._save(st, meta)
            return None

        fl = self._flap_rec(st, key, now)
        if self._flap_hold(fl, now, mins):
            fl["held"] = int(fl.get("held") or 0) + 1
            rec["last_seen_at"] = now
            st[key] = rec
            self._save(st, meta)
            return None

        # Persist first_at BEFORE sending so the escalation clock never restarts.
        rec["last_seen_at"] = now
        st[key] = rec
        self._flap_tidy(st, key)
        durable = self._save(st, meta)
        fl = self._flap_rec(st, key, now)

        warn_head, degraded_n = "", 0
        if not durable:
            last_ts, degraded_n = self._degraded_sent.get(key, (0.0, 0))
            gap_h = self.repeat_h if degraded_n < 2 else self.repeat_long_h
            if degraded_n and (now - last_ts) / 3600.0 < gap_h:
                self._log("alert held (state not savable, throttled): %s" % title)
                return None
            warn_head = ("Note: the alert state file could not be saved, so escalation cannot be "
                         "tracked. Sending as urgent so this is not filtered away; "
                         "limited to one message per %d h per problem meanwhile.\n\n"
                         % int(gap_h if degraded_n else self.repeat_h))

        if kind == "urgent" and mins < 1.0:
            subject = "%s %s" % (self.tag_urgent, title)
            head = ("This problem needs human action, so it is urgent from the first message.\n\n")
        elif kind == "urgent":
            subject = "%s %s (unresolved for %s)" % (self.tag_urgent, title, _fmt_duration(mins))
            head = ("This problem has been open for %s. Figures below are re-measured now, "
                    "not copied from the first message.\n\n" % _fmt_duration(mins))
        elif not durable:
            subject = "%s %s" % (self.tag_urgent, title)
            head = ""
        else:
            subject = "%s %s" % (self.tag_normal, title)
            head = ""

        ok = self._send(subject, warn_head + self._flap_head(fl) + head + body)
        if ok and not durable:
            self._degraded_sent[key] = (now, degraded_n + 1)
        if ok:
            rec["sent_first"] = True
            if kind == "urgent" and durable:
                # A forced-urgent subject from a failed save must not count as a real escalation.
                rec["sent_urgent"] = True
                rec["urgent_count"] = int(rec.get("urgent_count") or 0) + 1
            rec["last_sent_at"] = now
            fl["last_mail_at"] = now
            fl["held"] = 0
            rec["last_seen_at"] = now
            st[key] = rec
            self._flap_tidy(st, key)
            self._save(st, meta)
        return kind if ok else None

    # ---------------------------------------------------------------- resolve
    def resolve(self, key, title=None, note=None):
        """Call when the problem is gone.

        Returns 'closed' | 'none' | None  (see module docstring; only None is falsy).
        The state change is recorded BEFORE the 'resolved' message is sent, so a
        failing disk cannot make a "resolved" message repeat on every call.
        """
        meta = {}
        st = self._load(meta)
        if meta.get("primary") in ("unreadable", "corrupt"):
            # "don't know" is not "nothing open": history is unreadable or was just lost
            return None
        rec = st.pop(key, None)
        if rec is None:
            return "none"
        now = self.clock()
        mins = (now - float(rec.get("first_at") or now)) / 60.0

        fl = self._flap_rec(st, key, now)
        if mins <= self.flap_window_min:
            fl["cycles"] = (fl.get("cycles") or []) + [now]
        tell = bool(rec.get("sent_urgent")) and bool(title)
        hold = tell and self._flap_hold(fl, now, mins)
        if hold:
            fl["held"] = int(fl.get("held") or 0) + 1
        self._flap_tidy(st, key)
        if not self._save(st, meta):
            return None                           # key is still open on disk: do not claim success
        if tell and not hold:
            sent = self._send("%s %s - resolved" % (self.tag_urgent, self.strip_tags(title)),
                              self._flap_head(fl)
                              + "The problem reported earlier is resolved (lasted %s)."
                              % _fmt_duration(mins)
                              + (("\n\n" + note) if note else ""))
            if sent:
                fl = self._flap_rec(st, key, now)
                fl["last_mail_at"] = now
                fl["held"] = 0
                self._flap_tidy(st, key)
                self._save(st, meta)
        return "closed"

    # ---------------------------------------------------------------- queries
    def open_keys(self, prefix="", meta=None):
        """Open problem keys starting with prefix (read-only).

        An empty list may mean "state unreadable": pass a dict as ``meta`` and check
        ``meta['primary']`` ('ok'|'none'|'unreadable'|'corrupt').
        """
        data, how, _ = self._read()
        if meta is not None:
            meta["primary"] = how
        return sorted(k for k, v in (data or {}).items()
                      if k != FLAP and isinstance(v, dict) and k.startswith(prefix))

    def minutes_open(self, key):
        data, _, _ = self._read()
        fa = float(((data or {}).get(key) or {}).get("first_at") or 0)
        return 0.0 if not fa else (self.clock() - fa) / 60.0
