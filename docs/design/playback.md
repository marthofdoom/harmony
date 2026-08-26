# Playback: play-to-device, not in-app decode

**Status: Idea / direction.** No code yet.

In-app audio playback was deprioritized early for a real reason: playing YT
Music or Qobuz streams by decoding them in-process is a licensing/ToS gray area
and technically fiddly (yt-dlp for YT, paid-tier stream URLs for Qobuz).

The better answer for a hub is **play to a device** — hand a stream URL or a
media item to a hardware renderer and let *it* do the playback. This sidesteps
the in-app-decode problem and fits how people actually listen (to speakers, not
to a laptop).

## Targets

- **WiiM / LinkPlay** — official on-LAN HTTP API
  (`http://<ip>/httpapi.asp?command=setPlayerCmd:play:<url>`), plus UPnP. The
  WiiM Mini is a concrete first target. See the reference notes in memory.
- **UPnP/DLNA** media renderers generally.
- **AirPlay 2, Chromecast** — supported by many of the same devices.

## Design implications

- A `playback` interface in the engine: enumerate devices, push a media item,
  transport control, poll status. Provider-agnostic; device-specific backends
  behind it (LinkPlay HTTP, UPnP, …).
- Belongs in the engine (headless), not the UI — it's control-plane, and a
  federated/headless instance must be able to drive playback too.
- Where the stream URL comes from per provider is the open question, and where
  the ToS line actually sits — to be worked out per provider before building.

See [ADR 0003](../decisions/0003-flatpak-for-webkit.md) for the packaging that
also unblocks bundling any native playback libs later.
