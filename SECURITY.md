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
the people recognition store (`recognition.dir`: biometric data, `0700` with
`0600` files, never backed up, removed by erase, forget, trash and
sweep-erased; docs/recognition.md);
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
