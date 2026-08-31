# Federation: instances, clients, and playback across networks

**Status: Partially shipped.** The mesh (mDNS discovery + advertise,
`src/harmony/mesh.py`), personal-key gating (`web/api.py`, `web/server.py`),
inter-instance audio routing (`web/audio_routing.py`, `/api/audio/*`), casting
(`web/cast.py`), and stream federation (`/api/resolve` + `/stream/<token>`) are
built and consumed by the desktop, web, and Android clients. This doc is the
**authoritative enumeration of federated use cases** — the map we check new work
against. **A combination that is missing from the matrix below is a bug in this
doc, not an implicit "no."** (That omission is exactly how the phone-bridge case
was missed.)

## The credential model — use vs. copy

The **personal key** is the trust boundary. How a node relates to credentials
depends on whether it's a *light client* or a *full instance*:

- **Light clients** (phone, web) **use** the hub's credentials over the API and
  never custody them. A phone that loses its session exposes nothing about the
  user's Qobuz account.
- **Full instances** (a second desktop, a headless server) **copy** the
  credentials from a key-matching peer, becoming *independent* credential
  holders. This is what makes a fresh headless `.deb` server usable — it can't
  do the browser/OAuth onboarding dance, so on first boot, if it has the
  matching personal key but no accounts, it pulls them from a peer.

The mechanism: `GET /api/credentials/export` serves the secrets + provider
settings + the YouTube auth file **only** to a caller presenting the matching
key (and never from an instance with no key set). `POST /api/credentials/adopt`
pulls from a named peer (or auto-picks a key-matching one); a full instance also
tries this automatically when its key is set or on startup. Because a *copy*
crosses the network, keep the mesh on a trusted network/tailnet.

See [auth](auth.md) and
[ADR 0004](../decisions/0004-federation-for-credential-custody.md).

## Roles a node can play

Federation is easier to reason about as **roles**, not device types. Any node may
play several. Naming them is how we avoid unnamed-role misses (the phone-bridge
was an unnamed "client as relay").

| Role | What it does | desktop | headless | web | phone |
|------|--------------|:---:|:---:|:---:|:---:|
| **Credential holder** | stores tokens, resolves service streams | ✅ | ✅ | — | — |
| **Player (sink)** | decodes + plays audio itself | ✅ | ✅ (has audio) | ✅ | ✅ |
| **Renderer controller** | discovers + drives WiiM/UPnP/Chromecast | ✅ | ✅ | via hub | ⬚ *planned* |
| **Relay / bridge** | re-serves a stream onto *another* network | ✅ (hub relay) | ✅ | — | ⬚ *planned* |
| **Audio source** | emits its own live output to others | ✅ | ✅ | — | ⬚ *planned* |

## The federated playback matrix

Axes — **Source**: `T` = hub-resolved service track, `S` = a machine's live
system output. **Where**: initiator / another instance's sink / a renderer on the
hub's LAN / a renderer on the *client's* LAN. **Topology**: `LAN` (one network),
`VPN` (client on a tunnel to the hub but physically on a foreign LAN),
`NAT` (hub in a container / behind a proxy).

### Track playback (source = hub `/stream/<token>`)

| Case | Topology | Status |
|------|----------|--------|
| Client plays a hub track on itself | LAN | **Supported** |
| …client on VPN / foreign LAN | VPN | **Supported** for playback; **discovery Missing** (mDNS is LAN-only — needs a saved/typed URL) |
| …hub behind reverse proxy / TLS | NAT | **Partial** — no TLS in the server; `?key=` in the stream URL leaks the key to proxy logs |
| Cast a hub track to a device **on the hub's LAN** | LAN / VPN-control | **Supported** (even when the initiator is remote — data never touches it) |
| Cast from a **containerized** hub | NAT | **Broken** — cast spins a second relay on an ephemeral port the container never publishes |
| Cast a hub track to a device on the **client's** LAN (phone-bridge) | VPN | **Missing** — hub can't see or reach the foreign-LAN device; the client has no relay/renderer stack |
| A spoke instance casts the home hub's track to *its* local device | VPN | **Missing** — cast resolves only via *local* providers; can't cast a peer's `/stream` URL |

### Live audio routing (source = system output)

| Case | Topology | Status |
|------|----------|--------|
| Route hub A's output ↔ hub B's speakers | LAN | **Supported** (ROC preferred; RTP fallback) |
| Phone plays the hub's live output on itself | LAN | **Supported (alpha)** — RTP → AudioTrack |
| …over VPN | VPN | **Partial/fragile** — uncompressed ~1.4 Mbps UDP, no FEC, fixed port, no NAT keepalive; fails silently if the hub can't send UDP inbound |
| Two hubs on **different LANs** route to each other | VPN | **Missing in practice** — the `audio_route` API would carry over a tunnel, but nothing produces a cross-LAN peer (mesh is mDNS-only; no manual-peer entry) |
| Hub output → a **cast device** (WiiM/Chromecast) | any | **Missing** — routing targets only another instance's PipeWire sink |
| **Phone as a source** (phone audio → hub speakers) | any | **Missing** — phone is sink-only |
| One source → **multiple** receivers (multi-room) | any | **Missing** — one sender + one receiver per instance, fixed ports |

