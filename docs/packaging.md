# Packaging

## Flatpak

**Status: Built & verified** (builds, installs, launches; WebKit + secrets
portal confirmed in-sandbox).

The Flatpak (`packaging/flatpak/`) is the primary distribution target. It's how
Harmony ships **WebKitGTK without a user-installed dependency**: the GNOME
runtime (`org.gnome.Platform//49`) already ships `WebKit-6.0.typelib` and
`libwebkitgtk-6.0`, so the embedded [Qobuz login](design/auth.md#qobuz) needs no
extra module.

### Layout

| File | Purpose |
|---|---|
| `io.github.marthofdoom.Harmony.yml` | The manifest. GNOME runtime 49, app built from the local git checkout. |
| `python3-deps.json` | Pinned Python dependency wheels (generated). |
| `flatpak-pip-generator.py` | Vendored generator for `python3-deps.json`. |
| `swap-sdists-to-wheels.py` | Rewrites any sdist source to its manylinux/abi3 wheel. |
| `regen-deps.sh` | Regenerate `python3-deps.json` end to end. |
| `build.sh` | Build + user-install the Flatpak. |

### Building

```bash
# One-time: the runtime, SDK, and builder
flatpak install -y flathub org.gnome.Platform//49 org.gnome.Sdk//49 org.flatpak.Builder

# Commit your changes (the manifest builds from the local git HEAD), then:
packaging/flatpak/build.sh
flatpak run io.github.marthofdoom.Harmony
```

### Why every dependency is a prebuilt wheel

flatpak-builder builds offline. Compiling `rapidfuzz` (C++), `cryptography`,
`cffi`, `pydantic_core`, or `jiter` (Rust/C) from sdist inside the sandbox means
a full toolchain and is slow and fragile. Every one publishes a
manylinux/abi3 wheel for cp313 (matching the runtime's Python 3.13), so
`swap-sdists-to-wheels.py` replaces each sdist with its wheel and the build does
no compilation, no Rust, and no network.

Regenerate after a dependency change:

```bash
PYTHON=/path/to/venv/python packaging/flatpak/regen-deps.sh
```

### Sandbox permissions

Network (the provider APIs), GPU/Wayland/X11 (GTK), and the secrets portal
(keyring). **No filesystem access** — playlist import/export uses the
file-chooser portal, and per-app config/data/cache land under
`~/.var/app/io.github.marthofdoom.Harmony/` automatically.

See [ADR 0003](decisions/0003-flatpak-for-webkit.md).
