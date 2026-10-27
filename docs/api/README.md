# HTTP API documentation

Harmony's server (`harmony serve`, also embedded in the desktop app) exposes a
JSON/HTTP API under `/api/`. The web client, the Android app and other Harmony
instances use it; so can your own tools. Every `/api/*` request is gated by the
instance's optional **personal key** (`X-Harmony-Key` header or `?key=`
query parameter); see [Auth](game-source.md#auth) for the details.

| Document | What it covers |
|----------|----------------|
| [`game-source.md`](game-source.md) | **Game source API** (`/api/game/*`): use Harmony as a proxy music source for a game engine (profiles per music type, shuffled bags, PCM WAV downloads with Range support, caching). Works with no accounts via YouTube Music's public catalogue. |

The rest of the API is internal to Harmony's own clients and not yet documented
here; for its shape see the headless-server design in
[`../design/headless-server.md`](../design/headless-server.md) and the
engine/frontend contract in [`../ARCHITECTURE.md`](../ARCHITECTURE.md).
