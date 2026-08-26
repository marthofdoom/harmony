# Layering: the engine/frontend boundary

**Status: Built & verified** (enforced by `tests/test_layering.py` and CI).

Harmony is one engine with several frontends. Today that's the GTK desktop app;
planned are a hosted web app and an Android app, plus instance
[federation](../design/federation.md). All of that only works if the engine is
frontend-agnostic.

## The rule

**No module outside `harmony.ui` may import GTK (`gi` / `Gtk` / `Adw` / WebKit).**

The engine — `models`, `config`, `db`, `providers`, `matching`, `sync`,
`io_formats`, `enrich`, `ai` — must import and run on a machine with no GTK
installed at all. It's checked two ways:

1. `tests/test_layering.py` parses the source (doesn't import it, so it runs
   headless) and fails if any core module imports a GUI toolkit.
2. CI additionally imports the whole engine with `gi` forced unavailable, which
   is stronger evidence than static analysis.

The one sanctioned exception is `harmony.tasks`, the bridge to the GTK main
loop: it may touch `GLib`, but only inside function bodies (never at module
scope), so the module still imports headless. A dedicated test enforces that.

## Why it matters for the roadmap

The engine has to be callable three ways, interchangeably:

- **Embedded, in-process** — the desktop app today.
- **Over HTTP** — a client pointed at a remote instance (federation).
- **By the Android client** — same interface, different transport.

Frontends should therefore depend on an *interface* (`providers`, `sync`,
`search`, …), satisfied by either the local engine or an HTTP client to another
instance. Keeping GTK out of the engine is what keeps that door open. When the
web/Android/federation work begins, `tasks.py` is the one module that gets
swapped (its GLib bridge becomes a job queue or async runtime); everything else
moves unchanged.

See [ADR 0001](../decisions/0001-engine-frontend-separation.md).
