#!/usr/bin/env bash
# Build the headless-server Debian package (harmony-server_<version>_all.deb).
#
# A .deb is an `ar` archive of three members, in order: debian-binary,
# control.tar.gz, data.tar.gz. We assemble it by hand so it builds on any host
# with binutils + tar (no dpkg-deb needed).
#
# Like the Flatpak, the package *vendors its Python dependencies as wheels* so
# the install is self-contained (offline) — no pip-from-PyPI on the target. We
# bundle wheels for the common server targets (CPython 3.11/3.12/3.13, x86_64);
# postinst installs them with `pip --no-index`, and falls back to PyPI if the
# target's Python/arch isn't among the bundled wheels. Building the wheel set
# needs network + a pip (bootstrapped here via venv); installing the .deb does not.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/src/harmony/__init__.py")"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

# Dependencies to vendor (runtime + the [server] extra). pip resolves the
# transitive set (urllib3, certifi, cryptography, SecretStorage, …).
DEPS="ytmusicapi requests rapidfuzz platformdirs keyring yt-dlp zeroconf PyChromecast"
PYVERS="3.11 3.12 3.13"
PLATFORMS="manylinux2014_x86_64 manylinux_2_17_x86_64 manylinux_2_28_x86_64"

# -- bootstrap a pip (host pythons here ship none) ----------------------------
python3 -m venv "$stage/pipenv"
PIP="$stage/pipenv/bin/pip"
"$PIP" install --quiet --upgrade pip >/dev/null 2>&1 || true

# -- vendor wheels: the app itself + all deps for each target ------------------
wheels="$stage/data/usr/share/harmony/wheels"
mkdir -p "$wheels"
echo "Building the harmony wheel…"
"$PIP" wheel --no-deps -w "$wheels" "$root" >/dev/null
for pv in $PYVERS; do
    abi="cp${pv//./}"
    echo "Downloading dependency wheels for CPython $pv (x86_64)…"
    plat_args=""
    for p in $PLATFORMS; do plat_args="$plat_args --platform $p"; done
    # shellcheck disable=SC2086
    "$PIP" download -d "$wheels" --only-binary=:all: \
        --python-version "$pv" --implementation cp \
        --abi "$abi" --abi abi3 --abi none \
        $plat_args \
        $DEPS >/dev/null
done
echo "Vendored $(find "$wheels" -name '*.whl' | wc -l) wheels."

# -- data tree: app source (online-fallback), the unit, a manual wrapper -------
app="$stage/data/usr/share/harmony/app"
mkdir -p "$app/src" "$stage/data/lib/systemd/system" "$stage/data/usr/bin"
cp "$root/pyproject.toml" "$root/README.md" "$root/LICENSE" "$app/"
cp -r "$root/src/harmony" "$app/src/"
find "$app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
cp "$here/harmony.service" "$stage/data/lib/systemd/system/harmony.service"
cat > "$stage/data/usr/bin/harmony-server" <<'EOF'
#!/bin/sh
# Run the installed Harmony server (or any `harmony` subcommand) by hand.
exec /opt/harmony/venv/bin/harmony "$@"
EOF
chmod 0755 "$stage/data/usr/bin/harmony-server"

# -- control tree -------------------------------------------------------------
ctl="$stage/control"
mkdir -p "$ctl"
sed "s/^Version:.*/Version: ${version}/" "$here/control" > "$ctl/control"
for f in postinst prerm postrm; do
    cp "$here/$f" "$ctl/$f"
    chmod 0755 "$ctl/$f"
done
( cd "$stage/data" && find . -type f -printf '%P\n' | while read -r p; do
    printf '%s  %s\n' "$(md5sum "$p" | cut -d' ' -f1)" "$p"
done ) > "$ctl/md5sums"

# -- assemble the ar archive (member order matters) ---------------------------
out="$here/harmony-server_${version}_all.deb"
( cd "$stage/data" && tar --owner=0 --group=0 --numeric-owner -czf "$stage/data.tar.gz" . )
( cd "$ctl" && tar --owner=0 --group=0 --numeric-owner -czf "$stage/control.tar.gz" . )
printf '2.0\n' > "$stage/debian-binary"
rm -f "$out"
( cd "$stage" && ar q "$out" debian-binary control.tar.gz data.tar.gz ) 2>/dev/null
echo "Wrote $out ($(du -h "$out" | cut -f1))"
