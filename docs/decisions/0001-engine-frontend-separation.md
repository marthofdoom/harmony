# 0001 — The engine must not import GTK

**Status:** Accepted.

## Context

Harmony started as a GTK desktop app but the roadmap has always included other
frontends — a hosted web app, an Android app — and later
[federation](../design/federation.md), where a client talks to a remote
instance's engine over HTTP. All of that requires the engine (providers,
matching, sync, enrichment, AI) to run with no display server and no GTK.

Left unguarded, GUI imports leak into logic modules gradually and are painful to
untangle later — exactly when the second frontend is being built and the pain is
highest.

## Decision

No module outside `harmony.ui` may import a GUI toolkit (`gi` / `Gtk` / `Adw` /
`WebKit`). Enforced by `tests/test_layering.py`, which parses source (so it runs
headless) rather than importing it, plus a CI step that imports the whole engine
with `gi` forced unavailable. The sole exception is `harmony.tasks` (the GTK
main-loop bridge), which may use `GLib` but only inside function bodies so it
still imports headless — enforced by its own test.

## Consequences

- The engine is provably importable on a box with no GTK. When the web/Android
  work starts, `tasks.py` is the one module that changes (its GLib bridge
  becomes a job queue / async runtime); everything else moves unchanged.
- Frontends depend on the engine *interface*, which a future HTTP client can
  satisfy identically — the precondition for federation.
- A real cost was paid to establish this: moving provider construction off the
  GTK main loop (correct for the fence) once stranded pages that were built
  before providers existed. The fence is right; the lesson is that changes which
  honor it still need their frontend consequences checked.
