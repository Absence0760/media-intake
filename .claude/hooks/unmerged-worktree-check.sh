#!/usr/bin/env bash
# SessionStart hook: surface work that has no route to `main`.
#
# Every change reaches `main` through a pull request, from its own branch and
# usually its own worktree. Retiring a worktree opens no PR, so a branch that is
# forgotten before its PR exists sits stranded off `main`.
#
# This hook lists every local branch holding work not yet on `main` and prints a
# warning to stdout, which Claude Code injects as session context, so the next
# turn can surface it and offer to open the missing PR. A branch whose PR is
# still open is listed too; that is expected. Silent when everything is on
# `main`. Fail-open: any error exits 0 with no output, never wedging startup.
#
# ## Which `main`
#
# Local `main` lags (and in a shared checkout accumulates other sessions'
# commits), while PRs merge into `origin/main`. So the comparison base is the
# remote default branch when one is known (`origin/HEAD`, else `origin/main`,
# else `origin/master`), falling back to local `main` / `master`. A repo with
# neither stays quiet.
#
# ## Why `git branch --no-merged` is not enough
#
# That test is ancestry-based, and PRs are squash-merged. A squash rewrites the
# branch's commits into one new commit with a different SHA, so the branch tip
# never becomes an ancestor of `main` and the branch stays "unmerged" forever,
# even though every line of it is on `main`. Measured in one project:
# the bare test reported three long-merged branches as 99 stranded commits. A
# warning that fires every session for landed work trains the reader to dismiss
# it, and the one genuinely stranded branch goes with it.
#
# So each candidate gets a second, content-based test. `commit-tree` builds a
# throwaway commit holding the branch's whole tree parented on its merge-base
# (the branch's cumulative diff as one patch, the same shape a squash merge
# produces) and `git cherry` asks whether the base already holds an equivalent
# patch. `-` means yes (squash-merged; stay quiet), `+` means no (report). The
# temporary commit is unreferenced and is collected by the next `git gc`.
set -u

repo="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -n "$repo" ] || exit 0

g() { git -C "$repo" "$@" 2>/dev/null; }

base=""
head_ref="$(g symbolic-ref --quiet refs/remotes/origin/HEAD)" && base="${head_ref#refs/remotes/}"
if [ -z "$base" ] || ! g rev-parse --verify --quiet "$base" >/dev/null; then
	base=""
	for cand in origin/main origin/master main master; do
		if g rev-parse --verify --quiet "$cand" >/dev/null; then
			base="$cand"
			break
		fi
	done
fi
[ -n "$base" ] || exit 0
base_name="${base#origin/}"

# Branches with commits not contained in the base. The base's own local
# namesake is skipped: a local `main` that is merely behind or ahead of
# `origin/main` is not a PR branch.
branches="$(g branch --no-merged "$base" --format='%(refname:short)')" || exit 0
[ -n "$branches" ] || exit 0

current="$(g rev-parse --abbrev-ref HEAD || echo '')"

# True when the base already holds an equivalent patch for the branch's
# cumulative diff: the squash-merge case ancestry cannot see.
squash_merged() {
	local br="$1" mb squashed cherry
	mb="$(g merge-base "$base" "$br")" || return 1
	[ -n "$mb" ] || return 1
	squashed="$(g commit-tree "$br^{tree}" -p "$mb" -m _)" || return 1
	[ -n "$squashed" ] || return 1
	cherry="$(g cherry "$base" "$squashed")" || return 1
	case "$cherry" in
		-*) return 0 ;;
		*) return 1 ;;
	esac
}

report=""
while IFS= read -r br; do
	[ -n "$br" ] || continue
	[ "$br" = "$base_name" ] && continue
	squash_merged "$br" && continue
	count="$(g rev-list --count "$base..$br" || echo '?')"
	wt="$(g worktree list --porcelain \
		| awk -v b="refs/heads/$br" '
			/^worktree /{path=substr($0,10)}
			$0=="branch "b{print path}')"
	suffix=""
	[ "$br" = "$current" ] && suffix=" (the branch this session is on, so probably in flight)"
	if [ -n "$wt" ]; then
		report="${report}  - ${br} (${count} commit(s) off ${base}), worktree: ${wt}${suffix}
"
	else
		report="${report}  - ${br} (${count} commit(s) off ${base})${suffix}
"
	fi
done <<EOF
$branches
EOF

[ -n "$report" ] || exit 0

cat <<EOF
[unmerged-worktree-check] Work exists that is NOT on \`${base}\`:
${report}
Branches already squash-merged are filtered out, so everything listed really is
absent from \`${base}\`.

Work reaches \`main\` only through a pull request with a green \`CI gate\`
(squash-merged), never a local merge. A branch listed here whose PR is still
open is expected, not stranded. For one with no PR yet
(\`gh pr list --head <branch> --state all\`), from its worktree:
  git fetch origin && git merge origin/main   # bring it up to date; never rebase a pushed branch
  git push -u origin <branch>
  gh pr create --base main --title "<type>(<scope>): <subject>"
Surface this to the user and offer to open the missing PR before the work is forgotten.
EOF
exit 0
