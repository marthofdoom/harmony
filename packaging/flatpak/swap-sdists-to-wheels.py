#!/usr/bin/env python3
"""Rewrite sdist sources in a flatpak-pip-generator module file to wheels.

flatpak-pip-generator sometimes pins a source tarball for packages that also
publish prebuilt wheels (rapidfuzz, cryptography, cffi, pydantic_core, jiter).
Building those from sdist inside flatpak-builder means a compiler and, for the
Rust ones, a whole toolchain — offline. Every one of them ships a manylinux
(or abi3) wheel for cp313/x86_64, so we swap each sdist for the matching wheel
and the build becomes a pure unpack-and-copy with no network.

Usage: swap-sdists-to-wheels.py python3-deps.json
Targets cp313 / x86_64 to match the GNOME 49 runtime (Python 3.13).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request

_NAME_VER = re.compile(r"^(?P<n>.+?)-(?P<v>\d[^-]*)\.(?:tar\.gz|zip)$")


def pypi_files(name: str, version: str) -> list[dict]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)["urls"]


def pick_wheel(files: list[dict]) -> dict | None:
    """Prefer a pure-python wheel, else a cp313/abi3 manylinux x86_64 wheel."""
    pure = [
        f for f in files
        if f["filename"].endswith(("-py3-none-any.whl", "-py2.py3-none-any.whl"))
    ]
    if pure:
        return pure[0]

    def compatible(fn: str) -> bool:
        return (
            fn.endswith(".whl")
            and "x86_64" in fn
            and "manylinux" in fn
            and "cp313t" not in fn  # skip the free-threaded build
            and ("cp313-cp313-" in fn or "abi3" in fn)
        )

    cands = [f for f in files if compatible(f["filename"])]
    # Concrete cp313 build first, abi3 fallback second.
    cands.sort(key=lambda f: (0 if "cp313-cp313-" in f["filename"] else 1, f["filename"]))
    return cands[0] if cands else None


def main(path: str) -> int:
    with open(path) as fh:
        data = json.load(fh)

    swapped, failed = [], []
    for module in data["modules"]:
        for i, source in enumerate(module["sources"]):
            filename = source["url"].rsplit("/", 1)[-1]
            match = _NAME_VER.match(filename)
            if not match:
                continue  # already a wheel
            name, version = match["n"].replace("_", "-"), match["v"]
            try:
                wheel = pick_wheel(pypi_files(name, version))
            except Exception as exc:  # noqa: BLE001 - report and continue
                failed.append((filename, f"lookup failed: {exc!r}"))
                continue
            if wheel is None:
                failed.append((filename, "no compatible wheel published"))
                continue
            module["sources"][i] = {
                "type": "file",
                "url": wheel["url"],
                "sha256": wheel["digests"]["sha256"],
            }
            swapped.append((filename, wheel["filename"]))

    with open(path, "w") as fh:
        json.dump(data, fh, indent=4)
        fh.write("\n")

    for old, new in swapped:
        print(f"swapped {old}\n     -> {new}")
    if failed:
        print("\nCOULD NOT SWAP (build will need to compile these):", file=sys.stderr)
        for name, why in failed:
            print(f"  {name}: {why}", file=sys.stderr)
        return 1
    print(f"\n{len(swapped)} sdist(s) swapped to wheels; none left to compile.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
