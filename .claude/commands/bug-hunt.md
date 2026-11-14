---
description: Hunt for real correctness bugs across the repo. Reproduce each with a probe, fix at the root, lock it with a regression test, then sweep sibling paths. Commits path-scoped on a PR branch; never pushes main.
argument-hint: "[optional scope: a module, subcommand or area, e.g. media_intake/sources.py, 'the drive queue', 'erase'; omit to pick high-yield targets]"
---

Hunt for genuine correctness bugs and land the fixes. `/bug-hunt` ranks high-yield targets, finds bugs in each, **proves them with a runnable probe before believing them**, fixes the root cause, ships a regression test that fails on the old code, and sweeps the sibling paths that share the pattern. For behaviour that works but isn't tested, use `/coverage-hunt` instead.

`$ARGUMENTS` is an optional scope. If empty, pick targets (step 1).

## Operating rules (non-negotiable; `CLAUDE.md` § Hard rules)

- **Never open user media.** Probes and tests use synthetic files only (Pillow drawings, ffmpeg test patterns) in a throwaway HOME, with `MEDIA_INTAKE_STATE` and `PHOTO_LIBRARY` in a temp dir and `tests/support.py` imported for a hermetic config. Never point a probe at a real library, state dir or device, and never read anything under a `quarantine/`.
- **Prove it before you believe it.** A bug isn't real until a probe fails on the current code. A hypothesis you can't reproduce is not a finding. Delete throwaway probes before committing.
- **Fix the root cause; never mask.** No sleeps, longer timeouts, retries, loosened assertions or swallowed exceptions. If you can't fix it now, record a tracked follow-up.
- **Be honest when there's no bug.** If a target is sound, say so; the deliverable is then the coverage you added, never an invented fix.
- **Load-bearing paths go through `/safe-edit`**: deletes and their `--yes` guards, writes near originals, the network boundary, the local servers, detectors and quarantine.
- **Docs ride with the change.** A behaviour, command or option change updates its doc in the same commit.
- **Commit path-scoped on a PR branch** in a worktree cut from `origin/main` (`git worktree add ../media-intake-<slug> -b fix/<slug> origin/main`). `git commit -m "..." -- <paths>`; no AI-attribution trailer. Push the branch and open a PR; never push `main`.

## What counts as a bug

Both must hold:
1. The behaviour is **genuinely wrong**: not by design, not a documented trade-off (`ROADMAP.md` § Known limitations), not a style preference.
2. The **existing tests don't already cover it**.

Reject missing features (they go in `ROADMAP.md`), "consider refactoring", and speculative "could fail if...". Rank survivors by confidence times impact.

## Where bugs tend to live here

- **Delete and copy safety.** A delete that can reach a file with no verified library copy; a case-only path collision on Android's case-insensitive storage; a symlink followed out of the source; a partial copy treated as complete after an unplug.
- **Idempotency.** A re-run of stage, apply, tag or a queue chunk that duplicates, renames to `_1`, loses a file or re-flags a released one.
- **Paths that should agree.** Two places computing "the same" thing differently (sha256 vs near-duplicate matching, the review page vs the CLI for the same action, the gallery vs `erase`). Find the canonical one and diff the others against it.
- **Dates.** EXIF vs QuickTime UTC vs mtime, `OffsetTimeOriginal`, implausible camera clocks, midnight boundaries under a skewed `TZ`.
- **Parsing device and metadata output.** exiftool JSON with missing fields, filenames with spaces, unicode or separators, empty directories, `DCIM/` layouts that differ per device.
- **Gates that fail open.** A detector or check that treats a missing tool or an error as "clean" instead of flagging or refusing.
- **State and resumability.** A drive-queue chunk or a long page action that leaves state inconsistent when interrupted.

## Procedure

### 1. Pick targets

- **If `$ARGUMENTS` is given:** resolve it to files and hunt within.
- **If empty:** rank modules in `media_intake/` by logic density, thin coverage in `tests/`, and recent churn (`git log --oneline -20 -- <file>`). Prefer delete paths, dedupe, date logic and parsers. State each pick and why in one line.

### 2. Map before judging

Read the target's contract: its callers, the `docs/` page that describes it, the tests that pin it. For a wide scope, fan out read-only `general-purpose` hunters in one message, each on a few files, told to report concrete logic bugs as `file:line`, failing input, wrong output, expected output and confidence, that a bug is only real if the tests don't cover it, and that they must never open a media file. Read the code yourself in parallel; hunters are the workhorse, not the judge.

**Per-lane scratchpad.** Every lane inherits one scratchpad path. Name each lane's own `<scratchpad>/<lane-slug>/` and keep its temp files there. A lane that changes a file restores it with `git checkout HEAD -- <path>`, never a `.bak` copy.

### 3. Reproduce

For each candidate, read the cited code yourself, confirm the bug is reachable and untested, and compute any numeric claim. Then write a probe that fails on the current code: a throwaway `unittest` case or script on synthetic input (name it `_probe_*` so it's easy to delete). No probe, no finding.

### 4. Fix at the root

If the fix touches a load-bearing path or needs a product decision, route it through `/safe-edit` or ask the user. Otherwise apply the durable fix, matched to the surrounding style and tightly scoped.

### 5. Lock it with a regression test

Promote the probe into `tests/test_<area>.py`. It must fail on the old code and pass on the fix. Pin the class, not one input: the boundary on each side, empty, the case that should be excluded. Every bug found on the way, incidental ones included, gets a test or a tracked follow-up.

### 6. Sweep the siblings

Grep for the same shape elsewhere (the other delete paths, the other parsers, the other device sources) and fix and test them too, or state why each is already correct.

### 7. Verify

Run the new tests and the nearby area tests: `python3 -I -m unittest discover -s tests -p "test_<area>.py"`. Report pass and fail counts faithfully. The full suite is CI's job. For a load-bearing diff, run the `code-reviewer` agent before committing.

### 8. Commit

Fix and test as path-scoped commits, conventional messages. Then the next target, until the scope is covered. Push the branch and open a PR (one per round, or per bug when they're unrelated).

## Report

```
## /bug-hunt: <scope or "self-selected">

**Targets:** <each pick and why>

**Bugs found and fixed:**
- <file:line>: <what was wrong> -> <root-cause fix> | repro: <how> | test: <file>
- (or "none; targets were sound, coverage added where thin")

**Sibling sweep:** <paths checked: fixed too, or correct and why>

**Verification:** <new tests N/N; nearby area tests N/N; review verdict if run>

**Commits:** <sha and subject, one per line>

**Dismissed:** <low-confidence or by-design findings, one line each>

**Deferred:** <tracked follow-ups with the durable fix named, or "nothing outstanding">

**PR:** <url>
```

## Tone

Lead with the verdict. State a real bug plainly with its repro; if a target was sound, say so.
