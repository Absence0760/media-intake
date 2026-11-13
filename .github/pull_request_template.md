<!-- Title: a conventional commit — `type(scope): subject`, lowercase subject,
     no trailing period (pr-title-lint enforces it; it becomes the squash
     commit). A draft PR or one with merge conflicts runs no ci.yml, so a
     missing or red `CI gate` there is expected — mark it ready / merge
     origin/main in, then confirm CI actually started. -->

## Summary

<!-- 1–3 sentences on what this PR does and why. End with "Closes #<n>" when it finishes an issue. -->

## Changes

<!-- Bulleted list of the user-visible or developer-visible changes. -->

-
-

## Surface touched

- [ ] Intake, erase or trash (anything that moves or deletes files)
- [ ] Detectors, labels, search or the VLM check
- [ ] Review page or gallery (local web server)
- [ ] setup.sh, requirements or download pins
- [ ] CI / GitHub Actions (`.github/`)
- [ ] Docs only

## Privacy and safety checklist

- [ ] No new network call (Ollama on 127.0.0.1 is the only run-time host); downloads only in setup.sh, sha256-pinned
- [ ] No new delete path without `--yes` and the existing source/drive checks
- [ ] No output that prints media content, OCR text or a flagged file's name beyond counts and paths
- [ ] Originals still never written (sidecars only)
- [ ] No real photo, path, device id, name or batch data in any committed file (fixtures are synthetic)

## Docs

- [ ] Docs that describe the changed behaviour are updated in this PR (or: none do)

## Test plan

<!-- The targeted commands you ran for the area you changed. CI runs the full suite. -->

- [ ] Targeted tests for the changed area pass locally (commands below)
- [ ] Manual walkthrough on the affected surface (describe below)
- [ ] Screenshots attached for UI changes

<!-- Commands run / walkthrough notes: -->
