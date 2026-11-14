---
description: Fix a failing GitHub Actions run. Root-causes the failure, fixes it without retry, timeout or skip band-aids, reproduces it locally with the command CI ran, and lands coverage so it can't return silently.
argument-hint: "<GitHub Actions run URL or run ID> [optional: which job]"
---

Fix the failing CI run `$ARGUMENTS`. Find the real cause, fix it at the root, add coverage, and land it through a PR.

## The two hard rules

1. **Don't code around the failure.** A red test that catches a real defect is doing its job. Forbidden unless you can prove pure infrastructure noise *and* name the structural reason:
   - a longer timeout, a retry, a `sleep`;
   - `skip` / `skipUnless` on a real failure, `continue-on-error`;
   - a loosened assertion or a deleted case;
   - re-running until green.
2. **Report what actually failed, and leave coverage behind**: a test for a code defect, an explicit check for a missing precondition. Coverage ships in the same commit as the fix.

## Procedure

### 1. Pull the failure apart

- `gh run view <id>` for which job and step failed. The workflows are in `.github/workflows/`; `ci.yml` holds the unit tests and the `CI gate` aggregator, the rest are PR hygiene (title lint, labeler, mergeability), secret scanning, security and supply-chain checks.
- `gh run view <id> --log-failed` and grep to the **first** error, not the final `exit 1`: a traceback, a `FAIL:` / `ERROR:` line from `unittest`, a non-zero command.
- Quote the job, step and error line back to the user.

### 2. Classify it honestly

- **Genuine defect**: the code is wrong. Fix it; pin it with a test.
- **Test bug**: the test asserts the wrong thing, races in its own setup, or leaks out of its throwaway HOME. Fix the test properly, not by loosening it.
- **Environment difference**: CI runs Ubuntu 24.04 with apt's `python3-pil`, `python3-yaml`, `python3-numpy`, exiftool and ffmpeg, no venv, no models and no Ollama, under `TZ=America/New_York`. A test that assumes a Fedora-only tool, a newer Python, a local model or the local timezone is a test bug: it should skip cleanly on a missing optional dependency or set what it needs.
- **Infra flake**: make the step deterministic or remove the fragile dependency; don't retry it.

### 3. Read the context before changing it

Workflow files carry comments explaining why a step has its shape. Read them, and update any comment your fix makes obsolete.

### 4. Reproduce locally, then verify the fix

Run the command CI ran, narrowed to the failing area: `python3 -I -m unittest discover -s tests -p "test_<area>.py" -v`, with `TZ=America/New_York` if dates are involved. If CI's condition differs from this machine (no venv, no models), reproduce that condition rather than assuming. Confirm the failure reproduces before the fix and is gone after; report the command and its result. Don't run the full suite locally; CI does.

For a workflow change, validate the YAML: `python3 -c "import yaml, sys; yaml.safe_load(open(sys.argv[1]))" <workflow>`.

### 5. Fix at the lowest sensible layer

If the same broken pattern appears in sibling tests or jobs, fix them too. Keep the blast radius proportional. Never weaken a safety test (deletes behind `--yes`, sidecar-only writes, the token and `127.0.0.1` checks) to get green; fix the code.

### 6. Sweep docs

If a doc describes what you changed (a CI step, a command, a test convention in `CONTRIBUTING.md`), update it in the same commit.

### 7. Commit on a PR branch

- One coherent piece, one path-scoped commit with fix, coverage and doc together: `git commit -m "..." -- <paths>`. No AI-attribution trailer.
- If the red run belongs to an open PR, fix it on that PR's branch in its worktree; if the branch is behind, merge `origin/main` into it (never rebase and force-push). If the red run is on `main`, cut a fresh worktree from `origin/main` and open a new PR. Merging is the maintainer's call.
- A draft or conflicting PR runs no `ci.yml`, so a missing `CI gate` there is expected.
- Consider a `code-reviewer` pass before handing back if the fix touched a load-bearing path.

## Output

The failing job, step and root cause (one or two sentences); the fix and why it isn't a band-aid; the coverage added; the local command and its result; any residual risk.
