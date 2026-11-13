---
description: Pre-commit gate. Runs code-reviewer, test-gap-checker and doc-hygiene-checker in parallel against the working diff and aggregates their findings. Advisory output; cheaper than /safe-edit. Use it before every non-trivial commit.
---

Run a parallel review of the working diff, aggregate the findings, and report. Advisory only: you don't apply fixes here; the user decides which to land.

## When to use this command

**Right fit:**
- Right before committing a non-trivial change.
- After a bug fix, to confirm a regression test went with it.
- After a feature change, to confirm coverage and docs went with it.

**Wrong fit, refuse:**
- Trivial diffs (typos, comment edits, a pin bump with no source change). Every agent skips these.
- An empty `git status`: there's nothing to review. Tell the user.

## What this command does NOT do

- Apply fixes. Each agent is read-only; the output is a list of gaps.
- Replace `/safe-edit`'s coder and reviewer loop, which is for changes to delete paths, sidecar writes, the network boundary, the local servers or the detectors.

## Procedure

### 1. Sanity-check the diff exists

Run `git status`. If staged and unstaged are both empty and there's no committed branch work to review, stop and say so.

### 2. Confirm it's not trivial

If the diff is trivial, stop with a one-line "trivial — skipping `/check`".

### 3. Spawn the agents in parallel

Send a single message with one Agent call per agent in `.claude/agents/engineering/`:

- `code-reviewer`: "Review the working diff against this repo's hard rules. Output the strict format from your spec."
- `test-gap-checker`: "Audit the working diff for missing unittest coverage per the CLAUDE.md tests-and-docs rule. Output the format from your spec."
- `doc-hygiene-checker`: "Audit the working diff against the CLAUDE.md doc-update rule. Output which docs need updating."

If the change is already committed on a branch, say so in every prompt ("the diff is `git diff origin/main...HEAD`"), or the agents see an empty working diff.

**Per-lane scratchpad.** Every lane inherits one scratchpad path. Name each lane's own `<scratchpad>/<lane-slug>/` in its prompt and tell it to keep every temporary file there. A lane that changes a file to test something restores it with `git checkout HEAD -- <path>`, never a `.bak` copy.

### 4. Aggregate

```
## /check report

**Change:** <one-sentence summary>

### Code review (`code-reviewer`)
Status: <CLEAN | NEEDS_CHANGES>
<verbatim findings, or "no concrete findings">

### Test gaps (`test-gap-checker`)
<verbatim verdicts, or "test surface is consistent">

### Doc gaps (`doc-hygiene-checker`)
<verbatim verdicts, or "doc set is clean">

### Recommendation
<one of:>
- Every agent came back clean; ready to commit.
- Code review and docs are clean; <N> test gap(s). Land the tests now or record a follow-up.
- <N> code-review finding(s); apply or push back before committing.
- Gaps across review, tests and docs; list them and let the user pick.
```

### 5. Hand off

Ask the user how to proceed. Don't apply fixes automatically, and don't commit.

## Tone

The user sees a one-line "Running review, test-gap and doc checks...", the aggregated report, and a short question at the end. Don't narrate the fan-out.
