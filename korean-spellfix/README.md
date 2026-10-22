---
license: mit
language:
- ko
task_categories:
- text2text-generation
tags:
- korean
- spelling-correction
- rule-based
- negative-examples
pretty_name: Korean Spellfix Rules (with counter-examples)
size_categories:
- n<1K
---

# Korean Spellfix Rules

25 deterministic Korean spelling-correction rules, each shipped with the contexts in which the
"wrong-looking" form is actually legitimate and must **not** be changed.

The point of the dataset is the counter-examples. Many Korean "typo" rules are easy to write and
dangerous to run: `금새 -> 금세` corrupts the noun 금새 (price of goods), `-께 -> -게` corrupts the
honorific particle (사장님께), `희안 -> 희한` corrupts names. Every rule here was written together with
the sentences that would break it, and a test suite pins both sides.

## Files

| File | Content |
|---|---|
| `rules.jsonl` | one rule per line |
| `apply_rules.py` | reference implementation (stdlib only, ~85 lines) |
| `test_apply_rules.py` | proves every example is fixed and every counter-example is untouched |

Run the tests: `python -I -m unittest` (inside this directory).
Apply to a file: `python apply_rules.py input.txt > output.txt`.

## Fields

| Field | Meaning |
|---|---|
| `id` | romanised identifier |
| `wrong`, `corrected` | human-readable representative of the change |
| `pattern`, `replacement` | Python `re` pattern and replacement (`\1` back-references) |
| `not_after` | optional regex; if the 40 characters before a match end with it, the match is skipped (variable-length lookbehind substitute) |
| `prev_jong` | optional; only fix when the preceding syllable has a final consonant other than ㄹ |
| `rationale` | why the rule exists / why it is scoped the way it is |
| `legit_context` | where the "wrong" form is legitimate, or a statement that none exists |
| `counter_examples` | strings that must come out of the whole rule set unchanged |
| `examples` | `{wrong, fixed}` pairs that must be fixed exactly |

## How the rules were built

The rules were collected from everyday business writing and reviewed one by one;
project-specific entries were left out. Rules were deliberately limited to cases where the wrong
form is almost always wrong. Cases whose correctness depends on meaning (되요/돼요, 낳다/낫다,
바램/바람, 안되/안 되) are **not** included - they need a human or a language model.

Each rule was added together with at least one counter-example; most carry several. Rules that
turned out to over-match were narrowed with lookbehinds (`halkke1`), a "must be followed by"
condition (`kkomkkomi`, `itseumnida`), or `not_after` / `prev_jong` guards (`andwae`, `eumnida`).
The verb-stem list of `halkke` contains only common neutral verbs.

## Using it safely (what `apply_rules.py` does)

1. **Mask protected spans** - URLs, e-mail addresses, `[tags]`, quoted text (`"…"`, `“…”`, `«…»`, `‘…’`, `「…」`, `『…』`, and straight `'…'` where the quotes
   clearly delimit a span - apostrophes inside words such as don't are not quotes, and an apostrophe between two ASCII letters inside a quoted span, as in `'don't …'` or `'it's …'`, does not end it), `key : value` lines,
   and any ASCII/number/SKU-like token - with the placeholder `§`.
2. **Apply rules to the masked text only**, so protected text is never edited and a rule never
   matches across it.
3. Splice replacements into the original.

If you reuse the rules in another engine, keep these three steps; the patterns assume that `§`
precedes ASCII-adjacent Hangul (see `halkke1`).

## Limitations

- Coverage is small by design; this is a safety-first list, not a full speller.
- Only the contexts in `counter_examples` are guaranteed. Names and rare nouns can still collide
  (e.g. a person called "꼼꼼이"; the rule only fires before a verb, but that is a heuristic).
- Some deliberately skipped cases leave an erroneous original untouched (`andwae`, `itseum`) because
  no single correct output exists.
- Written against standard Korean orthography (as of the 2020s); later changes to the standard
  language are not tracked. Not a substitute for proofreading legal or contractual text.
- Patterns operate on NFC text; normalise first if your input may be NFD.

## License

MIT.
