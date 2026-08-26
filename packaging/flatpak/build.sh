#!/usr/bin/env bash
# Build Harmony as a Flatpak: user-install it AND export a single-file bundle
# (packaging/flatpak/Harmony-<version>.flatpak) for sideloading elsewhere with
#   flatpak install --user Harmony-<version>.flatpak
#
# The app source is pulled from git (the manifest's `harmony` module tracks the
# public repo's main branch), so commit and push your changes before running
# this. WebKitGTK comes from the GNOME runtime; no extra setup is needed.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
manifest="$here/io.github.marthofdoom.Harmony.yml"
statedir="$here/.flatpak-builder"
builddir="$here/build"
repodir="$here/repo"
appid="io.github.marthofdoom.Harmony"

version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/src/harmony/__init__.py")"
bundle="$here/Harmony-${version:-dev}.flatpak"

if ! flatpak info org.flatpak.Builder >/dev/null 2>&1; then
    echo "org.flatpak.Builder is not installed." >&2
    echo "Install it with: flatpak install -y flathub org.flatpak.Builder" >&2
    exit 1
fi

# Build once; install locally and export to an OSTree repo we can bundle from.
flatpak run org.flatpak.Builder \
    --force-clean \
    --user --install \
    --repo="$repodir" \
    --state-dir="$statedir" \
    "$builddir" "$manifest" "$@"

# Single-file bundle (app only; the runtime comes from Flathub on install).
flatpak build-bundle "$repodir" "$bundle" "$appid"
echo "Wrote $bundle"
