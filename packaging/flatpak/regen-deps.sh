#!/usr/bin/env bash
# Regenerate python3-deps.json from the project's runtime dependencies.
#
# flatpak-pip-generator resolves the full transitive tree and pins each
# download by sha256. It shells out to `pip3`; if you only have pip inside a
# venv, point PIP3 at it (a wrapper that runs `python -m pip`).
#
# After generating, sdists for packages with compiled extensions (rapidfuzz,
# cryptography, cffi, pydantic_core, jiter) are swapped for their prebuilt
# manylinux/abi3 wheels by swap-sdists-to-wheels.py, so the Flatpak build needs
# no compiler, Rust toolchain, or network.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="${PYTHON:-python3}"

"$py" "$here/flatpak-pip-generator.py" --output "$here/python3-deps" \
    ytmusicapi requests rapidfuzz platformdirs keyring anthropic

"$py" "$here/swap-sdists-to-wheels.py" "$here/python3-deps.json"
echo "python3-deps.json regenerated."
