# alerting - deduplicating alert state machine

Stdlib-only, one file (`alerting.py`). 같은 문제로 메일/메시지가 반복 발송되는 것을 막고,
오래 풀리지 않으면 긴급으로 올리고, 복구되면 알려 주는 작은 상태 기계입니다.

```python
from alerting import Alerter

def send(subject, body):          # you provide delivery: mail, chat, webhook...
    print(subject); return True   # return True only if it was really delivered

a = Alerter("alert_state.json", send)

a.notify("db:down", "Database unreachable", "details...", escalate_after_min=60)
# ... each polling round while the problem persists: same call, silent until 60 min,
#     then ONE "[URGENT] ... (unresolved for 1.0 h)", then reminders after 12 h, then every 24 h.

a.resolve("db:down", "Database unreachable")   # clears key, sends "resolved" if it had escalated
```

## Behaviour

- `notify(key, title, body, escalate_after_min=None)` returns `'first'`, `'urgent'` or `None`.
  `escalate_after_min=0` is urgent from the first message; `None` never escalates.
- Severity tags are added by the module. Do not put `[ALERT]`/`[URGENT]` in titles (they are stripped).
- `resolve(key, title=None, note=None)` returns `'closed'`, `'none'` (nothing was open) or `None`
  (could not persist, or the state file was unreadable or corrupt so "open or not" is unknown;
  no "resolved" message is sent in that case). Only `None` is falsy,
  so test with `== 'closed'`. The state change is saved before the "resolved" message is sent.
- Flapping (open/close cycles shorter than 30 min, 2+ times) is batched to one message per day;
  the message says how many were held.
- State is JSON, replaced atomically (direct-write fallback). A corrupt file is moved aside
  (`state.json.corrupt-<ts>-<pid>-<n>`, unique even within one second) and a message is sent -
  never treated as "no alerts" silently. If that message cannot be delivered it is kept
  (`state.json.notice`) and retried on every later call, `last_failure` says so, and it is logged.
  If the file cannot even be moved aside (e.g. locked on Windows), it stays corrupt, so the
  notice is throttled to one per `corrupt_notice_h` hours (default 24) per state path
  (kept in memory and in the `.notice` sidecar as a delivery timestamp); `resolve()` keeps
  returning `None` meanwhile.
  An unreadable file is left untouched and nothing is written that round.
- If the state cannot be saved, alerts still go out as urgent (so they are not filtered) but are
  throttled in memory to 1 per 12 h, then 1 per 24 h, per key.
- Keys not re-reported for `stale_days` (default 7) are expired. Keys with a long
  `escalate_after_min` are kept long enough for the escalation to fire (max 30 days).
- Always call `resolve()` at the place where the condition clears, otherwise the open key
  silences the next occurrence until it goes stale.
- `Alerter(..., clock=time.time)`: inject a fake clock for tests. `python -I -m unittest` runs the tests.

Not thread/process-safe across concurrent writers: run notifiers from one scheduler, or add your own lock.
