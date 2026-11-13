#!/usr/bin/env python3
"""PreToolUse Bash guard for concurrent Claude sessions.

Blocks git commands that would sweep up working-tree changes the current
session did not make — so two (or more) Claude instances running in the same
checkout can only ever commit / discard the explicit paths they name, never
each other's in-flight edits.

It reads the PreToolUse hook payload on stdin, tokenises the command in a
quote-aware way (shlex), splits it into shell segments, and inspects each git
invocation. A blocked command is denied with a reason that points at the
path-scoped alternative; everything else is allowed through untouched.

Blocked:
  git add -A / --all / -u / --update / .          (stages the whole tree)
  git commit (no pathspec)                         (commits the whole staged index)
  git commit -a / -am / --all                      (auto-stages tracked edits)
  git commit --amend with staged changes           (folds the index into HEAD)
  git stash (any form that writes refs/stash)      (one stack shared by every worktree)
  git reset --hard                                 (discards all working edits)
  git checkout/restore . (or :/, *) — no pathspec  (discards across the tree)
  git rm . (or :/, *)                              (removes the whole tree)
  git clean -f                                     (deletes untracked files)
  git push to main/master, bare `git push`, HEAD,  (main only changes through a PR)
    --all / --mirror
  git push --force / -f / --force-with-lease / +ref (update a pushed branch by
                                                    merging origin/main instead)
  gh pr create/edit --title "<not conventional>"   (CI's pr-title-lint rejects it)

Allowed: git add <path>, git commit -m "…" -- <path>, git commit --allow-empty,
git commit --amend (pure reword, nothing staged), git restore -- <path>,
git stash list / show, git push -u origin <branch>, git push origin --delete
<branch>, and all read-only git.

Heredoc bodies (a commit message fed through `<<'EOF'`) are data, not shell,
and are never inspected — a message that merely mentions `git add -A` passes.
"""

import json
import os
import re
import shlex
import subprocess
import sys

PATHSPEC_ALL = {".", "*", "./", ":/", ":/.", ":/*"}

# A bare `<<` / `<<-` redirect token. Runs of punctuation come back as one
# token from shlex, so `<<<` (herestring) and a quoted `"<<EOF"` (data, not a
# redirect) both fail this and are correctly left alone.
_HEREDOC_OP = re.compile(r"^<<-?$")

# The PR-title rule CI enforces (.github/workflows/pr-title-lint.yml; the test
# checks these against it when the workflow exists): `<type>(<scope>): <subject>`,
# the subject starting lowercase and not ending with a period.
PR_TITLE_TYPES = ("feat", "fix", "chore", "docs", "refactor", "test", "perf",
                  "ci", "build", "revert")
PR_SUBJECT_PATTERN = r"^[a-z].+[^\.]$"

PROTECTED = {"main", "refs/heads/main", "master", "refs/heads/master"}
# Push options whose value is the next argument (--force-with-lease and
# --signed only take one in their `=` form).
PUSH_TAKES_VALUE = {"--repo", "--receive-pack", "--exec", "--push-option", "-o"}


def _deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _heredoc_delimiters(line):
    """Delimiters opened by heredoc redirects on this line."""
    # posix=False keeps quotes attached to their token, which is what lets a
    # quoted `"<<EOF"` (a string that merely contains the characters) be told
    # apart from a real `<<EOF` redirect.
    lex = shlex.shlex(line, posix=False, punctuation_chars=";&|<>()")
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return []  # unbalanced quotes on this line — don't guess
    delims = []
    for i, tok in enumerate(tokens):
        if not _HEREDOC_OP.match(tok) or i + 1 >= len(tokens):
            continue
        delim = tokens[i + 1]
        if delim.startswith("-"):
            delim = delim[1:]  # `<<-EOF` splits as `<<` + `-EOF`
        if len(delim) >= 2 and delim[0] == delim[-1] and delim[0] in "'\"":
            delim = delim[1:-1]
        if delim:
            delims.append(delim)
    return delims


