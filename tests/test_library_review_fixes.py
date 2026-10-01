"""Regression tests for the Library review fixes: fallback from a broken
Library copy, lock-free Library calls, peer re-addressing, title cores, the
mesh-Lidarr opt-out, uncached match failures, parallel peer probes, ISRC case
and the annotation cap. Offline; loopback only."""

# ruff: noqa: F811, F401
from __future__ import annotations

import contextlib
import threading
import time

import pytest
from test_library import (
    _enable,
    _json,
    _post,
    _tag,
    _with_streaming_service,
    lib,
    server,
)

from harmony import library
from harmony.library import core_title


def _lib_id(server, key="nemo"):
    return library.track_id_for(server.files[key])


def _fallback_source(server):
    from harmony.models import StreamSource

    return StreamSource(url=server.files["dark"].as_uri(), mime_type="audio/wav")


# -- 1. fallback when the Library copy can't play ---------------------------------


def _matched_streaming(server, monkeypatch):
    _enable(server)
    q = _with_streaming_service(server, monkeypatch)
    r = _json(server.url + "/api/search?q=nemo&kinds=tracks")
    nemo = next(t for t in r["tracks"] if t["id"] == "q-nemo")
    assert nemo["library_id"] == _lib_id(server)
    assert ("qobuz", "q-nemo") in server.engine._lib_alias
    return q


def test_resolve_falls_back_to_streaming_when_library_file_is_gone(server, monkeypatch):
    q = _matched_streaming(server, monkeypatch)
    server.files["nemo"].unlink()
    src = _fallback_source(server)
    q.resolve_stream = lambda tid, max_quality=False: src
    res = _json(server.url + "/api/resolve?service=qobuz&id=q-nemo")
    assert res["from_library"] is False and res["service"] == "qobuz"
    assert ("qobuz", "q-nemo") not in server.engine._lib_alias


def test_resolve_falls_back_when_library_resolve_raises(server, monkeypatch):
    q = _matched_streaming(server, monkeypatch)
    prov = server.engine._library_provider()

    def boom(*a, **k):
        raise RuntimeError("peer offline")

    monkeypatch.setattr(prov, "resolve_stream", boom)
    src = _fallback_source(server)
    q.resolve_stream = lambda tid, max_quality=False: src
    res = _json(server.url + "/api/resolve?service=qobuz&id=q-nemo")
    assert res["from_library"] is False
    assert ("qobuz", "q-nemo") not in server.engine._lib_alias


def test_cast_direct_retries_the_stream_when_library_cast_fails(server, monkeypatch):
    _matched_streaming(server, monkeypatch)
    calls = []

    class Caster:
        def cast(self, host, service, track_id, meta, kind, device_info):
            calls.append((service, track_id))
            if service == "local":
                raise RuntimeError("cannot cast library copy")
            return {"ok": True}

    monkeypatch.setattr(server.engine, "_caster", lambda: Caster())
    monkeypatch.setattr(server.engine, "_device_kind", lambda host: ("wiim", {}))
    out = server.engine._cast_direct("10.0.0.5", "qobuz", "q-nemo", {"title": "Nemo (Remastered)"})
    assert out == {"ok": True}
    assert calls == [("local", _lib_id(server)), ("qobuz", "q-nemo")]
    assert ("qobuz", "q-nemo") not in server.engine._lib_alias


# -- 2. Library calls don't hold the engine lock -------------------------------------


def test_library_resolve_does_not_need_the_engine_lock(server):
    _enable(server)
    engine = server.engine
    done = []
    with engine._lock:
        t = threading.Thread(
            target=lambda: done.append(engine.resolve("local", _lib_id(server))), daemon=True)
        t.start()
        t.join(5)
        finished = not t.is_alive()
    assert finished and done and done[0]["service"] == "local"


def test_plock_is_per_service(server):
    engine = server.engine
    assert isinstance(engine._plock("local"), contextlib.nullcontext)
    assert engine._plock("qobuz") is engine._lock


# -- 3. _library_for_peer ----------------------------------------------------------------


def test_library_for_peer_readdresses_ids(server, monkeypatch):
    from harmony.playback.relay import RelayServer

    monkeypatch.setattr(RelayServer, "_local_ip_for", staticmethod(lambda host: "192.168.1.2"))
    server.engine._http_port = 9999
    me = "192.168.1.2:9999"
    out = server.engine._library_for_peer([
        {"service": "local", "id": "t2@10.0.0.7:8080"},
        {"service": "local", "id": "t3@10.0.0.8:8080"},
        {"service": "local", "id": "t4"},
        {"service": "qobuz", "id": "q1"},
    ], "10.0.0.7:8080")
    assert [t["id"] for t in out] == ["t2", "t3@10.0.0.8:8080", f"t4@{me}", "q1"]


# -- 4. core_title ----------------------------------------------------------------------------


@pytest.mark.parametrize("raw, core", [
    ("Song (Clean Bandit Remix)", "song clean bandit remix"),
    ("Song (Stereo Love)", "song stereo love"),
    ("Song (ft. X)", "song"),
    ("Song (feat. A & B) [Explicit]", "song"),
    ("Yesterday (2009 Remaster)", "yesterday"),
    ("Creep (Live)", "creep live"),
])
def test_core_title(raw, core):
    assert core_title(raw) == core


# -- 5. mesh Lidarr opt-out ---------------------------------------------------------------


