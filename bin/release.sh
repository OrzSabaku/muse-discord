#!/bin/bash
# Tag a release: bin/release.sh X.Y.Z [--yes]   (or X.Y.Z-rc.N)
#
# Checks that you are on main, clean and level with origin/main, that
# CHANGELOG.md has a "## vX.Y.Z (YYYY-MM-DD): Title" entry and that the
# tag is new; then creates an annotated tag and pushes it. The push runs
# .github/workflows/release.yml, which publishes the GitHub Release.
# See RELEASING.md.
set -euo pipefail

die() { echo "release: $*" >&2; exit 1; }
usage() { echo "usage: bin/release.sh X.Y.Z[-rc.N] [--yes]" >&2; exit 2; }

version="" yes=0
for arg in "$@"; do
  case $arg in
    -y | --yes) yes=1 ;;
    -*) usage ;;
    *) [ -z "$version" ] || usage; version=${arg#v} ;;
  esac
done
[ -n "$version" ] || usage
num='(0|[1-9][0-9]*)' ident='[0-9A-Za-z-]+'
[[ $version =~ ^$num\.$num\.$num(-$ident(\.$ident)*)?$ ]] ||
  die "$version is not semver (X.Y.Z, or X.Y.Z-rc.N for a pre-release)"
tag=v$version base=v${version%%-*}

cd "$(git rev-parse --show-toplevel)"
[ "$(git symbolic-ref --short -q HEAD)" = main ] || die "check out main first"
[ -z "$(git status --porcelain)" ] || die "the working tree is not clean"
git fetch --quiet origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] ||
  die "main is not level with origin/main; pull (or merge your PR) first"
! git rev-parse -q --verify "refs/tags/$tag" >/dev/null || die "tag $tag already exists locally"
! git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null ||
  die "tag $tag already exists on origin"

entry() { grep -m1 -E "^## ${1//./\\.} \([0-9]{4}-[0-9]{2}-[0-9]{2}\): .+" CHANGELOG.md; }
heading=$(entry "$tag" || { [ "$tag" != "$base" ] && entry "$base"; }) ||
  die "CHANGELOG.md has no '## $tag (YYYY-MM-DD): Short title' entry; add one via a PR first"
title=${heading#*): }

echo "Release $tag from $(git log -1 --format='%h %s')"
echo "  entry: $heading"
echo "  then:  push annotated tag $tag to origin; the Release workflow publishes it"
if [ "$yes" -ne 1 ]; then
  read -r -p "Proceed? [y/N] " answer || answer=""
  [[ $answer == [yY]* ]] || die "aborted; nothing was tagged"
fi

git tag -a "$tag" -m "$tag: $title"
git push origin "refs/tags/$tag"
echo "Pushed $tag. Watch the Release workflow in the repo's Actions tab."
