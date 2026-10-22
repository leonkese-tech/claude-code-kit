# claude-code-kit

Small, self-contained helpers I use around Claude Code and local-AI work. Most are stdlib-only Python or POSIX sh; the one exception is `quote-from-template` (needs `openpyxl`). No company data, no secrets. Comments and messages are partly Korean, partly English. Includes a small Korean spelling-rule dataset.

작은 도구 모음입니다. 대부분 파이썬 표준 라이브러리와 POSIX sh만 쓰고, `quote-from-template` 만 `openpyxl` 이 필요합니다.

## Contents

| Path | What it does |
|---|---|
| `bin/git-safepush.sh` | One command: commit → fetch → rebase (only if the remote moved) → push, with retries. Built for several Claude sessions / worktrees sharing one `main`. Never force-pushes, never stashes or checks out, and refuses to rebase over a dirty tree (exit 4). It commits only what you have staged — or, if nothing is staged and nothing is unpushed, all modified *tracked* files — so `git add` your own files first when other sessions share the worktree. |
| `tools/session_waste_report.py` | Read-only analyzer of Claude Code session logs (`.jsonl`): where context/time is wasted. Outputs only fixed-vocabulary labels — never your prompts. Numbers are observations, not token measurements. |
| `alerting/` | Deduplicating alert state machine: one message per problem, urgent escalation if it stays open, back-off reminders, "resolved" notice, flapping batched, corrupt state never treated as "no alerts". You inject the `sender` callable. |
| `llm-ladder/` | Ordered provider ladder for text completion (CLI → API → local, all plain callables). Returns `None` instead of raising, treats API-error bodies as failures, `report_coverage` makes silent partial failure visible, never truncates input silently. |
| `gpu-broker/` | Share one GPU between heavy jobs: `with broker.budget(need_mb, name, make_room=[...], restore=[...])`, cross-process advisory lock with stale detection, restore always runs. |
| `code-map/` | AST skeleton + grep-by-definition for big Python files, to read only the line range you need. |
| `korean-spellfix/` | 25 hand-reviewed Korean spelling rules with legitimate-context counter-examples (`rules.jsonl`, dataset card) plus a ~85-line reference applier that protects emails, URLs, tags, quotes, `key : value` lines. |
| `quote-from-template/` | Upload any quotation/invoice-style `.xlsx`; the tool detects the layout (header, item slots, subtotal/VAT/total), fills items, inserts rows when needed, keeps amounts as formulas, and verifies the result. Ships no template of its own. |
| `workflows/plan-check.js`, `workflows/draft-polish.js` | Claude Code workflows: independent-lens plan critique; draft → independent critic → rewrite weak parts. |
| `workflows/expert-task-generic.js` | Claude Code workflow: specialist implements → independent reviewers re-read and reproduce → fix → re-review (max 3 rounds). A reviewer that returns nothing is `review_incomplete`, never "clean". |
| `docs/` | Notes: Korean encoding pitfalls on Windows; measuring agent context cost with controls; why an independent reviewer should re-check agent fixes. |

## Usage

```bash
# git-safepush: install once as an alias
git config --global alias.safepush '!sh /path/to/bin/git-safepush.sh'
git add <your files> && git safepush "feat: message"

# session waste report: point at ONE project folder under ~/.claude/projects
python tools/session_waste_report.py --dir ~/.claude/projects/<project> --days 14

# each component folder has its own README and tests
cd gpu-broker && python -I -m unittest
```

Exit codes of `git-safepush`: 0 pushed · 1 rebase/merge in progress · 2 rebase failed (usually a conflict; the conflicting files are printed) · 3 gave up after 5 retries · 4 remote is ahead but you have uncommitted changes · 5 push failed (not a race) · 6 could not reach the remote.

## Status

Extracted from a private repo, then rewritten or scrubbed by hand and reviewed by a second, independent reviewer. Each component folder ships its own tests (all pass on Windows with Python 3 and `python -I -m unittest`; not run on POSIX). `git-safepush.sh` was exercised in throwaway repos. `workflows/*.js` pass a syntax check and were run only against stubbed globals, not in a real Workflow runtime. Treat everything as beta: read the code before pointing it at anything important. MIT licensed.