def _peer_lidarr(server, monkeypatch):
    calls = []
    monkeypatch.setattr(server.engine, "instances",
                        lambda: {"instances": [{"name": "server", "host": "10.0.0.2", "port": 8080}]})

    def peer_call(via, method, path, body=None, timeout=15):
        calls.append((via, method, path))
        assert (method, path) == ("GET", "/api/lidarr?own=1")
        return {"enabled": True, "configured": True, "ok": True}

    monkeypatch.setattr(server.engine, "_peer_call", peer_call)
    return calls


def test_mesh_lidarr_opt_out(server, monkeypatch):
    calls = _peer_lidarr(server, monkeypatch)
    _post(server.url + "/api/lidarr/config", {"mesh": False})
    from harmony.config import Settings
    assert Settings.load().lidarr_mesh is False
    server.engine._lidarr_cache.clear()
    st = _json(server.url + "/api/lidarr")
    assert "via" not in st and st["mesh_enabled"] is False and calls == []

    _post(server.url + "/api/lidarr/config", {"mesh": True})
    assert Settings.load().lidarr_mesh is True
    server.engine._lidarr_cache.clear()
    st = _json(server.url + "/api/lidarr")
    assert st["via"] == "server" and calls and set(calls) == {("10.0.0.2:8080", "GET", "/api/lidarr?own=1")}


def test_mesh_lidarr_is_on_by_default(server, monkeypatch):
    calls = _peer_lidarr(server, monkeypatch)
    server.engine._lidarr_cache.clear()
    assert _json(server.url + "/api/lidarr")["via"] == "server" and calls


# -- 6. match failures aren't cached ------------------------------------------------------------


def test_match_failure_is_not_cached(server):
    _enable(server)
    engine = server.engine
    prov = engine._library_provider()
    real = prov.match
    state = {"fail": True}

    def flaky(items):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("peer hiccup")
        return real(items)

    prov.match = flaky
    items = [{"title": "Nemo", "artist": "Nightwish"}]
    assert engine._match_library(items) == [None]
    assert engine._lib_match_cache == {}
    assert engine._match_library(items) == [_lib_id(server)]
    assert len(engine._lib_match_cache) == 1


def test_match_tracks_accepts_string_or_float_duration(lib):
    lib.index.scan([lib.root])
    m = lib.index.match_tracks([
        {"title": "Nemo", "artist": "Nightwish", "duration_s": "245.5"},
        {"title": "Nemo", "artist": "Nightwish", "duration_s": 245.5},
        {"title": "Nemo", "artist": "Nightwish", "duration_s": "n/a"},
    ])
    assert len(m) == 3  # no exception; (a 245s song doesn't match a 1s file)
    assert m[2] == library.track_id_for(lib.files["nemo"])


# -- 7. desktop path --------------------------------------------------------------------------------


def test_library_provider_needs_no_streaming_providers(server, monkeypatch):
    import harmony.providers as providers
    from harmony.models import Service

    engine = server.engine

    def forbidden(*a, **k):
        raise AssertionError("streaming providers must not be built")

    monkeypatch.setattr(providers, "build_providers", forbidden)
    prov = engine._library_provider()
    assert prov is engine._library_provider() and engine._providers is None
    monkeypatch.setattr(providers, "build_providers", lambda *a, **k: {})
    assert engine._ensure_providers()[Service.LOCAL] is prov


# -- 8. parallel Lidarr peer probes -----------------------------------------------------------------


def test_lidarr_peer_probes_in_parallel(server, monkeypatch):
    engine = server.engine
    peers = [{"name": f"p{i}", "host": f"10.0.0.{i}", "port": 8080} for i in (1, 2, 3)]
    monkeypatch.setattr(engine, "instances", lambda: {"instances": peers})

    def peer_call(via, method, path, body=None, timeout=15):
        time.sleep(1)
        if via.startswith("10.0.0.3"):
            return {"enabled": True, "configured": True, "ok": True}
        raise ConnectionError("down")

    monkeypatch.setattr(engine, "_peer_call", peer_call)
    engine._lidarr_cache.clear()
    t0 = time.monotonic()
    found = engine._lidarr_peer()
    assert time.monotonic() - t0 < 2.5
    assert found and found["via"] == "10.0.0.3:8080" and found["name"] == "p3"


# -- 9. ISRC stored upper-case ---------------------------------------------------------------------------


def test_isrc_is_stored_upper_case_and_matched_any_case(lib):
    pytest.importorskip("mutagen")
    _tag(lib.files["hunter"], title="Hunter", artist="Björk", album="Homogenic",
         albumartist="Björk", track="1", isrc="gbum71701001")
    lib.index.scan([lib.root])
    tid = library.track_id_for(lib.files["hunter"])
    assert lib.index.track(tid)["isrc"] == "GBUM71701001"
    assert lib.index.match_tracks([{"isrc": "GbUm71701001", "title": "zzz"}]) == [tid]


# -- 10. annotation cap ----------------------------------------------------------------------------------------


def test_annotate_library_respects_the_cap(server, monkeypatch):
    import harmony.web.api as api

    _enable(server)
    monkeypatch.setattr(api, "_ANNOTATE_MAX", 1)
    tracks = [{"service": "qobuz", "id": f"q{i}", "title": "Nemo", "artist": "Nightwish"}
              for i in range(3)]
    server.engine._annotate_library(tracks)
    assert [t.get("library_id") for t in tracks] == [_lib_id(server), None, None]