def _strip_heredoc_bodies(command):
    """Drop heredoc BODIES, keeping the line that opens them.

    A heredoc body is data (a commit message, a file being written), not shell.
    Left in place, its newlines become `;` separators below, so a line of prose
    is parsed as a command: a commit message that merely mentions `git add -A`
    was read as that command and denied. The guard has to see the redirect
    itself, but must never read what is fed through it. Everything after the
    terminator line is shell again, so the body is not a hiding place.
    """
    lines = command.split("\n")
    kept, i = [], 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        delims = _heredoc_delimiters(line)
        i += 1
        for delim in delims:
            # Consume the body, then the terminator line. An unterminated
            # heredoc runs to EOF, which is also what the shell would do.
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1
    return "\n".join(kept)


def _segments(command):
    """Split a shell command into segments on operators, respecting quotes."""
    # Treat newlines as statement separators so multi-line commands are split.
    normalised = _strip_heredoc_bodies(command).replace("\n", " ; ")
    lex = shlex.shlex(normalised, posix=True, punctuation_chars=";&|<>()")
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        # Unbalanced quotes etc. — don't guess, let the command through.
        return []
    segments, current = [], []
    for tok in tokens:
        if tok and all(c in ";&|<>()" for c in tok):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _git_subcommand(tokens):
    """Return (subcommand, args) for a git invocation, or None."""
    i = 0
    while i < len(tokens) and not (tokens[i] == "git" or tokens[i].endswith("/git")):
        i += 1
    if i >= len(tokens):
        return None
    i += 1
    takes_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                   "--exec-path", "--super-prefix"}
    while i < len(tokens):
        t = tokens[i]
        if t in takes_value:
            i += 2
        elif t.startswith("-"):
            i += 1
        else:
            break
    if i >= len(tokens):
        return None
    return tokens[i], tokens[i + 1:]


def _check_add(args):
    after_dd = False
    for a in args:
        if a == "--":
            after_dd = True
            continue
        if not after_dd and a.startswith("-"):
            cluster = a[1:]
            if a in ("-A", "--all", "-u", "--update", "--no-ignore-removal") \
                    or (not a.startswith("--") and ("A" in cluster or "u" in cluster)):
                return ("`git add %s` stages every change in the working tree, "
                        "including files another Claude session may be editing. "
                        "Stage only the paths you changed: `git add path/to/file ...`."
                        % a)
            continue
        if a in PATHSPEC_ALL:
            return ("`git add %s` stages everything under the repo root, which can "
                    "include another session's changes. Name explicit paths instead: "
                    "`git add path/to/file ...`." % a)
    return None


