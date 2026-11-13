# .claude/

Claude Code tooling for this repo. Everything here follows the hard rules in
`CLAUDE.md`: never open flagged media, nothing leaves the machine, deletes only
behind `--yes`, originals never written, generic code only, synthetic tests in
a throwaway HOME.

## Commands (`commands/`)

| Command | Use |
| --- | --- |
| `/check` | Pre-commit gate: runs the three agents below in parallel on the working diff. Advisory. |
| `/safe-edit <task>` | Implement a change with a `code-reviewer` loop (two rounds max). For delete paths, writes near originals, the network boundary, the local servers, detectors and device handling. |
| `/bug-hunt [scope]` | Find real bugs, prove each with a probe, fix at the root, add a regression test, sweep siblings. |
| `/coverage-hunt [scope]` | Backfill tests for behaviour that works but isn't tested. |
| `/fix-ci <run>` | Root-cause a failing GitHub Actions run and fix it without band-aids. |

## Agents (`agents/engineering/`)

All read-only; they report, the caller applies.

| Agent | Reports |
| --- | --- |
| `code-reviewer` | Diff findings against `CLAUDE.md` § Hard rules and house style. |
| `test-gap-checker` | Which `tests/test_<area>.py` cases the diff should ship with, and the targeted commands. |
| `doc-hygiene-checker` | Which of `README.md`, `docs/`, `ROADMAP.md`, `CONTRIBUTING.md` and this file need updating. |

## Hooks (`hooks/`)

- `git-scope-guard.py` (PreToolUse on Bash): blocks whole-tree staging, bare
  commits, `git stash`, pushes to `main` and force-pushes. Its tests:
  `python3 -I .claude/hooks/git-scope-guard.test.py`.
- `unmerged-worktree-check.sh` (SessionStart): warns about branches holding
  commits not on `main`.

## Settings

`settings.json` wires the hooks and holds the shared permission allow and deny
lists. Personal overrides go in the git-ignored `settings.local.json`.
