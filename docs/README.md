# Harmony documentation

Harmony is a cross-service music hub: aggregate streaming providers into one
library, manage and sync playlists across them, and (planned) play to devices.
It ships as standalone apps that can also federate — point one at another and it
uses that instance as a backend.

This directory is the map. Start with whichever matches what you're doing.

## Architecture (how it's built)

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — **module contracts.** The engine's
  layers and the interface each must satisfy. Referenced directly from source
  docstrings; treat it as the binding contract, not prose.
- [`architecture/layering.md`](architecture/layering.md) — the engine/frontend
  boundary and why the engine must never import GTK. The rule the whole
  multi-client plan rests on.

## Design (why it's built this way)

- [`design/product-vision.md`](design/product-vision.md) — the long-range
  target: a Music-Assistant-class hub done well, multi-client and federated.
- [`design/federation.md`](design/federation.md) — instances pointed at one
  another; "login via another instance"; how this solves credential custody.
- [`design/auth.md`](design/auth.md) — authentication per service, the Qobuz
  token/WebKit story, and where credentials live.
- [`design/playback.md`](design/playback.md) — play-to-device (WiiM, UPnP)
  rather than in-app decode.

## Operations

- [`packaging.md`](packaging.md) — building the Flatpak; where WebKit comes from.
- [`roadmap.md`](roadmap.md) — the sequenced plan and current status.

## Decisions

- [`decisions/`](decisions/) — Architecture Decision Records for the calls that
  shaped the codebase, each with the context and the tradeoff. Read these to
  understand *why* something is the way it is before changing it.

## Status legend

Docs mark maturity explicitly, because much of this is planned rather than done:
**Built & verified** · **Built, unverified** · **Planned** · **Idea**.
