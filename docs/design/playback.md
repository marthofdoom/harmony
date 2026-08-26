# Playback: play-to-device, not in-app decode

**Status: Device control — Built & verified. Play-to-device (passive relay) —
Built; end-to-end verification against real hardware pending.**

In-app audio playback was deprioritized early for a real reason: decoding YT
Music or Qobuz streams in-process is a licensing/ToS gray area and technically
fiddly. The better answer for a hub is **play to a device** — hand audio to a
hardware renderer and let *it* do the playback. This fits how people actually
listen (to speakers, not to a laptop).

## What exists today (0.5.0)

The **control plane** is built: `harmony/playback/` (engine, headless) plus a
**Devices** page in the UI.

- `PlaybackDevice` interface (`playback/base.py`): device info, transport
  (play-url/pause/resume/stop/next/prev), volume/mute, status poll.
- `WiiMDevice` (`playback/wiim.py`): the LinkPlay on-LAN HTTP API
  (`http://<host>/httpapi.asp?command=…`), with an http→https sticky fallback
  for newer self-signed firmware, hex-decoded now-playing metadata, and ms→s
  normalization.
- `discover_wiim` (`playback/discovery.py`): best-effort SSDP discovery that
  never raises.
- The Devices page adds/discovers renderers and drives transport, volume, mute,
  and now-playing — every call off the main loop per the threading rule.

Play-to-device is now wired too: a **Play on Device** action on search results
resolves a stream, registers it with the relay, and tells the selected device to
play the relay URL (`AppState.play_track_on_device`). `MusicProvider.resolve_stream`
returns a `StreamSource` (Qobuz via signed `track/getFileUrl` → FLAC; YouTube via
yt-dlp → the AAC/M4A itag 140), and `RelayServer` (`playback/relay.py`) does the
byte-forwarding. What remains is real-hardware verification and extending the
action beyond search (playlists/library).

## Play-to-device: the passive-relay design

The chosen method is a **passive relay**, not a redirect. Harmony runs a small
local HTTP endpoint; the device plays *from Harmony*
(`setPlayerCmd:play:http://harmony-host:port/play/<id>`). On fetch, Harmony
opens the freshly-resolved provider stream and **forwards the bytes** — no
transcode. The device only ever talks to Harmony on the LAN; it never touches
the provider CDN.

- **Why relay beats a 302 redirect.** In a redirect the *device* fetches the CDN
  directly and is subject to the provider's IP/header/fingerprint URL locking
  (googlevideo URLs are IP-bound). In the relay, *Harmony* fetches the CDN and
  can satisfy whatever the URL demands, so that fragility disappears. More robust
  for YouTube.
- **Codec is capability negotiation, not transcoding.** Because Harmony resolves
  the source and knows the target, it selects the matching source
  *representation* per device — YouTube exposes multiple itags (AAC/M4A `140`
  for a WiiM, Opus, …); Qobuz `track/getFileUrl` returns FLAC/MP3. This stays
  passive (selection, zero CPU). Ladder: **select (default) → transcode with
  ffmpeg (rare last resort)**, needed only when device capabilities and source
  representations don't intersect.
- **Range requests.** The relay forwards HTTP `Range` through to the CDN and
  relays partial responses (renderers send `Range` for seek/buffer).
- **Posture.** Relaying bytes crosses the "does not stream audio" line in the
  README — Harmony handles the audio, the same architecture class as Music
  Assistant (Apache-2.0; a directly-applicable reference, GPL-3.0-compatible
  one-way, attribute it). Update the disclaimer to "relays your own entitled
  stream from the service to your own device" when this lands.

## The relay is also a federation primitive

The same endpoint that feeds a WiiM can feed *another instance*, so a
credential-less spoke plays by pulling from the trusted credential-holding
instance — playback stops being credential-gated at the edge, mirroring how
[federation](federation.md) routes control.

**Scope boundary:** federation assumes a *reachable* network. Cross-site
reachability is the user's VPN (Tailscale/WireGuard), not Harmony's job — we do
not build NAT traversal or overlay networking. With the network flat, the
credential-holder is directly reachable, so relay **direct, shortest path**;
chained relays shrink to a rare edge case (a node that can reach the source but
not the device, or vice versa) rather than a reachability mechanism. Geo-routing
around territorial blocks is out of scope as a feature — that too is a
consequence of where the fetching instance's egress sits, which the user
controls.

## Targets beyond WiiM

- **UPnP/DLNA** media renderers generally — a second `PlaybackDevice` backend.
- **AirPlay 2, Chromecast** — supported by many of the same devices.

The `PlaybackDevice` interface is provider-agnostic and lives in the engine
(headless), not the UI — it is control-plane, and a federated/headless instance
must be able to drive playback too. See
[ADR 0003](../decisions/0003-flatpak-for-webkit.md) for packaging that also
unblocks bundling native playback/transcode libs later.