def _has_staged_changes():
    """True if the shared index has staged changes. Fail-open (False) if git
    can't be queried — the guard should never wedge a command on uncertainty."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        # `diff --cached --quiet` exits 1 when there are staged changes, 0 when
        # clean. `-C <repo>` makes it independent of the hook's cwd.
        result = subprocess.run(
            ["git", "-C", repo, "diff", "--cached", "--quiet"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 1
    except Exception:
        return False


def _check_commit(args):
    skip_next = False
    saw_double_dash = False
    has_pathspec = False
    is_amend = False
    allow_empty = False
    no_edit = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a == "--":
            saw_double_dash = True
            continue
        if saw_double_dash:
            # Everything after `--` is a pathspec — the commit is scoped.
            has_pathspec = True
            continue
        if a == "--all":
            return ("`git commit --all` auto-stages every tracked modification, "
                    "including another session's. Stage your own paths explicitly "
                    "(`git add <path>`) and commit without --all.")
        if a.startswith("--"):
            if a == "--amend":
                is_amend = True
            if a in ("--allow-empty", "--allow-empty-message"):
                allow_empty = True
            if a == "--no-edit":
                no_edit = True
            if a.startswith("--pathspec-from-file"):
                has_pathspec = True
            if a in ("--message", "--file", "--reuse-message", "--reedit-message",
                     "--fixup", "--squash", "--author", "--date", "--template",
                     "--pathspec-from-file"):
                skip_next = True
            continue
        if a.startswith("-") and len(a) > 1:
            cluster = a[1:]
            if "a" in cluster:
                return ("`git commit -%s` includes -a, which auto-stages every "
                        "tracked modification (including another session's). Drop -a, "
                        "stage your specific paths with `git add <path>`, then commit."
                        % cluster)
            if cluster[-1] in ("m", "F", "C", "c"):
                skip_next = True
            continue
        # A bare positional token is a pathspec — the commit is scoped.
        has_pathspec = True

    if has_pathspec or allow_empty:
        return None
    if is_amend:
        # Amend folds the whole staged index into the previous commit. With
        # nothing staged it's a pure message reword (safe); with staged changes
        # in a shared checkout it absorbs another session's files AND rewrites a
        # shared commit. Block only the dangerous case.
        if _has_staged_changes():
            return ("`git commit --amend` with staged changes folds the ENTIRE staged "
                    "index into the previous commit — in a shared checkout that absorbs "
                    "another session's staged files (and rewrites a shared commit). "
                    "Unstage what isn't yours, or scope it: "
                    "`git commit --amend -- path/to/file ...`. A pure reword with "
                    "nothing staged is allowed.")
        return None
    if no_edit:
        # Merge / cherry-pick continuation (`git commit --no-edit`) — no pathspec
        # is normal there; not the racy bare-commit case.
        return None
    return ("`git commit` with no pathspec commits the ENTIRE staged index — in a "
            "shared checkout that sweeps up whatever another Claude session has "
            "staged (this has happened). Commit only your own paths: "
            "`git commit -m \"…\" -- path/to/file ...` (a path-scoped commit "
            "ignores anything else staged). Use --allow-empty if you genuinely have "
            "no paths.")


STASH_READ_ONLY = {"list", "show"}


def _check_stash(args):
    op = args[0] if args else "push"
    if op in STASH_READ_ONLY:
        return None
    return ("`git stash%s` is blocked, even with `-- <path>`: refs/stash is ONE "
            "stack shared by every worktree and session, so the path limits what "
            "goes in but not who pops it back out. Concurrent sessions have "
            "restored each other's work this way. To park changes, write a patch "
            "into your own scratchpad subdirectory (`git diff -- <path> > "
            "<scratchpad>/<you>/x.patch`, later `git apply` it) and restore the "
            "file with `git checkout HEAD -- <path>`, or do the work in a "
            "throwaway worktree." % ((" " + " ".join(args)) if args else ""))


def _check_reset(args):
    if "--hard" in args:
        return ("`git reset --hard` discards ALL uncommitted changes in the working "
                "tree, including another session's. To unstage your own files use "
                "`git restore --staged <path>`; to discard your own edits use "
                "`git restore <path>`.")
    return None


def _check_discard(name, args):
    after_dd = False
    paths = []
    for a in args:
        if a == "--":
            after_dd = True
            continue
        if not after_dd and a.startswith("-"):
            continue
        paths.append(a)
    for p in paths:
        if p in PATHSPEC_ALL:
            return ("`git %s %s` discards working-tree changes across the whole repo, "
                    "including another session's. Restore only your paths: "
                    "`git %s -- path/to/file`." % (name, p, name))
    return None


def _check_rm(args):
    after_dd = False
    paths = []
    for a in args:
        if a == "--":
            after_dd = True
            continue
        if not after_dd and a.startswith("-"):
            continue
        paths.append(a)
    for p in paths:
        if p in PATHSPEC_ALL:
            return ("`git rm %s` removes every tracked file under the repo root from "
                    "the index (and the working tree, unless --cached) — that stages "
                    "deletions across another session's work too. Name the specific "
                    "paths: `git rm path/to/file ...`." % p)
    return None


def _check_clean(args):
    forced = "--force" in args or any(
        a.startswith("-") and not a.startswith("--") and "f" in a[1:] for a in args
    )
    if forced:
        return ("`git clean` deletes untracked files, which may include new files "
                "another session just created. Delete only the specific files you "
                "created instead of cleaning the whole tree.")
    return None


def _check_push(args):
    """Changes reach main through a PR: push a branch, never main itself, and
    never rewrite a pushed branch."""
    how = ("Push your branch and open a PR instead: "
           "`git push -u origin <branch>` then `gh pr create` (CONTRIBUTING.md "
           "§ Git workflow).")
    positional, i = [], 0
    while i < len(args):
        a = args[i]
        if a in ("--all", "--mirror") or a.startswith("--mirror"):
            return "`git push %s` can push main. %s" % (a, how)
        if a in ("--force", "--force-if-includes") or a.startswith("--force-with-lease") \
                or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]):
            return ("`git push %s` rewrites a published branch. Once a branch is "
                    "pushed, bring it up to date by merging: `git fetch origin && "
                    "git merge origin/main`, then push normally. The PR is "
                    "squash-merged, so the merge commit never reaches main." % a)
        if a in PUSH_TAKES_VALUE and "=" not in a:
            i += 2
            continue
        if not a.startswith("-"):
            positional.append(a)
        i += 1
    refspecs = positional[1:]
    if not refspecs:
        return ("A bare `git push` pushes whatever branch is checked out, which may "
                "be main. Name the branch: `git push -u origin <branch>`. " + how)
    for spec in refspecs:
        if spec.startswith("+"):
            return ("`git push … %s` force-pushes (the leading `+`). Update a pushed "
                    "branch by merging `origin/main` into it instead." % spec)
        src, _, dst = spec.partition(":")
        target = dst or src
        if target in ("HEAD", "@"):
            return ("`git push … %s` pushes the checked-out branch, which may be "
                    "main. Name the branch explicitly. %s" % (spec, how))
        if target in PROTECTED:
            return ("Never push to main: every change reaches it through a pull "
                    "request (main is also protected on GitHub). " + how)
    return None


def _check_pr_title(tokens):
    """`gh pr create|edit --title` must pass CI's conventional-title lint."""
    i = 0
    while i < len(tokens) and not (tokens[i] == "gh" or tokens[i].endswith("/gh")):
        i += 1
    rest = tokens[i + 1:]
    if len(rest) < 2 or rest[0] != "pr" or rest[1] not in ("create", "edit"):
        return None
    title = None
    args = rest[2:]
    for j, a in enumerate(args):
        if a in ("--title", "-t") and j + 1 < len(args):
            title = args[j + 1]
        elif a.startswith("--title="):
            title = a.split("=", 1)[1]
    if title is None:
        return None
    m = re.match(r"^(\w+)(\([^)]*\))?!?: (.*)$", title)
    ok = (m is not None and m.group(1) in PR_TITLE_TYPES
          and re.match(PR_SUBJECT_PATTERN, m.group(3)) is not None)
    if ok:
        return None
    return ("PR title %r fails CI's pr-title-lint (.github/workflows/pr-title-lint.yml). "
            "Use `<type>(<scope>): <subject>` with type one of %s, the subject "
            "starting lowercase and not ending with a period, e.g. "
            "`fix(api): return 404 for a missing report`. Putting the scope where "
            "the type goes (`web: …`) is the usual mistake."
            % (title, ", ".join(PR_TITLE_TYPES)))


CHECKS = {
    "push": _check_push,
    "add": _check_add,
    "commit": _check_commit,
    "stash": _check_stash,
    "reset": _check_reset,
    "checkout": lambda a: _check_discard("checkout", a),
    "restore": lambda a: _check_discard("restore", a),
    "rm": _check_rm,
    "clean": _check_clean,
}


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, json.JSONDecodeError):
        sys.exit(0)
    if payload.get("tool_name") != "Bash":
        sys.exit(0)
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "git" not in command and "gh" not in command:
        sys.exit(0)
    for tokens in _segments(command):
        reason = _check_pr_title(tokens)
        if reason:
            _deny(reason)
        parsed = _git_subcommand(tokens)
        if not parsed:
            continue
        sub, args = parsed
        check = CHECKS.get(sub)
        if check:
            reason = check(args)
            if reason:
                _deny(reason)
    sys.exit(0)


if __name__ == "__main__":
    main()
