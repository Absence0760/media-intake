#!/usr/bin/env python3
"""Tests for git-scope-guard.py.

Runs the hook as a subprocess with crafted PreToolUse payloads and asserts
the deny/allow decision. Run: `python3 .claude/hooks/git-scope-guard.test.py`.
Not wired into CI — the hook itself is the live guard; this pins its logic so
an edit can't silently reopen a hole (notably the bare-`git commit` sweep that
let one session's commit capture another's staged changes).
"""

import json
import subprocess
import sys
from pathlib import Path

HOOK = str(Path(__file__).with_name("git-scope-guard.py"))


def decision(command):
    """Return 'deny' if the hook blocks the command, else 'allow'."""
    out = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True,
    ).stdout.strip()
    if not out:
        return "allow"
    try:
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    except (ValueError, KeyError):
        return "allow"


# (command, expected) — the core of the suite is the bare-commit gap.
CASES = [
    # The regression that motivated this guard: bare commit sweeps the index.
    ('git commit -m "msg"', "deny"),
    ("git commit", "deny"),
    ('git commit -m "msg" --no-verify', "deny"),
    # Scoped commits are safe and must pass.
    ('git commit -m "msg" -- foo.ts bar.ts', "allow"),
    ('git commit foo.ts -m "msg"', "allow"),
    ('git commit -m "msg" -- src/lib/types.ts', "allow"),
    # Scoped / no-content commits that don't race-snapshot the index.
    ('git commit --amend -m "reword" -- foo.ts', "allow"),  # amend + pathspec
    ('git commit --allow-empty -m "ci: trigger"', "allow"),
    ("git commit --no-edit", "allow"),  # merge / cherry-pick continuation
    # (bare `git commit --amend` is git-state-dependent — tested white-box below)
    # Pre-existing rules still hold.
    ('git commit -am "msg"', "deny"),
    ('git commit -a -m "msg"', "deny"),
    ("git add -u", "deny"),
    ("git add .", "deny"),
    ("git add -A", "deny"),
    ("git add foo.ts bar.ts", "allow"),
    ("git reset --hard", "deny"),
    ("git checkout -- .", "deny"),
    ("git restore .", "deny"),
    ("git restore -- foo.ts", "allow"),
    # Stash is banned outright: refs/stash is one stack shared by every
    # worktree, so a path limits what goes in but not who pops it out.
    ("git stash", "deny"),
    ("git stash push -- foo.ts", "deny"),
    ("git stash save wip", "deny"),
    ("git stash pop", "deny"),
    ("git stash apply stash@{0}", "deny"),
    ("git stash drop", "deny"),
    ("git stash clear", "deny"),
    ("git stash list", "allow"),
    ("git stash show -p stash@{0}", "allow"),
    ("git rm -r --cached .", "deny"),
    ("git rm .", "deny"),
    ("git rm src/old.ts", "allow"),
    ("git rm -r src/legacy", "allow"),
    ("git rm --cached src/old.ts", "allow"),
    ("git clean -fd", "deny"),
    # Read-only / unrelated — never blocked.
    ("git status", "allow"),
    ("git log --oneline -5", "allow"),
    ("git diff --cached", "allow"),
    # Compound commands: a deny anywhere in the chain blocks.
    ('git add foo.ts && git commit -m "msg"', "deny"),
    ('git add foo.ts && git commit -m "msg" -- foo.ts', "allow"),
    # A heredoc BODY is data, not shell. Its newlines used to become `;`
    # separators, so a commit message that merely *mentioned* a blocked
    # command was parsed as that command and denied.
    ("git commit -F - -- a.py <<'EOF'\nfix: x\n\nstop using git add -A\nEOF", "allow"),
    ("git commit -F - -- a.py <<'EOF'\nfix: x\n\nrun git commit -a to repro\nEOF", "allow"),
    ("git commit -F - -- a.py <<EOF\nfix: x\ngit reset --hard\nEOF", "allow"),
    ("git commit -F - -- a.py <<-EOF\n\tfix: x\n\tgit clean -f\n\tEOF", "allow"),
    # ...but the body must not become a hiding place: everything after the
    # terminator is shell again, and the lookalikes that are NOT heredocs (a
    # `<<<` herestring, a quoted string containing `<<EOF`) open nothing.
    ("git commit -F - -- a.py <<'EOF'\nmsg\nEOF\ngit add -A", "deny"),
    ("git commit -F - -- a.py <<'EOF'\nmsg\nEOF\ngit reset --hard", "deny"),
    ('git add -A <<<"x"', "deny"),
    ('echo "<<EOF" ; git add -A', "deny"),
    # main only changes through a PR: branches push, main never does.
    ("git push -u origin feat/thing", "allow"),
    ("git push origin fix/x:fix/x", "allow"),
    ("git push origin --delete feat/old", "allow"),
    ("git -C ../wt push -u origin feat/thing", "allow"),
    ("git push origin main", "deny"),
    ("git push origin master", "deny"),
    ("git push origin HEAD:main", "deny"),
    ("git push origin refs/heads/main", "deny"),
    ("git push", "deny"),
    ("git push origin", "deny"),
    ("git push origin HEAD", "deny"),
    ("git push --all origin", "deny"),
    ("git push --mirror", "deny"),
    ("cd x && git push origin main", "deny"),
    # A pushed branch is updated by merging origin/main, never rewritten.
    ("git push --force origin feat/thing", "deny"),
    ("git push -f origin feat/thing", "deny"),
    ("git push origin feat/thing --force", "deny"),
    ("git push -uf origin feat/thing", "deny"),
    ("git push --force-with-lease origin feat/thing", "deny"),
    ("git push --force-with-lease=feat/thing:abc origin feat/thing", "deny"),
    ("git push origin +feat/thing", "deny"),
    ("git push origin +feat:main", "deny"),
    # PR titles must pass CI's pr-title-lint (conventional, lowercase subject).
    ('gh pr create --title "feat(ui): add the leave guard" --body x', "allow"),
    ('gh pr create -t "fix: stop the 404"', "allow"),
    ('gh pr edit 5 --title="docs(api): note the null report"', "allow"),
    ('gh pr create --title "feat!: drop the v1 api"', "allow"),
    ('gh pr create --title "UI feedback: help pages"', "deny"),
    ('gh pr create --title "web: fix the header"', "deny"),
    ('gh pr create --title "style: reformat"', "deny"),
    ('gh pr create --title "feat(ui): Help pages"', "deny"),
    ('gh pr create --title "feat: add a thing."', "deny"),
    ('gh pr edit 167 --title "Add the leave guard"', "deny"),
    ('cd x && gh pr create --title "wip"', "deny"),
    ("gh pr create --fill", "allow"),
    ("gh pr view 5", "allow"),
]

