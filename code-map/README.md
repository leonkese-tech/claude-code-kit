# code-map

A token-saving navigator for big Python code bases. Instead of opening a 3,000-line
file to find one function, print its skeleton (about 1-3% of the size on files with
long function bodies), then read only the line range you need. Stdlib only.

```
python code_map.py --file big_module.py
python code_map.py --grep retry src tools
python code_map.py --file big_module.py --lines 120-180
```

Skeleton output (module docstring first line, classes, functions/methods with
signatures and `[start-end]` line ranges, UPPER_CASE constants):

```
# big_module.py (2410 lines)
"""HTTP client helpers."""
MAX_RETRIES                                                  [14]
def fetch(url, timeout)                                      [20-58]  # Fetch a URL with retry.
class Client                                                 [61-190]
  def send(self, data)                                       [70-112]
```

`--grep WORD [paths...]` (case-insensitive, default path `.`) groups matches by the
innermost enclosing def/class, so the output tells you which range to read.

Both modes print a token estimate (characters / 3.2) so you can see what you saved.
Files that do not parse are reported and skipped, never fatal. Output is UTF-8 safe
on Windows consoles.

## Tests

`python -I -m unittest`

## Limits

Python only; no cross-file call graph or semantic search - it is an index of
definitions plus a word lookup. Token counts are rough estimates.
