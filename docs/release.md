# Releases

media-intake is released as annotated git tags `vX.Y.Z` on `main`, with a
GitHub release per tag. Nothing is published to PyPI; a release is the
source tree at that commit, including its pins (`requirements*.txt`,
`media_intake/pins.json`), so a tagged checkout runs `setup.sh` against
exactly the wheels and models it was tested with.

Versions follow [Semantic Versioning](https://semver.org/). While the major
version is 0, a minor bump (`0.2.0`) may change commands, config or on-disk
state; a patch bump (`0.1.1`) only fixes bugs. A release that changes the
state folder's layout says how to migrate in its notes.

## Using a release (consumers)

Pin a release by **tag and commit SHA**, not by tag alone: a tag is a
movable name, the SHA is what you reviewed. Each release's notes give both.

```sh
git clone --branch v0.1.0 https://github.com/Absence0760/media-intake.git
cd media-intake
git rev-parse 'HEAD^{commit}'     # must equal the SHA in the release notes
bash setup.sh
```

To check a tag without cloning, `git ls-remote
https://github.com/Absence0760/media-intake.git 'refs/tags/v0.1.0^{}'` prints
the commit an annotated tag points at. If the tag and the SHA you recorded
ever disagree, stop: a published tag is never moved (see below), so a
mismatch means something is wrong. Anything that installs it for you (a
script, a dotfiles repo) records both, for example
`MEDIA_INTAKE_TAG=v0.1.0 MEDIA_INTAKE_SHA=<40 hex>`, and checks out the SHA.

## Cutting a release (maintainers)

1. **Check `main` is releasable.** `CI gate` is green on `origin/main`, and
   the model tests passed on it: run `models` (`.github/workflows/models.yml`)
   from the Actions tab with "Run workflow" on `main`, or use last night's
   run if `main` hasn't moved since. Decide whether to bump pins first
   (`python3 -I scripts/update-pins.py --check`; CONTRIBUTING.md, Pins). A
   pin bump is its own PR, merged and model-tested before the release PR.

2. **Bump the version in one place.** The version lives only in
   `media_intake/__init__.py` (`__version__ = "X.Y.Z"`). `pyproject.toml`
   reads it from there (`dynamic = ["version"]`,
   `[tool.setuptools.dynamic]`), so don't add a `version =` line to it.
   ```sh
   git fetch origin
   git worktree add ../media-intake-release-X.Y.Z -b chore/release-X.Y.Z origin/main
   cd ../media-intake-release-X.Y.Z
   # edit media_intake/__init__.py: __version__ = "X.Y.Z"
   python3 -I -m unittest discover -s tests -p "test_cli.py"   # includes the version check
   git commit -m "chore(release): X.Y.Z" -- media_intake/__init__.py
   git push -u origin chore/release-X.Y.Z
   gh pr create --title "chore(release): X.Y.Z" --body "<release notes draft>"
   ```
   Tick what the release finishes in `ROADMAP.md` in the same PR.

3. **Tag the merge commit.** After the PR is squash-merged, tag the commit
   on `origin/main` whose `__init__.py` says `X.Y.Z`, never a local branch:
   ```sh
   git fetch origin
   git show origin/main:media_intake/__init__.py | grep __version__   # X.Y.Z
   sha=$(git rev-parse origin/main)
   git tag -a vX.Y.Z -m "media-intake X.Y.Z" "$sha"      # -s instead of -a to sign it
   git push origin vX.Y.Z
   ```

4. **Publish the release** with the tag and its full SHA in the notes, so
   consumers can pin both:
   ```sh
   gh release create vX.Y.Z --verify-tag --title "media-intake X.Y.Z" \
     --notes "Commit: $sha. Pin by tag and this SHA (docs/release.md). <changes>"
   ```

## Rules

- **A published tag is never moved or deleted.** A broken release is fixed
  by the next patch release; its notes say what was wrong with the last one.
- Releases come only from `main`, through a PR, after the model tests pass.
- Tags are annotated (signed when the maintainer has a signing key), so they
  carry who made them and when.
- Owner step, not done by any script here: a tag ruleset on `refs/tags/v*`
  that blocks updates and deletions makes "never moved" enforced rather
  than a convention (Settings, Rules, Rulesets, New tag ruleset: target
  `v*`, restrict updates, restrict deletions).
