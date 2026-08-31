#!/usr/bin/env bash
# Build the headless-server Debian package (harmony-server_<version>_all.deb).
#
# A .deb is an `ar` archive of three members, in order: debian-binary,
# control.tar.gz, data.tar.gz. We assemble it by hand so it builds on any host
# with binutils + tar (no dpkg-deb needed). The package ships the app source and
# a systemd unit; postinst builds a private virtualenv and pip-installs the app
# (Architecture: all — pip fetches the right dep wheels on the target).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/src/harmony/__init__.py")"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

# -- data tree: the installable app source, the unit, a manual wrapper --------
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
echo "Wrote $out"
