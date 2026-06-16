# Releasing WatchMend

This document defines how WatchMend is versioned and released.

## Versioning policy

WatchMend follows [Semantic Versioning](https://semver.org/) in its pragmatic
`0.x` form: `0.MINOR.PATCH`. While the major version is `0`, the public surface
(panel URLs, data contract) is still allowed to evolve.

| Bump | When |
|------|------|
| **MINOR** (`0.X.0`) | New feature, breaking change, panel URL / data-contract change, or DB schema change |
| **PATCH** (`0.x.Y`) | Bug fix, copy / i18n, UX polish, or a documentation change worth publishing |

Even breaking changes bump only the minor version while we are on `0.x` — that
is SemVer's convention for a pre-1.0 project. Once `1.0` ships, breaking changes
bump the major version.

### Path to 1.0

We cut `1.0.0` on the next release after all of the following hold:

1. Outward positioning is finalized (README rewrite; audience and differentiators fixed).
2. Panel URLs and data contract are stable — one full release cycle (including at least one minor) with no breaking change.
3. No known blocker-level issue.
4. README, RELEASING, and CHANGELOG are complete and current.

## Conventional commits

Commits follow [Conventional Commits](https://www.conventionalcommits.org/). The
prefix drives the version bump:

| Prefix | Bump (`0.x`) | Bump (`>=1.0`) |
|--------|------------|--------------|
| `feat:` | minor | minor |
| `fix:` | patch | patch |
| `feat!:` or a `BREAKING CHANGE:` footer | minor | major |
| `docs:` `chore:` `refactor:` `test:` `ci:` `style:` | none — rides the next release | none; a publish-worthy docs change may ship as a patch |

Record every user-visible change under `## [Unreleased]` in **both**
`CHANGELOG.md` and `CHANGELOG.zh-CN.md` as you make it, so releasing is a *cut*,
not a *write*.

## Release process

`X.Y.Z` is the new version.

1. Branch: `git switch -c release/vX.Y.Z`
2. Bump `version` in `pyproject.toml`, then `uv lock`.
3. Cut the changelog in **both** files: move the entries under `## [Unreleased]`
   into a new `## [X.Y.Z] - <YYYY-MM-DD>` block, leave a fresh empty
   `## [Unreleased]`, and add the compare-link reference at the bottom.
4. `make check` (ruff + format + pytest + leak check) must be green.
5. Open a PR; both CI checks (`checks` + `demo-smoke`) must be green.
6. Merge with `gh pr merge --merge --delete-branch` (preserve granular history).
7. `gh release create vX.Y.Z` with notes taken from that version's changelog block.
8. The `release.yml` workflow builds and pushes the dual-arch image to
   `ghcr.io/hyxiaoge/watchmend:X.Y.Z` and `:X.Y` automatically.
9. Verify: `docker buildx imagetools inspect ghcr.io/hyxiaoge/watchmend:X.Y.Z`
   (do not build locally — CI owns the push).

## Rollback

Image tags are immutable and pinned. To roll back, set the deployed image tag to
the previous version and restart. WatchMend's storage is additive across releases
(no destructive migrations), so the N-1 image is always a safe target.
