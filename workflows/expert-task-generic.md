# expert-task-generic

A Claude Code Workflow script: one specialist agent implements a change, independent read-only
reviewers re-read and try to reproduce problems, the specialist fixes what it can reproduce, and the
reviewers look again - up to 3 rounds.

```
Implement (specialist)  ->  Review (parallel, different lenses)  ->  Fix (specialist)  ->  Review ...
```

## Result status

| status | meaning |
|---|---|
| `clean` | every configured reviewer answered and found nothing worth fixing. **Not the same as zero defects**: low-severity, low-confidence defects are not handed to the fixer and stay listed in `rounds[].defects` - read them before trusting the change |
| `defects_remain` | the round limit was reached with defects still open |
| `review_incomplete` | at least one reviewer returned nothing usable - **unknown, not clean**. Counted as incomplete: no answer, no `defects` array (malformed), empty `scope`, or `unverified` items with an empty `tests` list (it ran nothing). `rounds[i].incomplete` lists reviewer and reason |
| `no_change` | the specialist produced no commit and no file change - nothing was reviewed |
| `implement_failed` | the specialist's first call returned nothing |
| `fix_failed` | the fixer returned nothing usable or threw. `work` is the latest known report (the previous round's), `error` says why, `fixer_may_have_committed: true` - check `git log`, a throwing fixer can have committed before it died |
| `error` | an unexpected exception after the run started (specialist or reviewers). Never thrown: `work` (last known report, so commits already made stay reported), `rounds` and `error` are returned |

The return value also carries `rounds` (all defects, unverified items, reviewer tests per round),
`needs_user` (decisions the agents refused to make) and `work` (the last specialist report).

## Usage

Run it with the Workflow tool, `args` as JSON:

```json
{
  "task": "Fix the rounding bug in invoice totals and add a regression test.",
  "specialist": "billing-expert",
  "agentDefinitionPath": ".claude/agents/billing-expert.md",
  "risk": "high",
  "domainLens": "Compute totals by hand for 5 realistic invoices and compare with the code path used by the API and by the export job.",
  "collaborators": [{ "name": "reporting", "lens": "Monthly report totals must still equal the sum of invoices." }],
  "checkCommand": "pytest -q tests/billing",
  "maxRounds": 3
}
```

| arg | default | meaning |
|---|---|---|
| `task` | required | what to do |
| `specialist` | `general-purpose` | agent type that implements and fixes |
| `reviewer` | `general-purpose` | agent type used for every reviewer (give it a read-only definition if you have one) |
| `agentDefinitionPath` | none | if an agent type is not registered yet, a general agent reads this file and follows it |
| `risk` | `normal` | any other value throws an error listing the valid ones. `low` = 1 reviewer, `normal` = common + domain, `high` = common + domain + adversarial |
| `lensesByRisk` | see above | e.g. `{"normal": ["common","domain","adversarial"]}`; lens names `common`, `domain`, `adversarial`, `collaborators` |
| `domainLens` | generic text | the one thing only someone who knows the area would check |
| `collaborators` | `[]` | `{name, lens}` - one extra reviewer each, from a neighbouring area's point of view |
| `maxRounds` | 2 (all risk levels, so `low` also gets one fix pass) | hard-capped at 3; `1` = review only: defects end as `defects_remain` and no fix phase runs |
| `checkCommand` | none | project test/lint command both sides must run |
| `specialistModel`, `reviewerModel` | session default | `reviewerModel` is used for the domain reviewer so one reviewer runs on a different model |
| `extraRules` | none | project rules text appended to every prompt |

Configuration mistakes (unknown `risk`, unknown lens name such as a typo, an empty lens list, zero
reviewers) throw **before** the specialist runs, so nothing is committed for a run that could never be
reviewed. A single reviewer that throws is recorded as incomplete (`reviewer threw: ...`) and the other
reviewers' answers are kept.

## Design reasons

- **Reviewers are separate agents because the author cannot see its own side effects.** A fixer's
  "tests pass" only covers what the fixer thought to test. In practice a second reader who re-reads
  the commit and tries to reproduce finds new defects in a large share of first fixes, even when the
  test suite is green. The weakness is not missing knowledge; it is blindness to one's own change.
- **Unverified assumptions are reviewed first.** The specialist must list the assumptions it relied
  on without checking, with "what breaks if wrong". Reviewers start there. An empty list is treated as
  a warning sign, not as a good sign.
- **Lenses differ on purpose.** Common (regressions, silent error paths, unmentioned files), domain
  (real entry point, before/after on realistic input), adversarial (assume it is wrong; boundaries,
  re-runs, corrupted state) and collaborator (another area's rules). Identical reviewers find
  identical things. The adversarial reviewer may only report what it can confirm; the rest goes to
  `unverified`.
- **Silence is never a pass.** If any reviewer returns nothing usable (timeout, error, malformed output, empty scope,
  or "could not run anything"), the run ends as `review_incomplete` without throwing. Likewise a run that changed nothing ends as `no_change`, because a reviewer
  looking at an empty diff would otherwise report "clean".
- **Bounded loop.** Three rounds at most; if the same class of defect keeps coming back, the right
  move is usually to delete or guard the feature rather than patch it again.
- **Tests must not touch the real world.** Every prompt tells both sides to stub sending, production
  data writes and paid services before running anything.
- **Hand to the fixer:** all high/medium defects, plus low-severity ones only with high confidence.
  The fixer may decline a defect with evidence in `notes`.

## Notes

- The script uses the Workflow globals `args`, `agent()`, `parallel()`, `phase()`, `log()`; it cannot
  run outside a Claude Code Workflow. It was syntax-checked and exercised against stubbed globals only.
- Commits are made by the specialist; adapt the Implement prompt in the script if your repository
  needs a different commit/push procedure.
