---
description: Implement a non-trivial change with a code-reviewer loop (coder, review, fix, review, ready to commit). Costs about 2-3x a normal edit; use it on load-bearing paths.
argument-hint: <task description>
---

Implement the task `$ARGUMENTS` with the `code-reviewer` agent in the loop.

## When to use this command

**Right fit** (the load-bearing paths in this repo):
- **Delete paths**: erase, forget, trash, purge-review, offload, queue offload, wipe-free (`media_intake/intake.py`, `media_intake/drive_queue.py`), and the `--yes` and verified-copy checks that guard them.
- **Writes near originals**: tagging, rotate, date correction, release, share-copy (`media_intake/intake.py`, `label.py`, `geocode.py`, `gallery.py`). Originals must stay byte-identical; only sidecars change.
- **The network boundary**: anything that opens a socket or a URL (`media_intake/vlm_trash.py` talks to Ollama on `127.0.0.1`), and `setup.sh` downloads and their sha256 pins.
- **The local servers**: `media_intake/review_ui.py`, `media_intake/gallery.py` (bind address, token check, request handling).
- **Detectors and quarantine**: `media_intake/scan_sensitive.py`, the quarantine move, the state dir's `0700` mode, and anything that decides what gets flagged or released.
- **Device and source handling**: `media_intake/sources.py` (MTP, gphoto2, mass storage, case-insensitive storage).
- Anything you want a second pair of eyes on before commit.

**Wrong fit, refuse and edit directly:**
- Typos, one-line doc corrections, comment edits.
- Pin bumps that don't change source.
- A diff under about 10 lines that touches none of the paths above.

The loop costs real tokens and time. If the task is trivial, stop and tell the user to edit it normally.

## The loop

1. **Coder pass.** Implement the task with its tests and docs. Run the targeted tests (`python3 -I -m unittest discover -s tests -p "test_<area>.py"`). Do NOT commit yet.

2. **Round 1 review.** Spawn `code-reviewer` with:
   > "Review the working diff against this repo's hard rules. The task being implemented is: `$ARGUMENTS`. Output the strict format from your spec."

   - `Status: CLEAN`: go to step 5.
   - `Status: NEEDS_CHANGES`: go to step 3.

3. **Apply fixes.** For each Critical or Improvement finding:
   - If it's correct, apply it.
   - If it's wrong (a misread, missing context, a rule that doesn't apply), say explicitly why you're not applying it. Don't skip silently.
   - If it's borderline, apply it; the reviewer retracts on the next round if it was wrong.

4. **Round 2 review.** Spawn `code-reviewer` again with the same prompt, quoting any Round 1 finding you declined and why.
   - `CLEAN`, or the reviewer retracts: go to step 5.
   - `NEEDS_CHANGES` again: **stop**. Show the user the remaining findings and what you tried. No Round 3.

5. **Ready-to-commit handoff.** Tell the user what changed (one line), which round came back clean, and any Notes worth knowing. Ask whether to commit, unless the task already asked for a PR.

6. **On "yes":** commit path-scoped on a PR branch in its own worktree cut from `origin/main` (`git commit -m "..." -- <paths>`; never `git add -A`), conventional-commit message, no `Co-Authored-By` or generated-by trailer. Push the branch and open a PR; never push `main`, and don't merge your own PR unless asked.

## Loop-termination guarantees

- Hard cap: two review rounds.
- The reviewer can't re-cycle on vague concerns; `NEEDS_CHANGES` requires concrete file:line changes. Treat vague findings as `CLEAN` and pass them on as comments.
- Out-of-scope observations never trigger a cycle.

## What this command does NOT replace

- `/check`, the lighter single-pass gate for everyday changes.
- The targeted tests for the area you changed. The full suite is CI's job.

## Tone

One sentence per significant coder step, then "Round 1 review found N findings; applied them" or "came back clean", then the handoff. If the loop ends without consensus, summarise what's contested and ask for the user's call.
