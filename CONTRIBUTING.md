# Contributing

Thanks for helping. media-intake handles people's private photos, so the bar
is: nothing leaves the machine, nothing is deleted without an explicit yes,
and every change comes with tests.

## Workflow

- Every change reaches `main` through a pull request; `main` is protected and
  its required check is `CI gate` (`.github/workflows/ci.yml`). PRs are
  squash-merged.
- Branch from `origin/main` in a worktree:
  `git fetch origin && git worktree add ../media-intake-<slug> -b <type>/<slug> origin/main`.
- PR titles are conventional commits: `<type>(<scope>): <subject>`, type one
  of feat, fix, chore, docs, refactor, test, perf, ci, build, revert; subject
  lowercase, no trailing period (`pr-title-lint` checks it).
- After the first push, update a branch by merging `origin/main`; never rebase
  and force-push a shared branch.
- Never `git stash` (one stack shared by every worktree); park work as a patch.
- `pre-commit install` once: gitleaks scans every commit.

## Code

- Python standard library first. The system Python (3.11+) runs the CLI; the
  venv from `setup.sh` runs only the ML pieces (NudeNet, SigLIP, scipy).
- A new download is pinned by sha256 in `setup.sh`; a new Python dependency
  goes in `requirements.txt` with `--hash` lines, a version at least 7 days old.
- No network calls at run time except to `127.0.0.1` (Ollama). No telemetry.
- Originals are never written: tags go in XMP sidecars.
- An assistant or tool must never read, view or describe a quarantined or
  flagged file; keep output to counts and paths.

## Tests

- Every change updates its tests and docs in the same PR.
- Tests use synthetic media only (Pillow drawings, ffmpeg test patterns) in a
  throwaway HOME. Never commit a real photo, path, device id or batch name.
- Run what you touched: `python3 -I -m unittest discover -s tests -p "test_<area>.py"`; the full
  suite is `python3 -I -m unittest discover -s tests`. Tests that need the
  venv, the models or Ollama skip themselves when those are missing.
- `tests/test_offline_guard.py` enforces "nothing leaves the machine" from the
  source: network clients only in `vlm_trash.py`, HTTP servers bound to
  `127.0.0.1`, no runtime downloaders. A new legitimate use changes its
  allow-list in the same PR, so it gets reviewed.
- When a test fails, fix the test if it is wrong, or the code if it is; never
  loosen an assertion or add a sleep to make it pass.
