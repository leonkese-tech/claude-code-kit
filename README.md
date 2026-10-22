# claude-code-kit

Small, self-contained helpers I use around Claude Code. Everything here is stdlib-only (Python, POSIX sh) or a plain workflow script; no company data, no secrets. Comments and messages are in Korean.

작은 도구 모음입니다. 파이썬 표준 라이브러리와 POSIX sh만 씁니다.

## Contents

| Path | What it does |
|---|---|
| `bin/git-safepush.sh` | One command: commit → fetch → rebase (only if the remote moved) → push, with retries. Built for several Claude sessions / worktrees sharing one `main`. Never force-pushes, never stashes or checks out, and refuses to rebase over a dirty tree (exit 4). It commits only what you have staged — or, if nothing is staged and nothing is unpushed, all modified *tracked* files — so `git add` your own files first when other sessions share the worktree. |
| `tools/session_waste_report.py` | Read-only analyzer of Claude Code session logs (`.jsonl`): where context/time is wasted (huge tool results, repeated commands, compactions). Outputs only fixed-vocabulary labels — never your prompts. Numbers are observations, not measurements of tokens. |
| `workflows/plan-check.js` | Claude Code workflow: three independent lenses (break the plan / list assumptions / check facts) then a fixed-format summary. Read-only. |
| `workflows/draft-polish.js` | Claude Code workflow: draft → independent critic scores 0–10 → rewrite only the weak parts (max 3 rounds). The critic is never the writer. |
| `docs/korean-encoding-pitfalls.md` | Windows + Korean text: the cp949 / BOM / CRLF / chcp traps and what to do about each. |

## Usage

```bash
# git-safepush: install once as an alias
git config --global alias.safepush '!sh /path/to/bin/git-safepush.sh'
git add <your files> && git safepush "feat: message"

# session waste report: point at ONE project folder under ~/.claude/projects
python tools/session_waste_report.py --dir ~/.claude/projects/<project> --days 14
```

Exit codes of `git-safepush`: 0 pushed · 1 rebase/merge in progress · 2 rebase failed (usually a conflict; the conflicting files are printed) · 3 gave up after 5 retries · 4 remote is ahead but you have uncommitted changes · 5 push failed (not a race) · 6 could not reach the remote.

## Status

Extracted from a private repo and scrubbed by hand. Not packaged, no tests included. `git-safepush.sh` was exercised in throwaway repos (normal push, remote moved, dirty tree, conflict). `session_waste_report.py` was run on synthetic logs. `workflows/*.js` pass a syntax check but have not been run end to end. Licensed under MIT.
