---
description: Find behaviour that works but isn't tested and backfill unittest coverage for it; no bug required. The area-scoped counterpart to the diff-scoped test-gap-checker. Commits path-scoped on a PR branch; never pushes main.
argument-hint: "[optional scope: a module or area, e.g. media_intake/drive_queue.py, 'sidecar writes'; omit to pick an under-covered area]"
---

Find behaviour that *works but isn't tested* and lock it in. Unlike `/bug-hunt` (deliverable: a fix) and the `test-gap-checker` agent (reviews a diff), `/coverage-hunt` hardens an **area** of landed code. If you trip over a real bug, fix it and test the corrected behaviour, but the goal is durable coverage.

`$ARGUMENTS` is an optional scope. If empty, pick an under-covered area (step 1).

## Operating rules (non-negotiable; `CLAUDE.md`)

- **Synthetic and hermetic.** Generate fixtures in the test (Pillow drawings, ffmpeg test patterns); never commit or read a real photo. Run in a throwaway HOME with `MEDIA_INTAKE_STATE` and `PHOTO_LIBRARY` in a temp dir, and import `tests/support.py`. Never open anything under a `quarantine/`.
- **Test behaviour, not implementation.** Assert the observable contract: files present or absent, sha256 unchanged, sidecar contents, exit codes, counts and paths in the output.
- **It must be able to fail.** Revert the behaviour briefly (or reason it through explicitly); a test that stays green isn't testing anything. Cover the edges, not only the happy path.
- **No masking.** No sleeps, longer timeouts, retries or loosened assertions. If a deterministic wait needs a real signal (a status file, a readiness flag), add it to the code.
- **Optional dependencies skip cleanly.** A test needing the venv, the models, exiftool, ffmpeg or Ollama uses `skipUnless` with a reason, so CI still runs the rest. Ollama is faked, never called for real.
- **Commit path-scoped on a PR branch** in a worktree cut from `origin/main` (`git worktree add ../media-intake-<slug> -b test/<slug> origin/main`). `test(<area>): ...` commits, `git commit -m "..." -- <paths>`, no AI-attribution trailer. Push the branch and open a PR; never push `main`.

## Procedure

### 1. Find the gap

- **If `$ARGUMENTS` is given:** that's the area.
- **If empty:** map each module in `media_intake/` to its `tests/test_<area>.py` and rank by logic density against thin coverage, favouring delete paths, sidecar writes, dedupe, dates and device parsing. State the pick and why in one line.
- **Enumerate the untested behaviours** in the area: each branch, each error path, each refusal, each promise in `CLAUDE.md` § Hard rules and in the area's `docs/` page. That list is the work.

The promises worth a test each, wherever they're reachable: a delete refuses without `--yes` or a verified library copy; an original's sha256 is unchanged after tagging, rotating or releasing; the state dir and quarantine are `0700`; the review page and gallery bind `127.0.0.1` and refuse a request without the token; nothing connects to a host other than `127.0.0.1`.

### 2. Confirm current behaviour

Run the code on synthetic input so the test encodes what it does today. If today's behaviour is wrong, that's a `/bug-hunt` finding: fix it and test the fix.

### 3. Write the tests

One test per behaviour, named so a failure reads as a sentence about what broke. Include the refusals and failure paths: missing input, duplicate, interrupted run, re-run.

### 4. Verify

Run the new tests and the area's file: `python3 -I -m unittest discover -s tests -p "test_<area>.py"`. Prove each can fail. Report counts faithfully. The full suite is CI's job.

### 5. Commit

`test(<area>): ...` commits, path-scoped. A real code affordance added for determinism is a separate non-test commit with its doc update.

## Report

```
## /coverage-hunt: <area>

**Picked:** <area> and why (omit if named)

**Behaviours now covered:** <bullets>

**Tests added:** <files and count>

**Bug found while testing:** <what and the fix, or "none">

**Verification:** <new tests N/N; can-fail check done; area tests N/N>

**Commits:** <sha and subject>

**Still uncovered:** <what was left and why, or "area is well covered">
```

## Tone

The deliverable is honest coverage, not a vanity green. If you couldn't make a test able to fail, say why and don't ship it.
