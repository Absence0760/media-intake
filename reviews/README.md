# reviews/

Output folder for review and audit reports (`reviews/<name>.md`); findings belong here,
not in `docs/` — docs describe how things *are*, not a list of open bugs.

**Everything in here except this README is git-ignored.** The reports are
per-clone working notes, not a committed artifact — they name `file:line`
locations that rot the moment code lands (a reorg alone invalidates every
path), so they're regenerated, not version-controlled. Keeping them out of git
means a stale report can never masquerade as current truth; the record that
matters is the fix and its test, in the commit that closed the finding.

## If you open a report to act on it

These are **living documents**. A finding is only as good as the commit it was
verified against (see the header stamp in each file). Before you act on, cite,
or hand off any finding:

1. Open the `file:line` it points at and confirm it still reproduces at HEAD.
2. If it's already fixed, move it to `## Resolved` with the fixing commit.
3. If you changed code that a report covers, re-run that review so the report
   reconciles instead of drifting.
