# CLAUDE.md

Guidance for Claude Code (and other coding assistants) in this repository.
Rules only; reference material lives in `README.md`, `docs/` and `CONTRIBUTING.md`.

## What this is

media-intake: a local-only photo and video intake tool (Python, Fedora first).
Package `media_intake/` (flat sibling modules, each runnable as a script),
entry point `media-intake` (`media_intake/cli.py`), tests in `tests/`,
setup in `setup.sh`. Work list: `ROADMAP.md`.

## Hard rules

- **Never open user media that the tool flags.** Don't Read, view, montage or
  describe any file under the state dir's `quarantine/` or one `scan.json`
  flags. Command output you rely on is counts and paths only.
- **Nothing leaves the machine.** No network at run time except Ollama on
  `127.0.0.1`; downloads happen only in `setup.sh`, each pinned by sha256.
  No telemetry, no cloud APIs, no remote models.
- **No deletion without an explicit yes.** Device and drive deletes stay
  behind `--yes` and the checks that already guard them; never weaken those.
- **Originals are never written.** Tags go to `<file>.<ext>.xmp` sidecars.
- **Generic code only.** No personal paths, names, device ids, project names
  or real batch data in code, tests, docs or commits. Project-specific
  behaviour goes in config (`docs/configuration.md`) or a plugin
  (`docs/plugins.md`).
- **Tests are synthetic** and run in a throwaway HOME; `tests/support.py`
  makes the config hermetic. Never point a test at a real library.
- **Secrets never live in this repo**; the tool needs none.
- **No AI attribution** (`Co-Authored-By`, "Generated with ...") in commits or PR bodies.

## Git and PRs

`.claude/hooks/git-scope-guard.py` enforces the path-scoped commit rules; the
workflow is in `CONTRIBUTING.md`. In short: PRs to `main` only, one worktree
per branch cut from `origin/main`, conventional PR titles, commit with
explicit paths (`git commit -m "..." -- <paths>`), no `git add -A`, no stash,
no force-push.

## Tests and docs

- Every code change updates its tests and docs in the same change.
- Run the tests for what you changed: `python3 -I -m unittest discover -s tests -p "test_<area>.py"`.
  The full suite (`python3 -I -m unittest discover -s tests`) is CI's job.
- Fix bugs at the source; never inflate a timeout, add a sleep, loosen an
  assertion or skip a failing test to get green.