failures = []
for command, expected in CASES:
    got = decision(command)
    status = "ok" if got == expected else "FAIL"
    if got != expected:
        failures.append((command, expected, got))
    print(f"  [{status}] expect {expected:5} got {got:5}  {command}")

# White-box: bare `git commit --amend` is git-state-dependent (deny only when
# the index has staged changes). Load the module and stub the git query so both
# branches are deterministic without touching the real shared index.
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("gsg", HOOK)
gsg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gsg)

for staged, args, want_deny in [
    (True, ["--amend", "-m", "reword"], True),    # staged → absorbs the index
    (True, ["--amend", "--no-edit"], True),
    (False, ["--amend", "-m", "reword"], False),  # nothing staged → pure reword
    (False, ["--amend", "--no-edit"], False),
    (True, ["--amend", "-m", "x", "--", "foo.ts"], False),  # scoped → safe even if staged
]:
    gsg._has_staged_changes = lambda s=staged: s
    reason = gsg._check_commit(args)
    got_deny = reason is not None
    ok = got_deny == want_deny
    if not ok:
        failures.append((f"_check_commit({args}) staged={staged}", want_deny, got_deny))
    print(f"  [{'ok' if ok else 'FAIL'}] amend staged={str(staged):5} "
          f"expect {'deny' if want_deny else 'allow':5} got {'deny' if got_deny else 'allow':5}  {args}")

# The hook's title rule must stay the workflow's: same types, same subject
# pattern. Skipped when the project has removed pr-title-lint.yml.
wf_path = Path(__file__).resolve().parents[2] / ".github/workflows/pr-title-lint.yml"
if wf_path.exists():
    import re as _re  # noqa: E402
    wf = wf_path.read_text()
    m_types = _re.search(r"types: \|\n((?:[ \t]+\w+\n)+)", wf)
    m_subject = _re.search(r"subjectPattern: (.+)", wf)
    for what, hook_v, wf_v in [
        ("types", gsg.PR_TITLE_TYPES, tuple(m_types.group(1).split()) if m_types else None),
        ("subjectPattern", gsg.PR_SUBJECT_PATTERN, m_subject.group(1).strip() if m_subject else None),
    ]:
        ok = hook_v == wf_v
        if not ok:
            failures.append((f"PR title {what} vs pr-title-lint.yml", wf_v, hook_v))
        print(f"  [{'ok' if ok else 'FAIL'}] PR title {what} matches pr-title-lint.yml")
else:
    print("  [skip] no .github/workflows/pr-title-lint.yml to compare the title rule against")

if failures:
    print(f"\n{len(failures)} FAILED:")
    for command, expected, got in failures:
        print(f"  {command!r}: expected {expected}, got {got}")
    sys.exit(1)
print(f"\nAll {len(CASES)} subprocess + 5 white-box cases passed.")
