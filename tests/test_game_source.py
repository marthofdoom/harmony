"""Game-source API (harmony.game_source): profiles, bags, source routing, the WAV
pipeline (real ffmpeg on generated audio), Range/HEAD, caching and the key gate.

The HTTP tests boot the real ``HarmonyHTTPRequestHandler`` with a fake engine
whose providers are in-memory, so routing, headers and bytes are the real thing.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import shutil
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from harmony import config as config_module
from harmony import game_source as gs
from harmony.models import SearchResults, Service, StreamSource, Track
from harmony.web import server as web_server
from harmony.web.server import HarmonyHTTPRequestHandler

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


# -- generated audio ---------------------------------------------------------------


def _tone(path: Path, *, seconds: float = 2.0, rate: int = 44100, channels: int = 2,
          codec: str = "pcm_s16le", sample_fmt: str | None = None, freq: int = 440,
          lead_silence: float = 0.0, tail_silence: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    src = f"sine=f={freq}:d={seconds}:r={rate}"
    af = []
    if lead_silence:
        af.append(f"adelay={int(lead_silence * 1000)}:all=1")
    if tail_silence:
        af.append(f"apad=pad_dur={tail_silence}")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-ac", str(channels)]
    if af:
        cmd += ["-af", ",".join(af)]
    if sample_fmt:
        cmd += ["-sample_fmt", sample_fmt]
    subprocess.run([*cmd, "-c:a", codec, str(path)], check=True)
    return path


def _trk(tid: str, title: str, artist: str = "Band", service: Service = Service.LOCAL,
         genre: str | None = None, duration: int = 60) -> Track:
    return Track(id=tid, title=title, service=service, artists=[artist], album="Alb",
                 duration_s=duration, genre=genre, quality={"label": "FLAC"})


# -- fake engine -------------------------------------------------------------------


class FakeProv:
    def __init__(self, service: Service, tracks: list[Track]) -> None:
        self.service = service
        self.tracks = tracks
        self.calls: list[tuple] = []
        self.is_authenticated = True
        self.on = True
        self.playlists: dict[str, list[Track]] = {}
        self.albums: dict[str, list[Track]] = {}

    def active(self) -> bool:
        return self.on

    def search(self, query, *, kinds=("tracks",), limit=25):
        self.calls.append(("search", query))
        q = query.lower().removesuffix(" music")  # genre selectors search "<genre> music"
        return SearchResults(tracks=[t for t in self.tracks
                                     if q in t.title.lower() or q in (t.genre or "").lower()][:limit])

    def browse_view(self, view, *, genre=None, limit=200, order="artist", **_kw):
        self.calls.append(("browse", view, genre))
        rows = [t for t in self.tracks if (genre or "").lower() == (t.genre or "").lower()][:limit]
        return {"songs": rows, "total": len(rows)}

    def get_track(self, tid):
        for t in self.tracks:
            if t.id == tid:
                return t
        raise KeyError(tid)

    def get_playlist_tracks(self, pid):
        self.calls.append(("playlist", pid))
        return list(self.playlists[pid])

    def get_album_tracks(self, aid):
        self.calls.append(("album", aid))
        return list(self.albums[aid])

    def get_artist_top_tracks(self, aid, *, limit=20):
        self.calls.append(("artist", aid))
        return [t for t in self.tracks if t.artists == [aid]][:limit]


class FakeEngine:
    def __init__(self, tmp: Path) -> None:
        self.key: str | None = None
        self.files: dict[str, Path] = {}      # (any service) track id -> audio file
        self.lib = FakeProv(Service.LOCAL, [])
        self.qobuz = FakeProv(Service.QOBUZ, [])
        self.yt: FakeProv | None = None
        self.preferred = True
        self.resolve_calls: list[tuple[str, str]] = []
        self.resolve_delay = 0.0
        self.resolve_error: Exception | None = None
        self.alias: dict[tuple[str, str], tuple[str, str]] = {}  # streaming id -> Library copy
        self.alias_unavailable = False   # the Library copy fails to resolve -> falls back to the stream
        self.tmp = tmp

    # surface used by game_source
    def check_key(self, provided):
        return not self.key or provided == self.key

    def _library_provider(self):
        return self.lib

    def _ensure_providers(self):
        provs = {Service.LOCAL: self.lib, Service.QOBUZ: self.qobuz}
        if self.yt is not None:
            provs[Service.YTMUSIC] = self.yt
        return provs

    def _plock(self, _service):
        return contextlib.nullcontext()

    def _library_preferred(self):
        return self.preferred

    def _match_library(self, items):
        out = []
        for d in items:
            hit = next((t for t in self.lib.tracks if t.title.lower() == (d.get("title") or "").lower()), None)
            out.append(hit.id if hit else None)
        return out

    def _prefer_library(self, service, tid):
        return self.alias.get((service, tid), (service, tid))

    def _resolve_preferring_library(self, service, tid):
        self.resolve_calls.append((service, tid))
        if self.resolve_delay:
            time.sleep(self.resolve_delay)
        if self.resolve_error:
            raise self.resolve_error
        svc, real = self._prefer_library(service, tid)
        if (svc, real) != (service, tid) and self.alias_unavailable:
            svc, real = service, tid
            self.alias.pop((service, tid), None)  # the stale match is forgotten
        return svc, real, StreamSource(url=self.files[real].as_uri(), mime_type="audio/x")

    def _source_for(self, service, tid):
        return {"service": service, "label": service, "instance": None, "own": service == "local"}

    def game_request(self, method, parts, query, body, headers):
        return gs.game_source_for(self).handle(method, parts, query, body, headers)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(config_module, "cache_dir", lambda: tmp_path / "cache")
    eng = FakeEngine(tmp_path)
    monkeypatch.setattr(web_server, "_engine", eng)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), HarmonyHTTPRequestHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://{}:{}".format(*httpd.server_address)
    yield SimpleNamespace(eng=eng, url=url, tmp=tmp_path, game=gs.game_source_for(eng))
    httpd.shutdown()
    httpd.server_close()


def call(env, method, path, body=None, headers=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(env.url + path, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - loopback test server
            payload = r.read()
            status, hdrs = r.status, r.headers
    except urllib.error.HTTPError as exc:
        payload, status, hdrs = exc.read(), exc.code, exc.headers
    if raw:
        return status, hdrs, payload
    return status, (json.loads(payload) if payload else None)


PROFILE = {"sources": "library", "types": {"explore": {"genre": "Ambient"},
                                            "combat": {"genre": "Metal"},
                                            "default": {"query": "calm"}}}


_SPECS = [("a1", "Drift", "Ambient", {}), ("a2", "Haze", "Ambient", {"freq": 330}),
          ("a3", "Fog calm", "Ambient", {"channels": 1}),
          ("m1", "Crush", "Metal", {"rate": 96000, "codec": "flac", "sample_fmt": "s32"}),
          ("m2", "Smash", "Metal", {"rate": 48000})]


@pytest.fixture(scope="module")
def audio_files(tmp_path_factory):
    d = tmp_path_factory.mktemp("game-audio-src")
    return {tid: _tone(d / f"{tid}.{'flac' if kw.get('codec') == 'flac' else 'wav'}", seconds=2.0, **kw)
            for tid, _t, _g, kw in _SPECS}


@pytest.fixture
def seed(env, audio_files):
    """Library: 3 Ambient, 2 Metal (one a 24/96 FLAC), real audio files behind them."""
    for tid, title, genre, _kw in _SPECS:
        env.eng.files[tid] = audio_files[tid]
        env.eng.lib.tracks.append(_trk(tid, title, genre=genre))
    return env


# -- profile validation --------------------------------------------------------------


def test_profile_defaults_filled_in():
    p = gs.validate_profile({"types": {"explore": {"genre": "Ambient"}}})
    assert p == {"sources": "all", "types": {"explore": {"genre": "Ambient"}},
                 "format": {"rate": 48000, "bits": 16, "channels": 2},
                 "normalize": False, "trim_silence": False}


def test_profile_accepts_every_selector_and_unions():
    ref = {"service": "qobuz", "id": "123"}
    p = gs.validate_profile({"sources": "all", "types": {
        "a": [{"genre": "x"}, {"playlist": ref}, {"album": ref}, {"artist": {"service": "local", "id": "ab12@h:1"}},
              {"query": "dark ambient"}]}, "format": {"rate": 44100, "bits": 24, "channels": 1},
        "normalize": True, "trim_silence": True})
    assert len(p["types"]["a"]) == 5 and p["format"]["bits"] == 24 and p["normalize"]


@pytest.mark.parametrize("bad", [
    [],
    {"types": {}},
    {"types": {"a": {"genre": "x"}}, "bogus": 1},
    {"types": {"a": {"genre": "x"}}, "sources": "everything"},
    {"types": {"a": {"genre": "x"}}, "format": {"rate": 12345}},
    {"types": {"a": {"genre": "x"}}, "format": {"bits": 32}},
    {"types": {"a": {"genre": "x"}}, "format": {"channels": 6}},
    {"types": {"a": {"genre": "x"}}, "format": {"rate": True}},
    {"types": {"a": {"genre": "x"}}, "format": {"codec": "mp3"}},
    {"types": {"a": {"genre": "x"}}, "normalize": "yes"},
    {"types": {"a": {"genre": "x"}}, "trim_silence": 1},
    {"types": {"bad name": {"genre": "x"}}},
    {"types": {"../a": {"genre": "x"}}},
    {"types": {"a": {}}},
    {"types": {"a": {"genre": "x", "query": "y"}}},
    {"types": {"a": {"mood": "x"}}},
    {"types": {"a": {"genre": ""}}},
    {"types": {"a": {"genre": 5}}},
    {"types": {"a": []}},
    {"types": {"a": [[{"genre": "x"}]]}},
    {"types": {"a": {"playlist": {"service": "spotify", "id": "1"}}}},
    {"types": {"a": {"playlist": {"service": "qobuz"}}}},
    {"types": {"a": {"playlist": {"service": "qobuz", "id": "../../etc"}}}},
    {"types": {"a": {"playlist": {"service": "qobuz", "id": "1", "x": 1}}}},
    {"types": {"a": {"album": "qobuz:1"}}},
])
def test_profile_rejects_invalid(bad):
    with pytest.raises(gs.GameError) as exc:
        gs.validate_profile(bad)
    assert exc.value.status == 400


@pytest.mark.parametrize("name", ["", "a/b", "..", ".x", "x" * 65, "a b", 5, None])
def test_profile_name_rejected(name):
    with pytest.raises(gs.GameError):
        gs.validate_name(name)


# -- the bag -----------------------------------------------------------------------------


def test_bag_never_repeats_within_a_cycle_and_reshuffles():
    keys = [("local", str(i)) for i in range(7)]
    bag = gs.Bag(random.Random(1))
    bag.sync(keys)
    orders = []
    for _ in range(5):
        cycle = bag.take(7, consume=True)
        assert sorted(cycle) == sorted(keys)  # each exactly once
        orders.append(cycle)
    assert len({tuple(o) for o in orders}) > 1  # reshuffled between cycles


@pytest.mark.parametrize("seed", range(40))
def test_bag_no_immediate_repeat_across_the_boundary(seed):
    bag = gs.Bag(random.Random(seed))
    bag.sync([("local", "a"), ("local", "b")])
    seq = [bag.take(1, consume=True)[0] for _ in range(40)]
    assert all(x != y for x, y in zip(seq, seq[1:], strict=False))


def test_bag_peek_does_not_consume_and_matches_the_next_consume():
    bag = gs.Bag(random.Random(3))
    bag.sync([("local", str(i)) for i in range(4)])
    peeked = bag.take(6, consume=False)  # spans into the next cycle
    assert bag.take(6, consume=False) == peeked
    assert bag.take(2, consume=True) == peeked[:2]
    assert bag.take(4, consume=True) == peeked[2:6]


def test_bag_single_member_repeats_and_empty_is_empty():
    bag = gs.Bag()
    bag.sync([("local", "only")])
    assert bag.take(3, consume=True) == [("local", "only")] * 3
    empty = gs.Bag()
    empty.sync([])
    assert empty.take(2, consume=True) == []


def test_bag_sync_keeps_cycle_and_adds_removes():
    bag = gs.Bag(random.Random(5))
    bag.sync([("local", "a"), ("local", "b"), ("local", "c")])
    first = bag.take(1, consume=True)[0]
    bag.sync([("local", "a"), ("local", "b"), ("local", "c"), ("local", "d")])
    rest = bag.take(3, consume=True)
    assert first not in rest and len(rest) == 3 and ("local", "d") in rest
    bag.sync([("local", "d")])
    assert bag.take(5, consume=True) == [("local", "d")] * 5


# -- Range parsing -----------------------------------------------------------------------


@pytest.mark.parametrize(("hdr", "want"), [
    (None, None), ("bytes=0-9", (0, 9)), ("bytes=5-", (5, 99)), ("bytes=-10", (90, 99)),
    ("bytes=90-500", (90, 99)), ("bytes=100-", "bad"), ("bytes=50-10", None), ("bytes=10-5", None), ("bytes=-0", "bad"),
    ("bytes=0-1,5-6", None), ("items=0-1", None), ("bytes=", None), ("garbage", None)])
def test_parse_range(hdr, want):
    assert gs.parse_range(hdr, 100) == want


# -- profiles over HTTP ------------------------------------------------------------------------


def test_profile_crud_persists_to_settings(env):
    assert call(env, "GET", "/api/game/profiles") == (200, {"profiles": []})
    status, body = call(env, "PUT", "/api/game/profiles/skyrim", PROFILE)
    assert status == 200 and body["name"] == "skyrim" and body["format"]["rate"] == 48000
    assert call(env, "GET", "/api/game/profiles/skyrim")[1]["types"]["combat"] == {"genre": "Metal"}
    assert [p["name"] for p in call(env, "GET", "/api/game/profiles")[1]["profiles"]] == ["skyrim"]
    stored = json.loads((env.tmp / "settings.json").read_text())
    assert "skyrim" in stored["game_profiles"]
    assert call(env, "DELETE", "/api/game/profiles/skyrim")[1]["deleted"] == "skyrim"
    assert call(env, "GET", "/api/game/profiles/skyrim")[0] == 404
    assert call(env, "DELETE", "/api/game/profiles/skyrim")[0] == 404
    assert json.loads((env.tmp / "settings.json").read_text())["game_profiles"] == {}


def test_profile_put_validates_strictly_and_names(env):
    status, body = call(env, "PUT", "/api/game/profiles/p", {"types": {"a": {"genre": "x"}}, "oops": 1})
    assert status == 400 and body["code"] == "invalid_profile" and "oops" in body["error"]
    assert call(env, "PUT", "/api/game/profiles/bad%20name", PROFILE)[0] == 400
    assert call(env, "PUT", "/api/game/profiles/p", {"name": "other", **PROFILE})[0] == 400
    assert call(env, "PUT", "/api/game/profiles/p", {"name": "p", **PROFILE})[0] == 200
    assert call(env, "GET", "/api/game/profiles/missing")[0] == 404


def test_profile_save_survives_a_stale_settings_object(env):
    from harmony.config import Settings

    stale = Settings.load()
    call(env, "PUT", "/api/game/profiles/skyrim", PROFILE)
    stale.last_page = "library"
    stale.save()  # the desktop saving an unrelated change must not drop the profile
    assert "skyrim" in json.loads((env.tmp / "settings.json").read_text())["game_profiles"]


# -- next: source routing -----------------------------------------------------------------------


def test_next_library_mode_ignores_streaming(env, seed):
    env.eng.qobuz.tracks.append(_trk("q1", "Qobuz Ambient", service=Service.QOBUZ, genre="Ambient"))
    call(env, "PUT", "/api/game/profiles/g", {"sources": "library",
                                              "types": {"explore": [{"genre": "Ambient"}, {"query": "Qobuz"}]}})
    seen = set()
    for _ in range(6):
        body = call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore"})[1]
        seen.add(body["tracks"][0]["id"])
    assert seen == {"a1", "a2", "a3"}
    assert env.eng.qobuz.calls == []


def test_next_all_mode_unions_streaming_and_prefers_library_copy(env, seed):
    env.eng.qobuz.tracks += [_trk("q1", "Qobuz Ambient", service=Service.QOBUZ, genre="Ambient"),
                             _trk("q2", "Drift", service=Service.QOBUZ, genre="Ambient")]  # twin of a1
    call(env, "PUT", "/api/game/profiles/g", {"sources": "all", "types": {"explore": {"genre": "Ambient"}}})
    body = call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore", "count": 10})[1]
    ids = [t["id"] for t in body["tracks"]]
    assert body["sources"] == "all" and body["pool_size"] == 4
    assert set(ids) == {"a1", "a2", "a3", "q1"}  # the Drift twin collapses onto the Library copy
    q1 = next(t for t in body["tracks"] if t["id"] == "q1")
    assert q1["service"] == "qobuz" and q1["audio_url"].startswith("/api/game/audio/qobuz/q1.wav?")
    assert ("search", "Ambient music") in env.eng.qobuz.calls


def test_next_request_sources_overrides_profile(env, seed):
    env.eng.qobuz.tracks.append(_trk("q1", "Qobuz Ambient", service=Service.QOBUZ, genre="Ambient"))
    call(env, "PUT", "/api/game/profiles/g", {"sources": "library", "types": {"explore": {"genre": "Ambient"}}})
    body = call(env, "POST", "/api/game/next",
                {"profile": "g", "type": "explore", "sources": "all", "count": 10})[1]
    assert "q1" in {t["id"] for t in body["tracks"]}
    assert call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore", "sources": "x"})[0] == 400


def test_streaming_swapped_for_library_when_streamed_copy_picked(env, seed):
    env.eng.qobuz.playlists["pl"] = [_trk("q9", "Crush", service=Service.QOBUZ)]  # Library has "Crush" (m1)
    call(env, "PUT", "/api/game/profiles/g", {"sources": "all", "types": {
        "x": {"playlist": {"service": "qobuz", "id": "pl"}}}})
    t = call(env, "POST", "/api/game/next", {"profile": "g", "type": "x"})[1]["tracks"][0]
    assert t["id"] == "m1" and t["service"] == "local" and t["source"]["from_library"] is True
    env.eng.preferred = False  # setting off: the streamed copy stays
    call(env, "PUT", "/api/game/profiles/g", {"sources": "all", "types": {
        "y": {"playlist": {"service": "qobuz", "id": "pl"}}}})
    t = call(env, "POST", "/api/game/next", {"profile": "g", "type": "y"})[1]["tracks"][0]
    assert (t["service"], t["id"]) == ("qobuz", "q9")


def test_playlist_album_artist_selectors_and_library_mode_skips_streaming_refs(env, seed):
    env.eng.qobuz.playlists["pl"] = [_trk("q1", "P1", service=Service.QOBUZ)]
    env.eng.qobuz.albums["al"] = [_trk("q2", "A1", service=Service.QOBUZ)]
    env.eng.lib.albums["la"] = [env.eng.lib.tracks[0]]
    env.eng.qobuz.tracks.append(_trk("q3", "T3", artist="Zed", service=Service.QOBUZ))
    sel = {"p": {"playlist": {"service": "qobuz", "id": "pl"}},
           "a": {"album": {"service": "qobuz", "id": "al"}},
           "z": {"artist": {"service": "qobuz", "id": "Zed"}},
           "l": {"album": {"service": "local", "id": "la"}}}
    call(env, "PUT", "/api/game/profiles/all", {"sources": "all", "types": sel})
    call(env, "PUT", "/api/game/profiles/lib", {"sources": "library", "types": sel})
    for t, want in (("p", "q1"), ("a", "q2"), ("z", "q3"), ("l", "a1")):
        assert call(env, "POST", "/api/game/next", {"profile": "all", "type": t})[1]["tracks"][0]["id"] == want
    assert call(env, "POST", "/api/game/next", {"profile": "lib", "type": "l"})[1]["tracks"][0]["id"] == "a1"
    status, body = call(env, "POST", "/api/game/next", {"profile": "lib", "type": "p"})
    assert status == 404 and body["code"] == "no_tracks"


def test_unknown_type_uses_default_else_404(env, seed):
    call(env, "PUT", "/api/game/profiles/g", PROFILE)
    body = call(env, "POST", "/api/game/next", {"profile": "g", "type": "weird"})[1]
    assert body["type"] == "default" and body["requested_type"] == "weird"
    assert body["tracks"][0]["id"] == "a3"  # query "calm" -> "Fog calm"
    assert call(env, "POST", "/api/game/next", {"profile": "g", "type": "EXPLORE"})[1]["type"] == "explore"
    call(env, "PUT", "/api/game/profiles/nodef", {"types": {"explore": {"genre": "Ambient"}}})
    status, body = call(env, "POST", "/api/game/next", {"profile": "nodef", "type": "weird"})
    assert status == 404 and body["code"] == "unknown_type" and "explore" in body["error"]
    assert call(env, "POST", "/api/game/next", {"profile": "ghost", "type": "x"})[1]["code"] == "unknown_profile"


@pytest.mark.parametrize("bad", [
    {"profile": "g"}, {"type": "explore"}, {"profile": "g", "type": "explore", "count": 0},
    {"profile": "g", "type": "explore", "count": 11}, {"profile": "g", "type": "explore", "count": "2"},
    {"profile": "g", "type": "explore", "count": True}, {"profile": "g", "type": "ex plore"},
    {"profile": "g", "type": "explore", "extra": 1}, {"profile": "g", "type": "explore", "peek": "no"}])
def test_next_validates_the_request(env, seed, bad):
    call(env, "PUT", "/api/game/profiles/g", PROFILE)
    assert call(env, "POST", "/api/game/next", bad)[0] == 400


def test_next_bag_over_http_and_peek(env, seed):
    call(env, "PUT", "/api/game/profiles/g", PROFILE)
    peek = call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore", "count": 3, "peek": True})[1]
    again = call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore", "count": 3, "peek": True})[1]
    assert [t["id"] for t in peek["tracks"]] == [t["id"] for t in again["tracks"]] and peek["peek"]
    first = call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore", "count": 2})[1]
    second = call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore", "count": 1})[1]
    got = [t["id"] for t in first["tracks"] + second["tracks"]]
    assert got == [t["id"] for t in peek["tracks"]] and len(set(got)) == 3  # one full cycle
    t = first["tracks"][0]
    assert set(t) == {"service", "id", "title", "artist", "album", "duration_s", "quality", "source",
                      "audio_url"}
    assert "rate=48000&bits=16&channels=2&normalize=0&trim=0" in t["audio_url"]


def test_editing_a_profile_resets_its_bag(env, seed):
    call(env, "PUT", "/api/game/profiles/g", PROFILE)
    call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore"})
    call(env, "PUT", "/api/game/profiles/g", {"types": {"explore": {"genre": "Metal"}}})
    ids = {call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore"})[1]["tracks"][0]["id"]
           for _ in range(4)}
    assert ids == {"m1", "m2"}


# -- audio: WAV correctness ------------------------------------------------------------------------


def parse_wav(data: bytes):
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    riff, = struct.unpack("<I", data[4:8])
    assert riff + 8 == len(data), "RIFF size must describe the whole file"
    assert data[12:16] == b"fmt "
    fmt_size, tag, ch, rate, byte_rate, block, bits = struct.unpack("<IHHIIHH", data[16:36])
    assert (fmt_size, tag) == (16, 1)
    assert data[36:40] == b"data"
    dlen, = struct.unpack("<I", data[40:44])
    assert 44 + dlen + dlen % 2 == len(data)
    assert byte_rate == rate * block and block == ch * bits // 8 and dlen % block == 0
    return {"rate": rate, "channels": ch, "bits": bits, "frames": dlen // block, "pcm": data[44:44 + dlen]}


def peak(pcm: bytes, bits: int) -> int:
    if bits == 16:
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    else:
        samples = [int.from_bytes(pcm[i:i + 3], "little", signed=True) for i in range(0, len(pcm), 3)]
    return max(abs(s) for s in samples)


def fetch_audio(env, tid, service="local", q="rate=48000&bits=16&channels=2", **kw):
    return call(env, "GET", f"/api/game/audio/{service}/{tid}.wav?{q}", raw=True, **kw)


@pytest.mark.parametrize(("tid", "q", "rate", "bits", "ch"), [
    ("a1", "rate=48000&bits=16&channels=2", 48000, 16, 2),       # 44.1k stereo wav -> 48k
    ("m1", "rate=48000&bits=16&channels=2", 48000, 16, 2),       # 24/96 FLAC -> 48k/16
    ("m1", "rate=44100&bits=24&channels=2", 44100, 24, 2),
    ("a3", "rate=44100&bits=24&channels=1", 44100, 24, 1),       # mono source, odd-sized data
    ("a1", "rate=44100&bits=16&channels=1", 44100, 16, 1),       # downmix
    ("a1", "", 48000, 16, 2)])                                    # defaults
def test_audio_is_exact_pcm_wav(env, seed, tid, q, rate, bits, ch):
    status, hdrs, data = fetch_audio(env, tid, q=q)
    assert status == 200 and hdrs["Content-Type"] == "audio/wav"
    assert int(hdrs["Content-Length"]) == len(data) and hdrs["Accept-Ranges"] == "bytes"
    w = parse_wav(data)
    assert (w["rate"], w["bits"], w["channels"]) == (rate, bits, ch)
    assert abs(w["frames"] - rate * 2.0) <= rate * 0.05  # the source is 2.0 s
    assert peak(w["pcm"], bits) > (1000 if bits == 16 else 250000)  # real audio, not silence


def test_audio_matches_an_independent_ffprobe(env, seed):
    _s, _h, data = fetch_audio(env, "m1", q="rate=48000&bits=16&channels=2")
    out = env.tmp / "o.wav"
    out.write_bytes(data)
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(out)],
        check=True, capture_output=True, text=True).stdout)
    s = probe["streams"][0]
    assert (s["codec_name"], s["sample_rate"], s["channels"]) == ("pcm_s16le", "48000", 2)
    assert abs(float(probe["format"]["duration"]) - 2.0) < 0.05
    assert subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"],
                          capture_output=True).returncode == 0  # decodes cleanly end to end


def test_normalize_changes_level_toward_target(env, seed):
    quiet = _tone(env.tmp / "quiet.wav", seconds=6, freq=440)
    loud = env.tmp / "loud.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(quiet), "-af", "volume=-30dB", str(loud)], check=True)
    env.eng.files["q"] = loud
    plain = parse_wav(fetch_audio(env, "q")[2])
    norm = parse_wav(fetch_audio(env, "q", q="rate=48000&bits=16&channels=2&normalize=1")[2])
    assert peak(norm["pcm"], 16) > 4 * peak(plain["pcm"], 16)
    assert abs(norm["frames"] - plain["frames"]) < 48000 * 0.2


def test_trim_silence_removes_leading_and_trailing(env):
    f = _tone(env.tmp / "pad.wav", seconds=2, lead_silence=1.5, tail_silence=1.5)
    env.eng.files["pad"] = f
    full = parse_wav(fetch_audio(env, "pad")[2])
    trimmed = parse_wav(fetch_audio(env, "pad", q="rate=48000&bits=16&channels=2&trim=1")[2])
    assert full["frames"] > 48000 * 4.5  # 1.5 + 2 + 1.5
    assert abs(trimmed["frames"] - 48000 * 2) < 48000 * 0.15


def test_audio_format_validation(env, seed):
    for q in ("rate=96000", "rate=abc", "bits=32", "bits=8", "channels=3", "channels=0", "normalize=maybe",
              "trim=2", "wait=x"):
        assert fetch_audio(env, "a1", q=q)[0] == 400, q


# -- Range / HEAD ------------------------------------------------------------------------------------


def test_range_and_head(env, seed):
    _s, _h, full = fetch_audio(env, "a1")
    n = len(full)
    s, h, part = fetch_audio(env, "a1", headers={"Range": "bytes=100-199"})
    assert s == 206 and part == full[100:200]
    assert h["Content-Range"] == f"bytes 100-199/{n}" and h["Content-Length"] == "100"
    s, h, part = fetch_audio(env, "a1", headers={"Range": "bytes=-44"})
    assert s == 206 and part == full[-44:]
    s, h, part = fetch_audio(env, "a1", headers={"Range": f"bytes={n - 10}-"})
    assert s == 206 and part == full[-10:]
    s, h, _ = fetch_audio(env, "a1", headers={"Range": f"bytes={n}-"})
    assert s == 416 and h["Content-Range"] == f"bytes */{n}"
    s, h, body = fetch_audio(env, "a1", headers={"Range": "bytes=0-1,5-6"})
    assert s == 200 and body == full  # multi-range is ignored
    req = urllib.request.Request(env.url + "/api/game/audio/local/a1.wav?rate=48000&bits=16&channels=2",
                                 method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - loopback
        assert r.status == 200 and r.read() == b""
        assert int(r.headers["Content-Length"]) == n and r.headers["Content-Type"] == "audio/wav"
        assert r.headers["Accept-Ranges"] == "bytes"


# -- cache / single flight / 202 ------------------------------------------------------------------------


def test_cache_hit_skips_resolve_and_transcode(env, seed):
    a = fetch_audio(env, "a1")[2]
    b = fetch_audio(env, "a1")[2]
    assert a == b and env.eng.resolve_calls == [("local", "a1")]
    fetch_audio(env, "a1", q="rate=44100&bits=16&channels=2")  # different format -> its own entry
    assert len(env.eng.resolve_calls) == 2
    files = sorted(p.name for p in (env.tmp / "cache" / "game-audio").iterdir())
    assert len(files) == 2 and all(f.endswith(".wav") and len(f) == 44 for f in files)


def test_concurrent_requests_share_one_transcode(env, seed):
    env.eng.resolve_delay = 0.6
    results = []

    def go():
        results.append(fetch_audio(env, "a2")[2])

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert len(results) == 6 and all(r == results[0] for r in results)
    assert env.eng.resolve_calls == [("local", "a2")]  # one transcode served all six


def test_slow_transcode_returns_202_then_200(env, seed):
    env.eng.resolve_delay = 1.0
    status, hdrs, body = fetch_audio(env, "a1", q="rate=48000&bits=16&channels=2&wait=0")
    assert status == 202 and hdrs["Retry-After"] == "2"
    assert json.loads(body)["status"] == "transcoding"
    deadline = time.time() + 30
    while time.time() < deadline:
        status, _h, body = fetch_audio(env, "a1", q="rate=48000&bits=16&channels=2&wait=0")
        if status == 200:
            break
        time.sleep(0.2)
    assert status == 200 and parse_wav(body)["frames"] > 0
    assert len(env.eng.resolve_calls) == 1


def test_failed_transcode_is_never_served_or_cached(env, seed):
    env.eng.files["broken"] = env.tmp / "broken.wav"
    env.eng.files["broken"].write_bytes(b"this is not audio at all")
    status, _h, body = fetch_audio(env, "broken")
    assert status == 502 and json.loads(body)["code"] == "transcode_failed"
    cache = env.tmp / "cache" / "game-audio"
    assert list(cache.glob("*.wav")) == [] and list(cache.glob("*.part")) == []
    env.eng.resolve_error = RuntimeError("provider down https://cdn.example/x?token=SECRET")
    status, _h, body = fetch_audio(env, "a1")
    assert status == 502 and json.loads(body)["code"] == "source_unavailable"
    assert "SECRET" not in body.decode()
    env.eng.resolve_error = KeyError("qobuz")
    time.sleep(gs.FAIL_TTL_S * 0 + 0.01)
    assert fetch_audio(env, "a2")[0] == 404


def test_wav_valid_rejects_truncated_and_foreign_files(tmp_path):
    good = tmp_path / "g.wav"
    pcm = b"\x01\x00" * 100
    good.write_bytes(gs.wav_header(len(pcm), 48000, 16, 1) + pcm)
    assert gs.wav_valid(good)
    cut = tmp_path / "c.wav"
    cut.write_bytes(good.read_bytes()[:-10])
    assert not gs.wav_valid(cut)
    junk = tmp_path / "j.wav"
    junk.write_bytes(b"x" * 100)
    assert not gs.wav_valid(junk) and not gs.wav_valid(tmp_path / "missing.wav")


def test_invalid_cache_file_is_rebuilt_not_served(env, seed):
    body = fetch_audio(env, "a1")[2]
    (path,) = (env.tmp / "cache" / "game-audio").glob("*.wav")
    path.write_bytes(path.read_bytes()[:500])  # a torn file
    assert fetch_audio(env, "a1")[2] == body
    assert len(env.eng.resolve_calls) == 2


def test_lru_eviction_keeps_cache_under_cap(env, seed):
    # ~380 KB per 2 s stereo 48k/16 WAV; the floor is 64 MB so patch the cap directly.
    # The effective cap never drops below twice the largest file, so two always fit.
    env.game.cache._cap = lambda: 100_000
    env.game.cache.min_evict_age_s = 0  # production spares entries used in the last 2 minutes
    paths, sizes = {}, []
    for tid in ("a1", "a2", "m2"):
        fetch_audio(env, tid)
        paths[tid] = sorted(env.game.cache.dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)[-1]
        sizes.append(paths[tid].stat().st_size)
        time.sleep(0.05)
    st = env.game.cache.stats()
    biggest = max(sizes)
    assert st["files"] == 2 and st["bytes"] <= 2 * biggest  # oldest evicted, the last two kept
    assert not paths["a1"].exists() and paths["a2"].exists() and paths["m2"].exists()
    assert call(env, "GET", "/api/game/status")[1]["cache"]["cap"] == 100_000


def test_cache_cap_setting_has_a_floor(env):
    (env.tmp / "settings.json").write_text(json.dumps({"game_cache_max_mb": 1}))
    assert env.game._cap_bytes() == gs.MIN_CACHE_MB * 1024 * 1024
    (env.tmp / "settings.json").write_text(json.dumps({"game_cache_max_mb": 4096}))
    assert env.game._cap_bytes() == 4096 * 1024 * 1024


def test_missing_ffmpeg_is_a_clear_503(env, seed):
    env.game._ffmpeg = lambda: None
    status, _h, body = fetch_audio(env, "a1")
    assert status == 503 and json.loads(body)["code"] == "ffmpeg_missing"
    assert call(env, "GET", "/api/game/status")[1]["ffmpeg"] is False


# -- security -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/game/audio/local/..%2F..%2Fetc%2Fpasswd.wav",
    "/api/game/audio/local/%2e%2e%2f%2e%2e%2fsecret.wav",
    "/api/game/audio/local/a..b.wav",
    "/api/game/audio/..%2Fx/a1.wav",
    "/api/game/audio/spotify/a1.wav",
    "/api/game/audio/local/a1",
    "/api/game/audio/local/a1.mp3",
    "/api/game/audio/local/a%20b.wav",
    "/api/game/audio/local/http%3A%2F%2Fevil%2Fx.wav",
    "/api/game/audio/local/%00.wav",
    "/api/game/audio/local/.wav",
])
def test_audio_path_params_are_validated(env, seed, path):
    status, _h, _b = call(env, "GET", path, raw=True)
    assert status in (400, 404)
    assert env.eng.resolve_calls == []  # never reached the engine
    cache = env.tmp / "cache" / "game-audio"
    assert not cache.exists() or list(cache.iterdir()) == []


def test_audio_only_serves_engine_resolved_sources(env, seed):
    # an id the engine doesn't know: resolve fails, nothing is fetched from anywhere
    env.eng.resolve_error = KeyError("nope")
    assert fetch_audio(env, "zzz")[0] == 404


def test_ffmpeg_input_rejects_odd_schemes_and_header_injection():
    assert gs.ffmpeg_input_args("file:///tmp/a%20b.flac")[-1] == "/tmp/a b.flac"
    with pytest.raises(gs.GameError):
        gs.ffmpeg_input_args("concat:/etc/passwd|/etc/hosts")
    with pytest.raises(gs.GameError):
        gs.ffmpeg_input_args("data:audio/wav;base64,AAAA")
    args = gs.ffmpeg_input_args("https://cdn/x", {"X-A": "ok", "X-B": "a\r\nEvil: 1", "User-Agent": "UA"})
    assert "Evil" not in " ".join(args) and "X-A: ok" in " ".join(args) and "UA" in args
    assert args[args.index("-protocol_whitelist") + 1] == "file,http,https,tcp,tls,crypto"


def test_key_gate_on_every_method(env, seed):
    env.eng.key = "sekrit"
    call(env, "PUT", "/api/game/profiles/g", PROFILE, headers={"X-Harmony-Key": "sekrit"})
    cases = [("GET", "/api/game/status", None), ("GET", "/api/game/profiles", None),
             ("GET", "/api/game/profiles/g", None), ("PUT", "/api/game/profiles/g", PROFILE),
             ("DELETE", "/api/game/profiles/g", None),
             ("POST", "/api/game/next", {"profile": "g", "type": "explore"}),
             ("POST", "/api/game/playing", {"profile": "g", "type": "explore", "service": "local", "id": "a1"}),
             ("GET", "/api/game/audio/local/a1.wav", None), ("HEAD", "/api/game/audio/local/a1.wav", None)]
    for method, path, body in cases:
        assert call(env, method, path, body, raw=True)[0] == 401, (method, path)
        assert call(env, method, path, body, headers={"X-Harmony-Key": "wrong"}, raw=True)[0] == 401
    assert env.eng.resolve_calls == []
    ok = {"X-Harmony-Key": "sekrit"}
    assert call(env, "GET", "/api/game/status", headers=ok)[0] == 200
    assert call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore"}, headers=ok)[0] == 200
    assert call(env, "GET", "/api/game/audio/local/a1.wav?key=sekrit", raw=True)[0] == 200  # query key
    assert call(env, "HEAD", "/api/game/audio/local/a1.wav", headers=ok, raw=True)[0] == 200


def test_disabled_switch(env, seed):
    (env.tmp / "settings.json").write_text(json.dumps({"game_source_enabled": False}))
    assert call(env, "GET", "/api/game/status")[1]["enabled"] is False
    for method, path in (("GET", "/api/game/profiles"), ("POST", "/api/game/next")):
        status, body = call(env, method, path, {} if method == "POST" else None)
        assert status == 403 and body["code"] == "disabled"
    assert fetch_audio(env, "a1")[0] == 403


def test_unknown_route_and_wrong_method(env):
    assert call(env, "GET", "/api/game/nope")[0] == 404
    assert call(env, "GET", "/api/game/next")[0] == 405
    assert call(env, "POST", "/api/game/profiles")[0] == 405
    assert call(env, "DELETE", "/api/game/status")[0] == 404


def test_oversized_put_body_rejected(env):
    req = urllib.request.Request(env.url + "/api/game/profiles/g", data=b"{" + b" " * 1_100_000 + b"}",
                                 method="PUT", headers={"Content-Type": "application/json"})
    with pytest.raises((urllib.error.HTTPError, urllib.error.URLError, ConnectionError)):
        urllib.request.urlopen(req, timeout=10)  # noqa: S310 - loopback


# -- playing / status -----------------------------------------------------------------------------------------


def test_playing_records_now_playing_and_status(env, seed):
    call(env, "PUT", "/api/game/profiles/g", PROFILE)
    assert call(env, "GET", "/api/game/status")[1]["now_playing"] is None
    track = call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore"})[1]["tracks"][0]
    status, body = call(env, "POST", "/api/game/playing",
                        {"profile": "g", "type": "explore", "service": track["service"], "id": track["id"]})
    assert status == 200 and body["ok"] and body["now_playing"]["title"] == track["title"]
    st = call(env, "GET", "/api/game/status")[1]
    assert st["enabled"] is True and st["ffmpeg"] is True and st["profiles"] == ["g"]
    assert st["now_playing"]["id"] == track["id"] and st["now_playing"]["type"] == "explore"
    assert set(st["cache"]) == {"bytes", "files", "in_progress_bytes", "cap"} and st["cache"]["cap"] == 2048 * 1024 * 1024
    assert st["history"][0]["id"] == track["id"]
    # a game that went away stops "playing" once the track (+ grace) is over
    env.game._now["started_at"] -= 10_000
    assert call(env, "GET", "/api/game/status")[1]["now_playing"] is None
    assert len(call(env, "GET", "/api/game/status")[1]["history"]) == 1


@pytest.mark.parametrize("bad", [
    {}, {"profile": "g", "type": "explore", "service": "spotify", "id": "1"},
    {"profile": "g", "type": "explore", "service": "local", "id": "../x"},
    {"profile": "g", "type": "explore", "service": "local"},
    {"profile": "../g", "type": "explore", "service": "local", "id": "a"},
    {"profile": "g", "type": "ex plore", "service": "local", "id": "a"},
    {"profile": "g", "type": "explore", "service": "local", "id": "a", "x": 1}])
def test_playing_validates(env, bad):
    assert call(env, "POST", "/api/game/playing", bad)[0] == 400


def test_invalid_stored_profile_is_reported_not_crashing(env):
    (env.tmp / "settings.json").write_text(json.dumps({"game_profiles": {"bad": {"types": 5}, "good": PROFILE}}))
    assert [p["name"] for p in call(env, "GET", "/api/game/profiles")[1]["profiles"]] == ["good"]
    status, body = call(env, "GET", "/api/game/profiles/bad")
    assert status == 409 and body["code"] == "invalid_profile"


# -- no accounts at all: YouTube Music's public catalogue ----------------------------------------


def _no_accounts(env, audio_files):
    """No library, Qobuz signed out, YouTube Music unauthenticated but public."""
    eng = env.eng
    eng.lib.on = False
    eng.qobuz.is_authenticated = False
    eng.qobuz.tracks.append(_trk("qx", "Qobuz Ambient", service=Service.QOBUZ, genre="Ambient"))
    eng.yt = FakeProv(Service.YTMUSIC, [])
    eng.yt.is_authenticated = False
    for i, tid in enumerate(("yAAAAAAAAAA", "yBBBBBBBBBB", "yCCCCCCCCCC")):
        eng.yt.tracks.append(_trk(tid, f"Calm Song {i}", service=Service.YTMUSIC, genre="Ambient"))
        eng.files[tid] = audio_files["a1"]
    eng.yt.playlists["PLpublic"] = eng.yt.tracks[:2]
    eng.yt.albums["MPREb_pub"] = eng.yt.tracks[1:]
    return env


def test_no_accounts_sources_all_serves_youtube_music(env, audio_files):
    _no_accounts(env, audio_files)
    call(env, "PUT", "/api/game/profiles/anon", {"sources": "all", "types": {
        "explore": {"genre": "Ambient"}, "calm": {"query": "calm"},
        "pl": {"playlist": {"service": "ytmusic", "id": "PLpublic"}},
        "al": {"album": {"service": "ytmusic", "id": "MPREb_pub"}}}})
    for t in ("explore", "calm"):
        body = call(env, "POST", "/api/game/next", {"profile": "anon", "type": t, "count": 3})[1]
        assert {x["service"] for x in body["tracks"]} == {"ytmusic"}
        assert {x["id"] for x in body["tracks"]} == {"yAAAAAAAAAA", "yBBBBBBBBBB", "yCCCCCCCCCC"}
    assert ("search", "Ambient music") in env.eng.yt.calls
    assert env.eng.qobuz.calls == []                      # signed-out Qobuz is skipped silently
    assert not any(c[0] == "browse" for c in env.eng.lib.calls)  # and so is the empty Library
    pl = call(env, "POST", "/api/game/next", {"profile": "anon", "type": "pl", "count": 5})[1]
    assert {x["id"] for x in pl["tracks"]} == {"yAAAAAAAAAA", "yBBBBBBBBBB"}
    al = call(env, "POST", "/api/game/next", {"profile": "anon", "type": "al", "count": 5})[1]
    assert {x["id"] for x in al["tracks"]} == {"yBBBBBBBBBB", "yCCCCCCCCCC"}
    track = pl["tracks"][0]
    status, hdrs, data = call(env, "GET", track["audio_url"], raw=True)
    assert status == 200 and hdrs["Content-Type"] == "audio/wav"
    assert parse_wav(data)["rate"] == 48000 and env.eng.resolve_calls[0][0] == "ytmusic"


def test_no_accounts_library_mode_has_nothing_and_says_so(env, audio_files):
    _no_accounts(env, audio_files)
    call(env, "PUT", "/api/game/profiles/anon", {"sources": "library", "types": {"explore": {"genre": "Ambient"}}})
    status, body = call(env, "POST", "/api/game/next", {"profile": "anon", "type": "explore"})
    assert status == 404 and body["code"] == "no_tracks" and "sources=library" in body["error"]
    ok = call(env, "POST", "/api/game/next", {"profile": "anon", "type": "explore", "sources": "all"})
    assert ok[0] == 200


def test_signed_out_qobuz_selector_is_skipped_not_an_error(env, audio_files):
    _no_accounts(env, audio_files)
    call(env, "PUT", "/api/game/profiles/anon", {"sources": "all", "types": {"x": [
        {"playlist": {"service": "qobuz", "id": "123"}}, {"genre": "Ambient"}]}})
    body = call(env, "POST", "/api/game/next", {"profile": "anon", "type": "x", "count": 3})[1]
    assert {t["service"] for t in body["tracks"]} == {"ytmusic"}


def test_blocked_youtube_track_stops_being_offered(env, audio_files):
    _no_accounts(env, audio_files)
    env.game.rng_factory = lambda: random.Random(11)   # a fixed shuffle: no flakiness
    call(env, "PUT", "/api/game/profiles/anon", {"sources": "all", "types": {"explore": {"genre": "Ambient"}}})
    env.eng.resolve_error = RuntimeError("Video unavailable in your country")
    status, _h, body = call(env, "GET", "/api/game/audio/ytmusic/yBBBBBBBBBB.wav", raw=True)
    assert status == 502 and json.loads(body)["code"] == "source_unavailable"
    env.eng.resolve_error = None
    for _ in range(3):  # whole cycles never contain the blocked track again
        ids = [t["id"] for t in call(env, "POST", "/api/game/next",
                                     {"profile": "anon", "type": "explore", "count": 2})[1]["tracks"]]
        assert "yBBBBBBBBBB" not in ids and len(ids) == 2
    peek = call(env, "POST", "/api/game/next", {"profile": "anon", "type": "explore", "count": 2, "peek": True})[1]
    real = call(env, "POST", "/api/game/next", {"profile": "anon", "type": "explore", "count": 2})[1]
    assert [t["id"] for t in peek["tracks"]] == [t["id"] for t in real["tracks"]]


def test_streaming_tracks_outside_a_game_friendly_length_are_not_offered(env, audio_files):
    _no_accounts(env, audio_files)
    env.eng.yt.tracks += [_trk("yLONG000000", "Calm Sleep 2h", service=Service.YTMUSIC, genre="Ambient",
                               duration=7201),
                          _trk("ySHORT00000", "Calm jingle", service=Service.YTMUSIC, genre="Ambient",
                               duration=12),
                          _trk("yUNKNOWN000", "Calm unknown length", service=Service.YTMUSIC,
                               genre="Ambient", duration=0)]
    call(env, "PUT", "/api/game/profiles/anon", {"sources": "all", "types": {"x": {"genre": "Ambient"}}})
    body = call(env, "POST", "/api/game/next", {"profile": "anon", "type": "x", "count": 10})[1]
    ids = {t["id"] for t in body["tracks"]}
    assert "yLONG000000" not in ids and "ySHORT00000" not in ids and "yUNKNOWN000" in ids


# -- the bag: peek == consume, count honoured (review finding 1) ---------------------------------------


def _bare_game(tmp_path, seed, n, bad=()):
    """A GameSource + one hand-built pool of ``n`` YouTube tracks (``bad`` ones blocked)."""
    game = gs.GameSource(FakeEngine(tmp_path), cache_dir=tmp_path / "audio", settings=lambda: SimpleNamespace())
    pool = gs.TrackPool(random.Random(seed))
    for i in range(n):
        pool.refs[("ytmusic", f"t{i}")] = {"service": "ytmusic", "id": f"t{i}"}
    pool.bag.sync(list(pool.refs))
    for i in bad:
        game._mark_bad("ytmusic", f"t{i}")
    return game, pool


def _ids(tracks):
    return [t["id"] for t in tracks]


@pytest.mark.parametrize("seed", range(120))
def test_take_peek_equals_consume_and_honours_count(tmp_path, seed):
    rng = random.Random(seed * 7919)
    n = rng.randint(1, 7)
    bad = rng.sample(range(n), rng.randint(0, n))
    good = n - len(bad)
    game, pool = _bare_game(tmp_path, seed, n, bad)
    emitted: list[str] = []
    for _ in range(14):
        count = rng.randint(1, gs.MAX_COUNT)
        if rng.random() < 0.5 and good:   # sometimes a (repeatable) peek first
            peek = _ids(game._take(pool, count, consume=False))
            assert _ids(game._take(pool, count, consume=False)) == peek
        else:
            peek = None
        got = _ids(game._take(pool, count, consume=True))
        if not good:
            assert got == [] and peek is None
            continue
        if peek is not None:
            assert got == peek
        assert len(got) == count            # never short, however many are blocked
        assert not set(got) & {f"t{i}" for i in bad}
        emitted += got
    if good > 1:                            # a track never plays twice in a row...
        assert all(a != b for a, b in zip(emitted, emitted[1:], strict=False))
    elif good == 1:                         # ...unless it is the only playable one
        assert len(set(emitted)) == 1


def test_take_reviewer_scenarios_blocked_pool(tmp_path):
    # 3 tracks, 2 blocked, count 4: used to return fewer / ['A','A','A','A'] diverging from the peek
    for seed in range(30):
        game, pool = _bare_game(tmp_path, seed, 3, bad=[1, 2])
        peek = _ids(game._take(pool, 4, consume=False))
        assert peek == _ids(game._take(pool, 4, consume=True)) == ["t0"] * 4
    # 2 playable of 5: peek and consume agree across a cycle boundary, count honoured, no A,A
    for seed in range(60):
        game, pool = _bare_game(tmp_path, seed, 5, bad=[0, 1, 2])
        peek = _ids(game._take(pool, 8, consume=False))
        got = _ids(game._take(pool, 8, consume=True))
        assert got == peek and len(got) == 8 and all(a != b for a, b in zip(got, got[1:], strict=False))


def test_pick_with_no_playable_returns_empty_and_does_not_spin(tmp_path):
    game, pool = _bare_game(tmp_path, 1, 3, bad=[0, 1, 2])
    assert game._take(pool, 5, consume=True) == [] and game._take(pool, 5, consume=False) == []
    bag = gs.Bag(random.Random(1))
    assert bag.pick(3, lambda _k: True, repeat_ok=False, consume=True) == []   # empty bag


# -- ffmpeg pipeline: bounded memory (review finding 2) -------------------------------------------------


def test_input_is_limited_before_dash_i_and_trim_does_not_reverse(monkeypatch, tmp_path):
    for url in ("file:///music/a.flac", "https://cdn.example/s.m4a"):
        args = gs.ffmpeg_input_args(url)
        assert args[args.index("-t") + 1] == str(gs.MAX_AUDIO_S) and args.index("-t") < args.index("-i")
    seen: dict[str, list[str]] = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        kw["stdout"].write(b"\x00\x10" * 4000)           # some non-silent PCM
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(gs.subprocess, "run", fake_run)
    gs.transcode_to_wav("ffmpeg", gs.ffmpeg_input_args("file:///music/a.flac"), tmp_path / "o.part",
                        rate=48000, bits=16, channels=2, normalize=True, trim=True)
    cmd = seen["cmd"]
    af = cmd[cmd.index("-af") + 1]
    assert "areverse" not in af and "silenceremove=start_periods=1" in af and "loudnorm" in af
    assert cmd.index("-t") < cmd.index("-i")                   # input-side cap, before -i
    assert "-t" in cmd[cmd.index("-i") + 2:]                   # and the output-side one
    assert gs.wav_valid(tmp_path / "o.part")


def test_input_side_cap_really_bounds_what_ffmpeg_reads(env, seed, monkeypatch):
    monkeypatch.setattr(gs, "MAX_AUDIO_S", 1)                  # the sources are 2 s long
    wav = parse_wav(fetch_audio(env, "a1", q="rate=48000&bits=16&channels=2&trim=1")[2])
    assert 0.9 < wav["frames"] / 48000 <= 1.01


def _pcm(bits, channels, frames_loud, frames_quiet, loud=8000):
    width = bits // 8
    unit = (loud << (8 * (width - 2))) & ((1 << bits) - 1) if bits == 24 else loud
    frame = int(unit).to_bytes(width, "little", signed=False) * channels
    quiet = (2).to_bytes(width, "little") * channels                # far under -50 dBFS
    return frame * frames_loud + quiet * frames_quiet


@pytest.mark.parametrize("bits", [16, 24])
@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize(("loud", "quiet"), [(100, 30), (70_000, 70_000), (1, 0), (65_536, 65_536)])
def test_trim_tail_silence_keeps_exactly_the_audible_frames(tmp_path, bits, channels, loud, quiet):
    block = bits // 8 * channels
    f = tmp_path / "pcm.bin"
    f.write_bytes(b"HEADER" + _pcm(bits, channels, loud, quiet))
    with open(f, "rb") as fh:
        assert gs.trim_tail_silence(fh, 6, (loud + quiet) * block, bits, channels) == loud * block


@pytest.mark.parametrize("bits", [16, 24])
def test_trim_tail_silence_all_silent_is_zero_and_one_channel_loud_counts(tmp_path, bits):
    f = tmp_path / "pcm.bin"
    f.write_bytes(_pcm(bits, 2, 0, 500))
    with open(f, "rb") as fh:
        assert gs.trim_tail_silence(fh, 0, 500 * bits // 8 * 2, bits, 2) == 0
    width = bits // 8                                           # loud on the RIGHT channel only
    loud = (20_000 << (8 * (width - 2)) if bits == 24 else 20_000).to_bytes(width, "little")
    quiet = (1).to_bytes(width, "little")
    f.write_bytes((quiet + loud) * 10 + (quiet + quiet) * 30)
    with open(f, "rb") as fh:
        assert gs.trim_tail_silence(fh, 0, 40 * width * 2, bits, 2) == 10 * width * 2


# -- audio cache: bounded, pinned, honest (review finding 3) --------------------------------------------


def _wav_bytes(n_frames: int = 400) -> bytes:
    pcm = b"\x01\x00" * n_frames
    return gs.wav_header(len(pcm), 48000, 16, 1) + pcm


def _writer(size_frames=400, hold: threading.Event | None = None, started: list | None = None,
            stored_as: str | None = None):
    def build(tmp):
        if started is not None:
            started.append(tmp.name)
        if hold is not None:
            assert hold.wait(30)
        tmp.write_bytes(_wav_bytes(size_frames))
        return stored_as
    return build


def test_cache_runs_at_most_max_builds_at_once_and_queues_the_rest(tmp_path):
    cache = gs.AudioCache(tmp_path, lambda: 10**9, max_builds=2, max_queued=10)
    hold, running, peak, lock = threading.Event(), [0], [0], threading.Lock()

    def build(tmp):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        assert hold.wait(30)
        tmp.write_bytes(_wav_bytes())
        with lock:
            running[0] -= 1

    assert [cache.get(f"k{i}", build, 0) for i in range(6)] == [None] * 6   # all 202, none dropped
    time.sleep(0.3)
    assert peak[0] == 2                                                      # only 2 transcoding
    hold.set()
    for i in range(6):
        path = cache.get(f"k{i}", build, 20)
        assert path is not None and path.exists()
    assert peak[0] == 2


def test_cache_refuses_a_flood_with_503_busy(tmp_path):
    cache = gs.AudioCache(tmp_path, lambda: 10**9, max_builds=1, max_queued=1)
    hold = threading.Event()
    cache.get("k0", _writer(hold=hold), 0)
    cache.get("k1", _writer(hold=hold), 0)
    with pytest.raises(gs.GameError) as exc:
        cache.get("k2", _writer(hold=hold), 0)
    assert exc.value.status == 503 and exc.value.code == "busy" and exc.value.retry_after
    assert gs._error(exc.value).headers["Retry-After"] == str(exc.value.retry_after)
    cache.get("k0", _writer(hold=hold), 0)       # joining an existing job is still fine
    hold.set()
    assert cache.get("k1", _writer(), 20) is not None


def test_part_files_count_toward_the_cap_and_refuse_new_builds(tmp_path):
    cache = gs.AudioCache(tmp_path, lambda: 1000, max_builds=1)
    (tmp_path / "other.1.2.part").write_bytes(b"x" * 5000)      # another job's half-written output
    with pytest.raises(gs.GameError) as exc:
        cache.get("new", _writer(), 5)
    assert exc.value.status == 503 and exc.value.code == "cache_full" and "game_cache_max_mb" in exc.value.message
    assert exc.value.retry_after and cache.stats()["in_progress_bytes"] == 5000
    (tmp_path / "other.1.2.part").unlink()
    assert cache.get("new", _writer(), 5) is not None


def test_eviction_spares_pinned_and_recent_entries_and_keeps_two_files(tmp_path):
    cache = gs.AudioCache(tmp_path, lambda: 1, min_evict_age_s=0)   # absurdly small cap
    old = time.time() - 1000
    for i, name in enumerate("abc"):
        (tmp_path / f"{name}.wav").write_bytes(_wav_bytes())
        os.utime(tmp_path / f"{name}.wav", (old + i, old + i))
    size = (tmp_path / "a.wav").stat().st_size
    cache.pin(tmp_path / "a.wav")                                    # being served right now
    cache._evict()
    assert sorted(p.name for p in tmp_path.glob("*.wav")) == ["a.wav", "c.wav"]   # b (oldest unpinned) went
    assert cache.stats()["bytes"] == 2 * size                        # floor: 2x the largest file, not 1 byte
    cache.unpin(tmp_path / "a.wav")
    assert not cache._pins
    cache.min_evict_age_s = 3600                                     # everything is "recent": spared
    (tmp_path / "d.wav").write_bytes(_wav_bytes())
    cache._evict()
    assert len(list(tmp_path.glob("*.wav"))) == 3


def test_two_jobs_do_not_evict_each_other_under_a_tiny_cap(tmp_path):
    cache = gs.AudioCache(tmp_path, lambda: 1, min_evict_age_s=0)
    hold = threading.Event()
    assert cache.get("one", _writer(hold=hold), 0) is None and cache.get("two", _writer(hold=hold), 0) is None
    hold.set()
    one, two = cache.get("one", _writer(), 20), cache.get("two", _writer(), 20)
    assert one is not None and two is not None and one.exists() and two.exists()   # no 202 loop


def test_cache_get_pins_the_returned_file_until_released(tmp_path):
    cache = gs.AudioCache(tmp_path, lambda: 1, min_evict_age_s=0)
    a = cache.get("a", _writer(), 5)
    assert cache._pinned(a.name)
    (tmp_path / "b.wav").write_bytes(_wav_bytes())
    (tmp_path / "c.wav").write_bytes(_wav_bytes())
    os.utime(a, (1, 1))                                              # the OLDEST, but pinned
    cache._evict()
    assert a.exists() and not (tmp_path / "b.wav").exists()          # b, the oldest UNPINNED, went
    cache.unpin(a)
    (tmp_path / "d.wav").write_bytes(_wav_bytes())
    cache._evict()
    assert not a.exists()                                            # now a is just the oldest


def test_build_may_store_under_the_key_of_the_source_it_really_used(tmp_path):
    cache = gs.AudioCache(tmp_path, lambda: 10**9)
    path = cache.get("predicted", _writer(stored_as="actual"), 10)
    assert path == cache.path_for("actual") and path.exists() and not cache.path_for("predicted").exists()


def test_failed_memory_is_bounded_and_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "MAX_FAILED", 3)
    cache = gs.AudioCache(tmp_path, lambda: 10**9)

    def boom(_tmp):
        raise gs.GameError(502, "source_unavailable", "nope")

    for i in range(8):
        with pytest.raises(gs.GameError):
            cache.get(f"f{i}", boom, 10)
    assert len(cache._failed) <= 3 and "f7" in cache._failed and "f0" not in cache._failed


def test_bad_set_is_bounded_and_expires(tmp_path, monkeypatch):
    game, _pool = _bare_game(tmp_path, 1, 1)
    monkeypatch.setattr(gs, "MAX_BAD", 5)
    for i in range(40):
        game._mark_bad("ytmusic", f"x{i}")
    assert len(game._bad) == 5 and ("ytmusic", "x39") in game._bad and ("ytmusic", "x0") not in game._bad
    monkeypatch.setattr(gs, "BAD_TTL_S", 0.05)
    time.sleep(0.1)
    game._mark_bad("ytmusic", "fresh")
    assert list(game._bad) == [("ytmusic", "fresh")]                 # the expired ones were dropped


def test_serving_pins_the_file_and_releases_it_afterwards(env, seed):
    fetch_audio(env, "a1")
    assert not env.game.cache._pins                                  # GET finished -> unpinned
    resp = env.game.audio("local", "a1.wav", {}, None)
    assert resp.file is not None and env.game.cache._pinned(resp.file.name)
    resp.done()
    resp.done()                                                      # idempotent
    assert not env.game.cache._pins
    assert call(env, "HEAD", "/api/game/audio/local/a1.wav", raw=True)[0] == 200
    assert call(env, "GET", "/api/game/audio/local/a1.wav", headers={"Range": "bytes=999999999-"},
                raw=True)[0] == 416
    assert not env.game.cache._pins                                  # HEAD and 416 released it too


def test_a_cache_file_that_vanishes_is_retried_then_503_never_500(env, seed):
    real_get = env.game.cache.get
    calls = []

    def vanishing_get(key, build, wait):
        path = real_get(key, build, wait)
        calls.append(path)
        if len(calls) == 1:
            path.unlink()                                            # evicted between get and stat
        return path

    env.game.cache.get = vanishing_get
    status, _h, body = fetch_audio(env, "a1")
    assert status == 200 and parse_wav(body)["frames"] > 0 and len(calls) == 2   # rebuilt once
    calls.clear()
    env.game.cache.get = lambda *a, **k: env.tmp / "gone.wav"        # never exists
    status, hdrs, body = fetch_audio(env, "a1")
    assert status == 503 and json.loads(body)["code"] == "cache_busy" and hdrs["Retry-After"]
    env.game.cache.get = real_get


def test_send_response_with_a_vanished_file_is_a_clean_503(tmp_path):
    class Handler:
        def __init__(self):
            self.status, self.sent, self.out = None, [], b""
            self.wfile = self

        def send_response(self, code):
            self.status = code

        def send_header(self, k, v):
            self.sent.append((k, v))

        def end_headers(self):
            pass

        def write(self, data):
            self.out += data

    released = []
    resp = gs.GameResponse(200, None, {"Content-Length": "5"}, file=tmp_path / "gone.wav", length=5,
                           release=lambda: released.append(1))
    h = Handler()
    gs.send_response(h, resp)
    assert h.status == 503 and json.loads(h.out)["code"] == "cache_busy" and ("Retry-After", "2") in h.sent
    assert released == [1]


def test_cache_key_follows_the_source_actually_used(env, seed):
    eng = env.eng
    eng.files["yX"] = eng.files["a1"]
    stream = fetch_audio(env, "yX", service="ytmusic")
    assert eng.resolve_calls == [("ytmusic", "yX")]
    eng.alias[("ytmusic", "yX")] = ("local", "a2")                   # the Library now has a copy
    lib = fetch_audio(env, "yX", service="ytmusic")
    assert len(eng.resolve_calls) == 2 and lib[2] != stream[2]       # NOT the cached stream
    assert fetch_audio(env, "yX", service="ytmusic")[2] == lib[2] and len(eng.resolve_calls) == 2
    assert fetch_audio(env, "a2")[2] == lib[2] and len(eng.resolve_calls) == 2   # same slot as the local id
    eng.alias_unavailable = True                                     # copy vanished: falls back to the stream
    eng.alias[("ytmusic", "yX")] = ("local", "a2")
    fetch_audio(env, "yX", service="ytmusic", q="rate=44100&bits=16&channels=2")
    keys = {p.name for p in env.game.cache.dir.glob("*.wav")}
    assert len(keys) == 3
    again = fetch_audio(env, "yX", service="ytmusic", q="rate=44100&bits=16&channels=2")
    assert again[0] == 200                                           # served from the stream-keyed entry


# -- profiles: size caps, one Settings load per request (review finding 4) -------------------------------


def _fat_profile(n_types: int, queries: int = 16, width: int = 190, tag: str = "x"):
    return {"types": {f"t{i}": [{"query": f"{tag}{i}-{j}-".ljust(width, "q")} for j in range(queries)]
                      for i in range(n_types)}}


def test_profile_size_is_capped_with_413(env):
    big = _fat_profile(12)                      # ~40 KB serialised
    assert gs._size(big) > gs.MAX_PROFILE_BYTES
    status, body = call(env, "PUT", "/api/game/profiles/big", big)
    assert status == 413 and body["code"] == "profile_too_large" and str(gs.MAX_PROFILE_BYTES) in body["error"]
    ok = _fat_profile(8)
    assert gs._size(ok) < gs.MAX_PROFILE_BYTES
    assert call(env, "PUT", "/api/game/profiles/ok", ok)[0] == 200
    assert call(env, "GET", "/api/game/profiles/big")[0] == 404


def test_total_profile_storage_is_capped_with_413(env):
    one = _fat_profile(8)
    per = gs._size({"sources": "library", "format": {"rate": 48000, "bits": 16, "channels": 2},
                    "normalize": False, "trim_silence": False, **one})
    fits = gs.MAX_PROFILES_BYTES // per
    assert 2 <= fits < gs.MAX_PROFILES                      # the byte budget binds before the count
    for i in range(fits):
        assert call(env, "PUT", f"/api/game/profiles/p{i}", _fat_profile(8, tag=f"p{i}"))[0] == 200
    status, body = call(env, "PUT", "/api/game/profiles/overflow", _fat_profile(8, tag="o"))
    assert status == 413 and body["code"] == "profiles_too_large"
    assert call(env, "PUT", "/api/game/profiles/p0", _fat_profile(8, tag="again"))[0] == 200  # replacing is fine
    assert call(env, "DELETE", "/api/game/profiles/p1")[0] == 200
    assert call(env, "PUT", "/api/game/profiles/overflow", _fat_profile(8, tag="o"))[0] == 200


def test_profile_count_is_capped(env):
    for i in range(gs.MAX_PROFILES):
        assert call(env, "PUT", f"/api/game/profiles/c{i}", {"types": {"x": {"genre": "G"}}})[0] == 200
    status, body = call(env, "PUT", "/api/game/profiles/one-too-many", {"types": {"x": {"genre": "G"}}})
    assert status == 400 and body["code"] == "too_many_profiles"


def test_each_request_loads_settings_once(env, seed, monkeypatch):
    for i in range(6):
        call(env, "PUT", f"/api/game/profiles/p{i}", PROFILE)
    real = env.game._settings
    loads: list[str] = []

    def counting():
        loads.append(threading.current_thread().name)
        return real()

    monkeypatch.setattr(env.game, "_settings", counting)
    fetch_audio(env, "a1")                                           # also warms the audio cache
    for method, path, body in [("GET", "/api/game/profiles", None), ("GET", "/api/game/status", None),
                               ("GET", "/api/game/profiles/p3", None),
                               ("POST", "/api/game/next", {"profile": "p1", "type": "explore"}),
                               ("POST", "/api/game/next", {"profile": "p1", "type": "explore", "peek": True}),
                               ("GET", "/api/game/audio/local/a1.wav", None)]:
        loads.clear()
        assert call(env, method, path, body, raw=method == "GET" and "audio" in path)[0] == 200
        assert len(loads) == 1, (method, path, loads)


def test_empty_pool_is_retried_soon_not_cached_for_the_ttl(env, seed, monkeypatch):
    monkeypatch.setattr(gs, "EMPTY_POOL_RETRY_S", 0.2)
    saved = list(env.eng.lib.tracks)
    env.eng.lib.tracks.clear()                                       # the Library is mid-scan
    call(env, "PUT", "/api/game/profiles/g", PROFILE)
    assert call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore"})[0] == 404
    env.eng.lib.tracks.extend(saved)                                 # scan finished
    time.sleep(0.4)
    status, body = call(env, "POST", "/api/game/next", {"profile": "g", "type": "explore"})
    assert status == 200 and body["tracks"]


# -- the default scope is every ACTIVE source ----------------------------------------------------------------


def _ids_over_cycles(env, profile, extra=None, cycles=8):
    seen = set()
    for _ in range(cycles):
        body = call(env, "POST", "/api/game/next", {"profile": profile, "type": "explore", "count": 10, **(extra or {})})[1]
        seen |= {(t["service"], t["id"]) for t in body["tracks"]}
    return seen, body["sources"]


def _with_streaming(env):
    env.eng.qobuz.tracks.append(_trk("q1", "Qobuz Ambient", service=Service.QOBUZ, genre="Ambient"))
    env.eng.yt = FakeProv(Service.YTMUSIC, [_trk("yAAAAAAAAAA", "Calm Song", service=Service.YTMUSIC,
                                                  genre="Ambient")])
    env.eng.yt.is_authenticated = False                        # public catalogue, no account
    env.eng.files["q1"] = env.eng.files["yAAAAAAAAAA"] = env.eng.files["a1"]


def test_default_sources_is_all_active_with_a_library(env, seed):
    _with_streaming(env)
    stored = call(env, "PUT", "/api/game/profiles/g", {"types": {"explore": {"genre": "Ambient"}}})[1]
    assert stored["sources"] == "all"                         # the profile default
    seen, used = _ids_over_cycles(env, "g")                   # neither request nor profile set it
    assert used == "all"
    assert seen == {("local", "a1"), ("local", "a2"), ("local", "a3"),
                    ("qobuz", "q1"), ("ytmusic", "yAAAAAAAAAA")}


def test_default_sources_without_a_library_or_qobuz_login_skips_them_silently(env, audio_files):
    _no_accounts(env, audio_files)                            # no library, Qobuz signed out, YT public
    call(env, "PUT", "/api/game/profiles/g", {"types": {"explore": {"genre": "Ambient"}}})
    seen, used = _ids_over_cycles(env, "g")
    assert used == "all" and {s for s, _i in seen} == {"ytmusic"} and len(seen) == 3
    assert env.eng.qobuz.calls == []


def test_default_sources_uses_qobuz_only_when_signed_in(env, seed):
    _with_streaming(env)
    env.eng.qobuz.is_authenticated = False
    call(env, "PUT", "/api/game/profiles/g", {"types": {"explore": {"genre": "Ambient"}}})
    seen, _used = _ids_over_cycles(env, "g")
    assert ("qobuz", "q1") not in seen and ("ytmusic", "yAAAAAAAAAA") in seen and ("local", "a1") in seen


def test_library_only_must_be_asked_for(env, seed):
    _with_streaming(env)
    call(env, "PUT", "/api/game/profiles/g", {"types": {"explore": {"genre": "Ambient"}}})        # default: all
    call(env, "PUT", "/api/game/profiles/lib", {"sources": "library", "types": {"explore": {"genre": "Ambient"}}})
    only_lib = {("local", "a1"), ("local", "a2"), ("local", "a3")}
    assert _ids_over_cycles(env, "lib") == (only_lib, "library")             # profile says library
    assert _ids_over_cycles(env, "g", {"sources": "library"}) == (only_lib, "library")   # request says library
    seen, used = _ids_over_cycles(env, "lib", {"sources": "all"})            # and the request can widen it
    assert used == "all" and ("ytmusic", "yAAAAAAAAAA") in seen
    # the bags are separate per scope: asking for library did not disturb the "all" pool
    assert _ids_over_cycles(env, "g")[1] == "all"