## Bridging: playing across a network boundary

The class the phone-bridge belongs to. **Revised doctrine:** *one hop of
chaining is a first-class supported shape*, replacing the old "chained relays are
a rare edge case" — for a roaming phone, the friend's-house / office / hotel case
is the **normal** shape, not the exception.

- **Phone-bridge** — the phone pulls `/stream/<token>` from the hub over the
  tunnel and **re-serves it on its own LAN** (a client-side mini-relay), then
  discovers and drives a local renderer (WiiM/UPnP, or Chromecast via Android's
  cast SDK). Needs: phone-side renderer discovery + an embedded HTTP relay +
  renderer control.
- **Spoke-bridge** — a full instance on the remote LAN casts a **remote source**.
  The smallest new capability in the whole audit: `POST /api/devices/<host>/play`
  accepting `{"source_url": ..., "key": ...}` (or a `peer` ref) so the caster can
  relay a peer's `/stream/<token>` instead of resolving locally. The credential
  custody stays put; only a *URL* crosses. This is the primitive
  [playback.md](playback.md) promised and never built.

## Discovery beyond the LAN

Discovery today conflates *adjacency* (same LAN) with *reachability* (can route a
packet). The flagship client is a phone **defined by changing networks**, so:

- **Manual peer registry** — `GET/POST /api/peers` (name, base URL, key-verified),
  merged with mDNS results everywhere peers are listed. Without it, tailnet peers
  don't exist and the working `audio_route` API is unreachable from the UI.
- **Advertise all addresses**, not the single default-route IP (`_primary_ipv4()`
  can pick the wrong iface on a multi-homed / VPN'd host).
- **Optional gossip** — peers share peer lists, so a client learns of the tailnet
  hub from the LAN hub.

## Reachability is asymmetric — test it, don't assume it

Routing derives the return address from the local egress iface and assumes the
peer can send UDP back; cast assumes the device can fetch the hub; nothing asks
"can X reach Y?", so every cross-topology failure is a timeout or silence rather
than a diagnosis. Design intent:

- Every peer/device record carries **who can reach it**, *probed* (a `/healthz`
  from each candidate origin), not assumed.
- Every play/route/cast picks a data path from **actual** reachability, and
  errors **name the failing edge** ("hub cannot reach 192.168.4.20; nearest
  bridge: this phone").
- **Control-plane success ≠ data-plane success.** `audio_send`/`route`/`cast`
  must confirm audio actually flows (receiver packet counters in
  `/api/audio/status`), not just that the command was accepted.

## Sessions, not singletons

Routes/casts today are per-instance singletons: one sender + one receiver, fixed
ports (10001-3 / 5004), no owner — so any client's `audio_stop` tears down
whoever's session, and a second `playHere` silently steals the sender. Intended:

- Routes and casts get **IDs, an owner, and a status** (incl. data-flowing).
- **Multiple concurrent sessions** with negotiated ports (multi-room).
- The **initiator dictates the transport to *both* halves** — fixes the
  ROC/RTP-mismatch bug where a ROC-less receiver is paired with a ROC sender and
  hears silence.

## Security across network classes

"The network is the security boundary" was written for one LAN; leaving the LAN
voids it. Intended posture:

- Distinguish **LAN-trust vs tunnel vs open internet**; TLS story, or state
  "tailnet-only, ever" outright.
- Kill `?key=` in URLs (logged everywhere) in favor of the header.
- The UDP audio plane (`module-rtp-recv` / `roc-recv` on `0.0.0.0`) accepts from
  anyone — bind it to the tunnel iface, or tag streams.

## Known gaps / bugs (from the federated audit)

Tracked so they don't get re-lost:

1. `route()` doesn't forward `transport` to the peer → ROC/RTP mismatch between
   heterogeneous instances plays silence. *(Being fixed.)*
2. Containerized cast uses an unpublished ephemeral relay port — should reuse the
   main server's `/stream/`.
3. Stream-token TTL (30 min) outlives provider URLs (~minutes) → late seeks 403,
   worst for slow/remote clients.
4. `_primary_ipv4()` advertises one address; a multi-homed host should advertise
   all.
5. The whole **bridge class** (phone-bridge, spoke-bridge) — the largest gap; see
   above.

## What it requires from the engine

The engine presents a **stable API** identical in-process or over HTTP; clients
depend on the interface, not a local implementation. The
[layering rule](../architecture/layering.md) is the precondition. See
[headless-server](headless-server.md) for the server/mesh shape and
[playback](playback.md) for the relay primitive.
