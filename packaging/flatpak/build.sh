#!/usr/bin/env bash
# Build and install Harmony as a Flatpak (user install).
#
# The app source is pulled from the local git repo by commit, so commit your
# changes before running this. WebKitGTK comes from the GNOME runtime; no extra
# setup is needed for the embedded login.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
manifest="$here/io.github.marthofdoom.Harmony.yml"
statedir="$here/.flatpak-builder"
builddir="$here/build"

if ! flatpak info org.flatpak.Builder >/dev/null 2>&1; then
    echo "org.flatpak.Builder is not installed." >&2
    echo "Install it with: flatpak install -y flathub org.flatpak.Builder" >&2
    exit 1
fi

exec flatpak run org.flatpak.Builder \
    --force-clean \
    --user --install \
    --state-dir="$statedir" \
    "$builddir" "$manifest" "$@"
