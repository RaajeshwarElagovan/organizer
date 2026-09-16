#!/usr/bin/env bash
# Build the release artifacts into dist/ from the committed Git tree:
#   dist/organizer-<version>.tar.gz     source tarball (git archive, reproducible)
#   dist/organizer_<version>-1_all.deb  Debian package (dpkg-buildpackage -b)
# Nothing is tagged, committed or published. The .deb is built from the tarball
# in a scratch directory, so the checkout and its parent stay clean.
# Needs: git, dpkg-dev, debhelper, dh-python, fakeroot (lintian optional).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REF="${1:-HEAD}"
cd "$REPO"

VERSION="$(python3 -c 'import re,sys
with open("organizer/__init__.py") as f:
    print(re.search(r"__version__ = \"([^\"]+)\"", f.read()).group(1))')"
DEBVER="$(dpkg-parsechangelog -l debian/changelog -S Version)"
[ "${DEBVER%-*}" = "$VERSION" ] || { echo "debian/changelog says $DEBVER but __version__ is $VERSION" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || echo "WARNING: uncommitted changes are NOT included (building from $REF)" >&2

DIST="$REPO/dist"; mkdir -p "$DIST"
TARBALL="$DIST/organizer-$VERSION.tar.gz"
DEB="organizer_${DEBVER}_all.deb"

echo "== source tarball: $TARBALL (git archive $REF)"
# gzip -n: no name/timestamp in the gzip header; git archive uses the commit
# time for every entry, so the same commit always yields the same bytes.
git archive --format=tar --prefix="organizer-$VERSION/" "$REF" | gzip -n -9 > "$TARBALL"

echo "== debian package: $DIST/$DEB"
BUILD="$(mktemp -d "${TMPDIR:-/tmp}/organizer-build.XXXXXX")"
trap 'rm -rf "$BUILD"' EXIT
tar -C "$BUILD" -xzf "$TARBALL"
# Sanitised PATH: a toolchain in ~/.local/bin (e.g. a musl gcc wrapper) would
# make dpkg-architecture mis-detect the host and fail the build-deps check.
( cd "$BUILD/organizer-$VERSION" && PATH=/usr/local/bin:/usr/bin:/bin dpkg-buildpackage -us -uc -b >"$BUILD/build.log" 2>&1 ) \
  || { cat "$BUILD/build.log" >&2; exit 1; }
cp "$BUILD/$DEB" "$DIST/$DEB"
cp "$BUILD/organizer_${DEBVER}_amd64.buildinfo" "$DIST/" 2>/dev/null || true
if command -v lintian >/dev/null; then
  echo "== lintian"
  ( cd "$BUILD" && lintian --no-tag-display-limit "$DEB" ) || true
fi
echo
ls -l "$DIST"
sha256sum "$TARBALL" "$DIST/$DEB"
