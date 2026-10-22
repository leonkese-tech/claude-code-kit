# llm-ladder - provider fallback for text completion

Stdlib-only, one file (`ladder.py`). 여러 LLM 호출 수단(CLI -> API -> 로컬 모델 등)을 순서대로
시도해 첫 번째 "쓸 만한" 답을 돌려주는 사다리입니다. 네트워크 호출·SDK 임포트 없음 - 공급자는 직접 만든 함수입니다.

```python
from ladder import complete, complete_ex, Provider, report_coverage, mostly_korean

providers = [
    Provider("cli",   lambda prompt: call_my_cli(prompt),   tags=("cloud",)),
    Provider("api",   lambda prompt: call_my_api(prompt),   tags=("cloud",)),
    Provider("local", lambda prompt: call_my_local(prompt), tags=("local",)),
]

text = complete("Summarize ...", providers, validate=mostly_korean)   # str or None, never raises
r = complete_ex("...", providers, allow={"local"})                    # only rungs named/tagged "local"
print(r.rung, r.failures, r.skipped)                                  # who answered, why others failed

answered = sum(1 for x in batch if complete(x, providers))
report_coverage("triage", len(batch), answered)    # logs "[triage] 7 of 10 answered"; True if too low
```

## Rules

- Returns the first answer that is non-empty, is not a short API error, and passes
  `validate(text)`. If everything fails: `None`. Provider exceptions are caught and recorded.
  Error detection (`looks_like_api_error`) is a **heuristic**, not a parser. Only a short single-line output
  (or one JSON / Python-dict object) is considered, and only when the *whole* output matches:
  a body with `"type":"error"` or an `error` object having BOTH `type` and `message`; a status line
  (`API Error: 400`, `Error code: 529`, `RateLimitError: 429 ...`, `anthropic.InternalServerError: 500 ...`,
  `HTTP 503 ...`, `429 Too Many Requests`) where the status number is followed by end of line, a known reason
  phrase, ` - `, or `:` plus a known error word (class-style `FooError: NNN` may continue freely); a bare token
  (`Overloaded`, `Error: Overloaded`, `Service Unavailable`, `Too Many Requests`, `Rate limit exceeded`); or a short
  vendor limit line (`Credit balance is too low ...`, `usage limit reached`). Unknown provider wordings will
  slip through, and an unusual answer that is exactly one of these lines would be rejected.

  | Flagged as error | NOT flagged (kept as an answer) |
  |---|---|
  | `API Error: 400` | `Error: 500 rows processed` |
  | `Error code: 429 - {'type': 'error', ...}` | `Error: 429: 3 people attended` |
  | `Error code: 529` | `Error handling means catching exceptions.` |
  | `anthropic.InternalServerError: 500 ...` | `{"error":"E101","code":101}` |
  | `Error: Overloaded` / `Service Unavailable` | `{"error": {"message": "bad key"}}` (no type) |
  | `{"type":"error","error":{...}}` | `Service unavailable pages should be friendly.` |
  | `Credit balance is too low` | `Usage limits are covered in section 3.` |
- `allow=` restricts rungs (set/list of names or tags, or a predicate). Skipped rungs are listed in `result.skipped`.
- Input is never silently truncated: with `max_input_chars` set, a longer prompt raises `InputTooLongError`
  (split it yourself). Providers receive the prompt unchanged.
- `report_coverage(label, asked, answered, min_ratio=0.5, on_bad=hook)` always logs "N of M" and returns True when
  `answered == 0` or below the ratio, so a batch that "worked" but got no answers is visible. Wire `on_bad` to alerting.
- `mostly_korean` is an example validator (small local models sometimes answer in Chinese); `parse_json_list` accepts
  array / `{"items": [...]}` / single object / truncated array so callers can count answers honestly.

Tests: `python -I -m unittest` (fakes only).
