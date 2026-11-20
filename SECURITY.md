# Security policy

media-intake handles private photos and videos, so reports about anything
that could leak media, metadata or locations, delete the wrong file, or let a
web page or another local user reach the review page or gallery are welcome.

## Reporting a vulnerability

Please **do not** open a public issue. Report it privately through GitHub's
private vulnerability reporting: the repository's **Security** tab, then
**Report a vulnerability**
(<https://github.com/jaredhoward-dev/media-intake/security/advisories/new>).
Only the maintainers see the report, and the fix is coordinated in that draft
advisory.

Include what you found, its impact, steps to reproduce (synthetic media only,
please: never send real photos), and the commit SHA you tested.

## What to expect

| Step | Target |
| --- | --- |
| Acknowledgement | 3 business days |
| Triage and first response | 7 days |
| Fix or disclosure plan (High / Critical) | 30 days |
| Fix (Medium / Low) | next release |

Disclosure is coordinated: the advisory is published once a fix is out.
Reporters are credited unless they ask not to be.

## Supported versions

Only the latest commit on `main` (and the latest tag, once there are tags).

## Scope

In scope: everything in this repository, including `setup.sh` and the
download pins; the gallery's local-app launch (`/api/open`, which runs the
desktop's default app on one library file); and the state folder's lock and
salt files (`locks/`, whose holder note must never name a file, and
`device-salt`, which keeps device ids from revealing camera serial numbers);
the people and pets recognition store (`recognition.dir`: biometric data, `0700` with
`0600` files, never backed up, removed by erase, forget, trash and
sweep-erased; docs/recognition.md); `places.json` (your named places and their
points: sensitive, where you live; `0600`, never backed up, synced or printed;
docs/places.md); and `datecheck.json` and `date-fixes.json` (paths and dates,
`0600`, a file's records dropped by erase, forget, trash and sweep-erased;
docs/dates.md); `refiles.json` (`0600`: each `refile` run's old and new paths,
for `refile --undo`; erase, forget, trash and sweep-erased drop a file's whole
move history, and `verify-gone` counts any left; docs/dates.md);
`vlm-progress.json` and `captions-progress.json` (`0600`: path, size, mtime
and model digest, and vlm-trash's verdict, never a caption; a file's entries
dropped by erase, forget, trash and sweep-erased and counted by `verify-gone`;
docs/search.md); captions in sidecars (`XMP-mediaintake:Caption` and
`CaptionModel`: a one-sentence description of the photo, written only by
`media-intake captions` from the Ollama model on `127.0.0.1`, and removed with
the sidecar by erase, forget and trash; docs/search.md); `ocr-text.db` (the text read from photos flagged for nothing,
`0600`, rows dropped by erase, forget, trash and sweep-erased and checked by
`verify-gone`; docs/search.md); `records-handoff.json` (`0600`: names, checksums and times of
photos of documents handed to the records inbox, never content) and
`quarantine/records-held/` (held ID, passport and payment-card photos, until
`to-records --finish`); the records inbox itself, which `to-records` only
writes (`0600` copies, checked in a `0700` staging folder beside it, then
renamed in whole, never over a file) and no sweep ever enters, nor any
`records.protect` folder (docs/records.md);
and file names from devices reaching an external tool's command line or a
GStreamer pipeline description (video frames and HEVC proxies are read through
a link in a private work folder, never by name).
Out of scope: bugs in dependencies alone (report upstream),
detector misses (NudeNet, OCR and the label model are hints, not
guarantees; see README), and attacks that need a compromised account on the
same machine.

## Defensive scaffolding

A TruffleHog secret scan on every push and PR and over the whole history
weekly (`.github/workflows/secret-scan.yml`; a verified or unverifiable
credential fails it), gitleaks in pre-commit, CodeQL for Python and GitHub
Actions, OpenSSF Scorecard, zizmor and osv-scanner (advisory), and grouped
Dependabot updates with a 7-day cooldown. Actions are pinned by SHA.
