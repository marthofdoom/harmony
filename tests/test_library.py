"""Local library + the Lidarr ↔ library loop — offline, against a GENERATED library.

A real folder of real (tiny) audio files is written per test: WAV via the
stdlib, tagged with ID3 through mutagen where a test needs tags, laid out the
way Lidarr files albums. That exercises the actual scanner, index, provider,
HTTP routes, stream proxy, relay and webhook end to end — no Lidarr, no
network (the Lidarr REST client is mocked where it's involved).
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from harmony import library
from harmony.library import ArtSigner, LibraryIndex, map_path
from harmony.lidarr import LidarrClient, queue_item, webhook_paths
from harmony.streamio import LocalFileResponse, open_stream

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-cover-bytes"


# -- the generated library ----------------------------------------------------


def _wav(path: Path, seconds: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x01\x00" * int(8000 * seconds))
    return path


def _tag(path: Path, **tags: str) -> None:
    """ID3-tag a WAV (title/artist/album/albumartist/track/date/rg/art)."""
    mutagen_wave = pytest.importorskip("mutagen.wave")
    from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, TXXX

    f = mutagen_wave.WAVE(str(path))
    f.add_tags()
    frames = {"title": TIT2, "artist": TPE1, "album": TALB, "albumartist": TPE2,
              "track": TRCK, "date": TDRC}
    for key, frame in frames.items():
        if key in tags:
            f.tags.add(frame(encoding=3, text=tags[key]))
    if "rg" in tags:
        f.tags.add(TXXX(encoding=3, desc="MusicBrainz Release Group Id", text=tags["rg"]))
    if tags.get("art"):
        f.tags.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=PNG))
    f.save()


def make_library(root: Path) -> dict[str, Path]:
    """Two albums the way Lidarr files them, plus junk that must be ignored."""
    a1 = root / "Nightwish" / "Once (2004)"
    files = {
        "dark": _wav(a1 / "Nightwish - Once - 01 - Dark Chest of Wonders.wav"),
        "wish": _wav(a1 / "Nightwish - Once - 02 - Wish I Had an Angel.wav"),
        "nemo": _wav(a1 / "Nightwish - Once - 04 - Nemo.wav"),
    }
    (a1 / "cover.jpg").write_bytes(b"\xff\xd8\xff-jpeg-cover")
    a2 = root / "Björk" / "Homogenic (1997)"
    files["hunter"] = _wav(a2 / "01 - Hunter.wav")
    files["joga"] = _wav(a2 / "02 - Jóga.wav")
    (root / "Nightwish" / "notes.txt").write_text("not audio")
    hidden = root / ".trash"
    _wav(hidden / "deleted.wav")
    return files


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "music"
    files = make_library(root)
    index = LibraryIndex(tmp_path / "library.db")
    yield SimpleNamespace(root=root, files=files, index=index, tmp=tmp_path)
    index.close()


# -- scanning + index ------------------------------------------------------------


def test_path_layout_fallback_parses_lidarr_naming():
    t = library._path_tags(Path("/m/Nightwish/Once (2004)/Nightwish - Once - 04 - Nemo.flac"))
    assert (t.title, t.artist, t.album, t.album_artist, t.track_no, t.year) == (
        "Nemo", "Nightwish", "Once", "Nightwish", 4, 2004)
    t = library._path_tags(Path("/m/Björk/Homogenic/CD2/03 - Jóga.mp3"))
    assert (t.title, t.album, t.artist, t.disc_no, t.track_no) == ("Jóga", "Homogenic", "Björk", 2, 3)
    t = library._path_tags(Path("/m/A/B/1-07 Song Name.flac"))
    assert (t.disc_no, t.track_no, t.title) == (1, 7, "Song Name")


def test_scan_indexes_audio_only_and_groups_albums(lib):
    report = lib.index.scan([lib.root])
    assert report.added == 5 and report.errors == 0          # no .txt, no dot-dir
    assert lib.index.stats() == {"tracks": 5, "albums": 2, "artists": 2}
    album_id = library.album_id_for("Nightwish", "Once")
    tracks = lib.index.album_tracks(album_id)
    assert [t["title"] for t in tracks] == ["Dark Chest of Wonders", "Wish I Had an Angel", "Nemo"]
    assert [t["track_no"] for t in tracks] == [1, 2, 4]
    assert tracks[0]["year"] == 2004 and tracks[0]["mime"] == "audio/wav"
    assert tracks[0]["duration_s"] in (None, 1)               # 1s when mutagen reads it


def test_rescan_is_incremental_and_prunes_deleted_files(lib):
    lib.index.scan([lib.root])
    again = lib.index.scan([lib.root])
    assert (again.added, again.updated, again.removed) == (0, 0, 0)
    lib.files["nemo"].unlink()
    report = lib.index.scan([lib.root / "Nightwish"])          # scoped rescan prunes too
    assert report.removed == 1
    assert lib.index.stats()["tracks"] == 4


def test_mutagen_tags_win_over_the_path(lib):
    pytest.importorskip("mutagen")
    _tag(lib.files["hunter"], title="Hunter", artist="Björk", album="Homogenic",
         albumartist="Björk", track="1/10", date="1997-09-22", rg="rg-homogenic", art="1")
    lib.index.scan([lib.root])
    row = lib.index.track(library.track_id_for(lib.files["hunter"]))
    assert row["mb_releasegroup"] == "rg-homogenic" and row["date"] == "1997-09-22"
    assert row["duration_s"] == 1
    # Exact match by release group, fuzzy by title+artist (accents folded).
    assert lib.index.find_album("anything", mbid="rg-homogenic")["album"] == "Homogenic"
    assert lib.index.find_album("homogenic", "bjork")["album"] == "Homogenic"
    assert lib.index.find_album("Homogenic Live", "Someone Else") is None
    # Embedded art when the folder has no cover image.
    art = lib.index.album_art(row["album_id"])
    assert art == (PNG, "image/png")


def test_search_ranks_and_folds_accents(lib):
    lib.index.scan([lib.root])
    r = lib.index.search("joga", kinds=("tracks", "albums", "artists"))
    assert [t["title"] for t in r["tracks"]] == ["Jóga"]
    r = lib.index.search("nightwish once", kinds=("tracks", "albums", "artists"))
    assert r["albums"][0]["album"] == "Once"
    assert len(r["tracks"]) == 3
    assert lib.index.search("björk", kinds=("artists",))["artists"][0]["name"] == "Björk"
    assert lib.index.search("zzzz", kinds=("tracks",))["tracks"] == []


def test_folder_cover_and_signed_art_urls(lib, tmp_path):
    lib.index.scan([lib.root])
    album_id = library.album_id_for("Nightwish", "Once")
    assert lib.index.album_art(album_id) == (b"\xff\xd8\xff-jpeg-cover", "image/jpeg")
    signer = ArtSigner.for_dir(tmp_path / "secrets")
    path = signer.path_for(album_id)
    token = path.rsplit("/", 1)[-1]
    assert signer.verify(token) == album_id
    assert signer.verify(album_id + ".0000") is None              # forged signature
    assert ArtSigner.for_dir(tmp_path / "secrets").verify(token) == album_id  # persisted key
    assert (tmp_path / "secrets" / "library-art.key").stat().st_mode & 0o777 == 0o600


def test_map_path_longest_prefix_on_segments():
    pm = [{"remote": "/music", "local": "/srv/media/music"},
          {"remote": "/music/lossless", "local": "/mnt/flac"}]
    assert map_path("/music/A/B.flac", pm) == "/srv/media/music/A/B.flac"
    assert map_path("/music/lossless/A.flac", pm) == "/mnt/flac/A.flac"
    assert map_path("/musical/x", pm) == "/musical/x"            # not a segment match
    assert map_path("/music", pm) == "/srv/media/music"
    assert map_path("/other/x", []) == "/other/x"


# -- file streaming ------------------------------------------------------------------


def test_local_file_response_ranges(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(bytes(range(100)))
    full = LocalFileResponse(p, "audio/wav", None)
    assert full.status_code == 200 and b"".join(full.iter_content(7)) == bytes(range(100))
    part = LocalFileResponse(p, None, "bytes=10-19")
    assert part.status_code == 206 and part.headers["Content-Range"] == "bytes 10-19/100"
    assert b"".join(part.iter_content()) == bytes(range(10, 20))
    tail = LocalFileResponse(p, None, "bytes=-5")
    assert b"".join(tail.iter_content()) == bytes(range(95, 100))
    open_ended = LocalFileResponse(p, None, "bytes=90-")
    assert open_ended.headers["Content-Length"] == "10"
    bad = LocalFileResponse(p, None, "bytes=500-")
    assert bad.status_code == 416 and list(bad.iter_content()) == []
    with pytest.raises(FileNotFoundError):
        open_stream((tmp_path / "missing.wav").as_uri())


def test_relay_serves_a_library_file_with_range(tmp_path):
    from harmony.models import StreamSource
    from harmony.playback.relay import RelayServer

    p = _wav(tmp_path / "a b" / "x.wav")
    data = p.read_bytes()
    relay = RelayServer(bind_host="127.0.0.1", port=0)
    relay.start()
    try:
        src = StreamSource(url=p.as_uri(), mime_type="audio/wav")
        token = relay.register(lambda: src, allow_icy=False)
        url = f"http://127.0.0.1:{relay.port}/play/{token}"
        req = urllib.request.Request(url, headers={"Range": "bytes=4-11"})
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - loopback
            assert resp.status == 206 and resp.read() == data[4:12]
            assert resp.headers["Content-Type"] == "audio/wav"
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            assert resp.read() == data
    finally:
        relay.stop()


# -- the server end to end ------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The real HTTP server over a fresh Engine whose config/data live in tmp."""
    from harmony import config
    from harmony.web import server as srv
    from harmony.web.api import Engine

    cfg = tmp_path / "config"
    data = tmp_path / "data"
    cfg.mkdir()
    data.mkdir()
    monkeypatch.setattr(config, "config_dir", lambda: cfg)
    monkeypatch.setattr(config, "data_dir", lambda: data)
    # No streaming providers: nothing here may touch the network.
    import harmony.providers as providers
    monkeypatch.setattr(providers, "build_providers", lambda *a, **k: {})
    monkeypatch.setenv("HARMONY_LIDARR_API_KEY", "")
    engine = Engine()
    # Offline: no MusicBrainz/Wikipedia overlay on search and artist pages.
    monkeypatch.setattr(engine, "_mb_enabled", lambda: False)
    monkeypatch.setattr(srv, "_engine", engine)

    root = tmp_path / "music"
    files = make_library(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.HarmonyHTTPRequestHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    engine.set_http_port(httpd.server_address[1])
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{httpd.server_address[1]}", engine=engine,
                              root=root, files=files, tmp=tmp_path)
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - loopback test server
        return resp.status, dict(resp.headers), resp.read()


def _json(url: str):
    return json.loads(_get(url)[2])


def _post(url: str, obj: dict):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


def _wait_scan(engine, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = engine.library_status()
        if not st["scan"].get("running") and st["scan"].get("finished"):
            return st
        time.sleep(0.02)
    raise AssertionError("library scan didn't finish")


def _enable(s, paths=None) -> dict:
    _post(s.url + "/api/library/config", {"enabled": True, "paths": paths or [str(s.root)]})
    return _wait_scan(s.engine)


def test_library_disabled_by_default(server):
    st = _json(server.url + "/api/library")
    assert st["enabled"] is False and st["stats"]["tracks"] == 0
    assert _json(server.url + "/api/library/browse")["enabled"] is False


def test_enable_scan_browse_search_album_stream_art(server):
    st = _enable(server)
    assert st["stats"] == {"tracks": 5, "albums": 2, "artists": 2}
    assert st["scan"]["added"] == 5 and st["missing"] == []

    browse = _json(server.url + "/api/library/browse?order=title")
    assert [a["title"] for a in browse["albums"]] == ["Homogenic", "Once"]
    assert all(a["service"] == "local" for a in browse["albums"])
    assert sorted(a["name"] for a in browse["artists"]) == ["Björk", "Nightwish"]

    # Smart search surfaces the library like any service.
    smart = _json(server.url + "/api/search/smart?q=nightwish")
    assert smart["artist"]["ref"]["service"] == "local"
    assert [a["title"] for a in smart["artist"]["albums"]] == ["Once"]
    accounts = _json(server.url + "/api/accounts")["accounts"]
    assert {"service": "local", "authenticated": True, "account": "5 tracks",
            "stale": False} in accounts

    once = next(a for a in browse["albums"] if a["title"] == "Once")
    page = _json(server.url + f"/api/album/local/{once['id']}")
    assert page["album"]["year"] == 2004 and page["album"]["track_count"] == 3
    assert page["artist_ref"]["name"] == "Nightwish"
    assert [t["track_number"] for t in page["tracks"]] == [1, 2, 4]

    artist = _json(server.url + f"/api/artist/local/{page['artist_ref']['id']}")
    assert [a["title"] for a in artist["albums"]] == ["Once"]
    assert len(artist["top_tracks"]) == 3

    # Resolve → stream with Range, straight from disk.
    t = page["tracks"][2]
    r = _json(server.url + f"/api/resolve?service=local&id={t['id']}")
    assert r["mime"] == "audio/wav" and r["label"].startswith("Library")
    data = server.files["nemo"].read_bytes()
    status, headers, body = _get(server.url + f"/stream/{r['token']}", {"Range": "bytes=0-99"})
    assert status == 206 and body == data[:100]
    assert headers["Content-Range"] == f"bytes 0-99/{len(data)}"
    status, _, body = _get(server.url + f"/stream/{r['token']}")
    assert status == 200 and body == data

    # Artwork: a signed path that needs no key; a forged one 404s.
    assert t["artwork_url"].startswith("/art/")
    status, headers, body = _get(server.url + t["artwork_url"])
    assert body == b"\xff\xd8\xff-jpeg-cover" and headers["Content-Type"] == "image/jpeg"
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server.url + f"/art/{once['id']}.deadbeef")
    assert exc.value.code == 404


def test_art_is_outside_the_key_gate_but_the_api_is_not(server, monkeypatch):
    from harmony.config import Settings

    _enable(server)
    album = _json(server.url + "/api/library/browse")["albums"][0]
    s = Settings.load()
    s.personal_key = "sekrit"
    s.save()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server.url + "/api/library")
    assert exc.value.code == 401
    assert _get(server.url + album["artwork_url"])[0] == 200


def test_missing_file_is_a_clear_error_not_a_crash(server):
    _enable(server)
    album = next(a for a in _json(server.url + "/api/library/browse")["albums"] if a["title"] == "Once")
    tid = _json(server.url + f"/api/album/local/{album['id']}")["tracks"][0]["id"]
    server.files["dark"].unlink()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server.url + f"/api/resolve?service=local&id={tid}")
    assert exc.value.code == 502 and b"missing from disk" in exc.value.read()


def test_library_has_no_playlists(server):
    _enable(server)
    assert _json(server.url + "/api/playlists")["playlists"] == []
    with pytest.raises(urllib.error.HTTPError):
        _post(server.url + "/api/playlists", {"service": "local", "title": "x"})


def test_removing_a_folder_prunes_its_tracks(server):
    _enable(server)
    nightwish_only = [str(server.root / "Nightwish")]
    _post(server.url + "/api/library/config", {"paths": nightwish_only})
    st = _wait_scan(server.engine)
    assert st["paths"] == nightwish_only and st["stats"]["tracks"] == 3


def test_scan_refuses_paths_outside_the_library(server, tmp_path):
    _enable(server, [str(server.root / "Nightwish")])
    r = _post(server.url + "/api/library/scan", {"paths": [str(server.root / "Björk")]})
    assert r["ok"] is False
    assert _json(server.url + "/api/library")["stats"]["tracks"] == 3


# -- the Lidarr import webhook -------------------------------------------------------


def _lidarr_import_payload(remote_album_dir: str, names: list[str]) -> dict:
    """What Lidarr POSTs on an import (eventType Download), trimmed to what matters."""
    return {
        "eventType": "Download",
        "artist": {"id": 1, "name": "Radiohead", "path": remote_album_dir.rsplit("/", 1)[0]},
        "album": {"id": 7, "title": "OK Computer", "foreignAlbumId": "rg-okc"},
        "trackFiles": [{"id": i, "path": f"{remote_album_dir}/{n}", "quality": "FLAC"}
                       for i, n in enumerate(names)],
        "isUpgrade": False,
    }


def test_webhook_import_indexes_new_album_through_path_mapping(server):
    _enable(server)
    _post(server.url + "/api/library/config",
          {"path_map": [{"remote": "/data/music", "local": str(server.root)}], "paths": [str(server.root)]})
    _wait_scan(server.engine)
    # Lidarr imports an album into its view of the root folder...
    album_dir = server.root / "Radiohead" / "OK Computer (1997)"
    names = ["Radiohead - OK Computer - 01 - Airbag.wav", "Radiohead - OK Computer - 02 - Paranoid Android.wav"]
    for n in names:
        _wav(album_dir / n)
    # ...and calls back with ITS paths.
    r = _post(server.url + "/api/lidarr/webhook",
              _lidarr_import_payload("/data/music/Radiohead/OK Computer (1997)", names))
    assert r["ok"] and r["event"] == "Download" and r["indexed"] == 2
    assert r["outside_library"] == []
    # Immediately searchable + playable.
    smart = _json(server.url + "/api/search/smart?q=ok%20computer")
    okc = next(a for a in smart["albums"] if a["title"] == "OK Computer")
    assert okc["service"] == "local" and okc["year"] == 1997
    state = _post(server.url + "/api/library/state",
                  {"albums": [{"title": "OK Computer", "artist": "Radiohead"}]})
    assert state["states"][0]["state"] == "library"
    assert state["states"][0]["ref"] == {"service": "local", "id": okc["id"], "title": "OK Computer"}


def test_webhook_test_event_and_unmapped_paths(server):
    assert _post(server.url + "/api/lidarr/webhook", {"eventType": "Test"})["event"] == "Test"
    _enable(server)
    r = _post(server.url + "/api/lidarr/webhook",
              _lidarr_import_payload("/elsewhere/Radiohead/OK Computer", ["01 - Airbag.wav"]))
    assert r["indexed"] == 0 and r["outside_library"] == ["/elsewhere/Radiohead/OK Computer/01 - Airbag.wav"]


def test_webhook_delete_and_rename_rescan(server):
    _enable(server)
    shutil.rmtree(server.root / "Björk")
    r = _post(server.url + "/api/lidarr/webhook", {
        "eventType": "AlbumDelete", "artist": {"path": str(server.root / "Björk")},
        "album": {"title": "Homogenic"}, "deletedFiles": []})
    assert r["rescanning"] == [str(server.root / "Björk")]
    assert _wait_scan(server.engine)["stats"]["albums"] == 1
    # Rename: files moved; Lidarr names the new + previous paths.
    old = server.files["nemo"]
    new = old.with_name("Nightwish - Once - 04 - Nemo (Remastered).wav")
    old.rename(new)
    r = _post(server.url + "/api/lidarr/webhook", {
        "eventType": "Rename", "artist": {"path": str(server.root / "Nightwish")},
        "renamedTrackFiles": [{"path": str(new), "previousPath": str(old)}]})
    assert r["indexed"] == 1 and r["removed"] == 1
    titles = [t["title"] for t in _json(server.url + "/api/search/smart?q=nemo")["incidental"]["tracks"]]
    assert titles == ["Nemo (Remastered)"]


def test_webhook_paths_shapes():
    assert webhook_paths({"eventType": "Download", "artist": {"path": "/m/A"},
                          "trackFiles": [{"path": "/m/A/B/1.flac"}],
                          "deletedFiles": [{"path": "/m/A/B/1.mp3"}]}) == {
        "event": "Download", "files": ["/m/A/B/1.flac"], "deleted": ["/m/A/B/1.mp3"], "dirs": []}
    assert webhook_paths({"eventType": "Retag", "artist": {"path": "/m/A"},
                          "trackFile": {"path": "/m/A/B/2.flac"}})["files"] == ["/m/A/B/2.flac"]
    assert webhook_paths({"eventType": "Rename", "artist": {"path": "/m/A"}})["dirs"] == ["/m/A"]
    assert webhook_paths({"eventType": "ArtistDelete", "artist": {"path": "/m/A"}})["dirs"] == ["/m/A"]
    assert webhook_paths({"eventType": "Grab", "artist": {"path": "/m/A"}}) == {
        "event": "Grab", "files": [], "deleted": [], "dirs": []}
    assert webhook_paths({}) == {"event": "", "files": [], "deleted": [], "dirs": []}


# -- registering the webhook in Lidarr (REST mocked) ------------------------------------


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload


WEBHOOK_SCHEMA = {
    "implementation": "Webhook", "configContract": "WebhookSettings", "name": "",
    "onGrab": False, "supportsOnGrab": True, "onReleaseImport": False, "supportsOnReleaseImport": True,
    "onUpgrade": False, "supportsOnUpgrade": True, "onRename": False, "supportsOnRename": True,
    "onHealthIssue": False, "supportsOnHealthIssue": True,
    "onAlbumDelete": False, "supportsOnAlbumDelete": False,
    "fields": [{"name": "url", "value": ""}, {"name": "method", "value": 1},
               {"name": "username", "value": ""}, {"name": "password", "value": ""}],
}


def _lidarr(monkeypatch, existing=None):
    calls = []

    def handler(method, url, **kw):
        path = url.split("/api/v1/", 1)[1]
        calls.append((method, path, kw.get("json")))
        if path == "notification/schema":
            return _Resp(200, [{"implementation": "Slack"}, WEBHOOK_SCHEMA])
        if path == "notification" and method == "GET":
            return _Resp(200, existing or [])
        if path.startswith("notification") and method in ("POST", "PUT"):
            return _Resp(200, {**(kw.get("json") or {}), "id": 9})
        raise AssertionError(f"unexpected {method} {path}")

    import harmony.lidarr as lid
    monkeypatch.setattr(lid.requests, "request", handler)
    return calls


def test_register_webhook_creates_from_schema(monkeypatch):
    calls = _lidarr(monkeypatch)
    out = LidarrClient("http://l", "k").register_webhook("http://h:8080/api/lidarr/webhook?key=z")
    method, path, body = calls[-1]
    assert (method, path) == ("POST", "notification")
    assert body["name"] == "Harmony" and body["implementation"] == "Webhook"
    assert body["onReleaseImport"] and body["onUpgrade"] and body["onRename"] and body["onGrab"]
    assert body["onHealthIssue"] is False                        # not ours to ask for
    assert body["onAlbumDelete"] is False                        # unsupported by this Lidarr
    fields = {f["name"]: f["value"] for f in body["fields"]}
    assert fields["url"] == "http://h:8080/api/lidarr/webhook?key=z" and fields["method"] == 1
    assert out["id"] == 9


def test_register_webhook_updates_existing(monkeypatch):
    existing = [{**WEBHOOK_SCHEMA, "id": 4, "name": "Harmony",
                 "fields": [{"name": "url", "value": "http://old"}, {"name": "method", "value": 2}]}]
    calls = _lidarr(monkeypatch, existing)
    LidarrClient("http://l", "k").register_webhook("http://new/api/lidarr/webhook")
    method, path, body = calls[-1]
    assert (method, path) == ("PUT", "notification/4")
    assert {f["name"]: f["value"] for f in body["fields"]} == {
        "url": "http://new/api/lidarr/webhook", "method": 1}


class _FakeLidarr:
    def __init__(self, albums=None, queue=None):
        self._albums = albums or []
        self._queue = queue or []
        self.webhook_url = None
        self.tested = False

    def root_folders(self):
        return [{"path": "/data/music"}]

    def register_webhook(self, url):
        self.webhook_url = url
        return {"id": 3, "name": "Harmony"}

    def test_webhook(self, notification):
        self.tested = True

    def albums(self):
        return self._albums

    def queue(self):
        return self._queue


def test_connect_library_adopts_mapped_root_and_registers_webhook(server, monkeypatch):
    from harmony.config import Settings

    s = Settings.load()
    s.personal_key = "pk/1"
    s.lidarr_path_map = [{"remote": "/data/music", "local": str(server.root)}]
    s.save()
    fake = _FakeLidarr()
    monkeypatch.setattr(server.engine, "_lidarr_client", lambda: (fake, Settings.load()))
    out = server.engine.lidarr_connect_library("http://192.168.1.5:8080/")
    assert out["roots"] == [str(server.root)] and out["tested"] is True
    assert fake.webhook_url == "http://192.168.1.5:8080/api/lidarr/webhook?key=pk%2F1"
    assert out["webhook_url"] == "http://192.168.1.5:8080/api/lidarr/webhook"   # key not echoed
    st = _wait_scan(server.engine)
    assert st["enabled"] and st["paths"] == [str(server.root)] and st["stats"]["tracks"] == 5


def test_connect_library_route_defaults_callback_to_the_host_header(server, monkeypatch):
    from harmony.config import Settings

    fake = _FakeLidarr()
    monkeypatch.setattr(server.engine, "_lidarr_client", lambda: (fake, Settings.load()))
    r = _post(server.url + "/api/lidarr/connect-library", {})
    assert fake.webhook_url == server.url + "/api/lidarr/webhook"
    assert r["roots"] == ["/data/music"] and r["missing"] == ["/data/music"]


# -- downloads + per-album state ------------------------------------------------------


QUEUE = [{"id": 1, "albumId": 70, "title": "Radiohead-OK.Computer-FLAC", "size": 400, "sizeleft": 100,
          "status": "downloading", "trackedDownloadState": "downloading",
          "trackedDownloadStatus": "ok", "timeleft": "00:01:00", "protocol": "torrent",
          "downloadClient": "qBittorrent",
          "album": {"id": 70, "title": "OK Computer", "foreignAlbumId": "rg-okc"},
          "artist": {"artistName": "Radiohead"}}]
ALBUMS = [
    {"id": 70, "title": "OK Computer", "foreignAlbumId": "rg-okc", "monitored": True,
     "artist": {"artistName": "Radiohead"}, "statistics": {"trackFileCount": 0, "totalTrackCount": 12}},
    {"id": 71, "title": "Kid A", "foreignAlbumId": "rg-kida", "monitored": True,
     "artist": {"artistName": "Radiohead"}, "statistics": {"trackFileCount": 2, "totalTrackCount": 10}},
    {"id": 72, "title": "Amnesiac", "foreignAlbumId": "rg-amn", "monitored": True,
     "artist": {"artistName": "Radiohead"}, "statistics": {"trackFileCount": 11, "totalTrackCount": 11}},
]


def test_queue_item_maps_progress_and_names():
    q = queue_item(QUEUE[0])
    assert (q["title"], q["artist"], q["progress"], q["status"], q["client"]) == (
        "OK Computer", "Radiohead", 75.0, "downloading", "qBittorrent")
    assert queue_item({"size": 0})["progress"] == 0.0


def test_album_states_cover_the_whole_loop(server, monkeypatch):
    from harmony.config import Settings

    _enable(server)
    s = Settings.load()
    s.lidarr_enabled, s.lidarr_url = True, "http://lidarr"
    s.save()
    monkeypatch.setenv("HARMONY_LIDARR_API_KEY", "k")
    fake = _FakeLidarr(ALBUMS, QUEUE)
    monkeypatch.setattr(server.engine, "_lidarr_client", lambda: (fake, Settings.load()))
    r = _post(server.url + "/api/library/state", {"albums": [
        {"title": "Once", "artist": "Nightwish"},
        {"title": "Whatever", "artist": "Radiohead", "mbid": "rg-okc"},
        {"title": "Kid A", "artist": "Radiohead"},
        {"title": "Amnesiac", "artist": "Radiohead"},
        {"title": "Pablo Honey", "artist": "Radiohead"},
    ]})
    states = [x["state"] for x in r["states"]]
    assert states == ["library", "downloading", "wanted", "imported", "none"]
    assert r["states"][1]["progress"] == 75.0
    assert (r["states"][2]["have"], r["states"][2]["total"]) == (2, 10)
    q = _json(server.url + "/api/lidarr/queue")["items"]
    assert q[0]["title"] == "OK Computer" and q[0]["progress"] == 75.0


def test_album_states_without_lidarr_or_library(server):
    r = _post(server.url + "/api/library/state", {"albums": [{"title": "Once", "artist": "Nightwish"}]})
    assert r == {"states": [{"title": "Once", "artist": "Nightwish", "state": "none"}], "lidarr": False}


def test_cast_meta_art_is_made_absolute_for_the_device(server, monkeypatch):
    seen = {}

    class Caster:
        def cast(self, host, service, track_id, meta, kind, device_info):
            seen.update(meta)
            return {"ok": True}

    monkeypatch.setattr(server.engine, "_caster", lambda: Caster())
    monkeypatch.setattr(server.engine, "_device_kind", lambda host: ("wiim", {}))
    server.engine._cast_direct("127.0.0.1", "local", "t1", {"art_url": "/art/abc.def", "title": "x"})
    assert seen["art_url"] == f"http://127.0.0.1:{server.engine._http_port}/art/abc.def"
    server.engine._cast_direct("127.0.0.1", "qobuz", "t1", {"art_url": "https://cdn/x.jpg"})
    assert seen["art_url"] == "https://cdn/x.jpg"


# -- review fixes ---------------------------------------------------------------------


def test_album_delete_with_boolean_deleted_files_does_not_crash(server):
    # Lidarr sends deletedFiles as a *bool* on AlbumDelete/ArtistDelete.
    assert webhook_paths({"eventType": "AlbumDelete", "artist": {"path": "/m/A"},
                          "deletedFiles": True})["dirs"] == ["/m/A"]
    _enable(server)
    shutil.rmtree(server.root / "Björk")
    r = _post(server.url + "/api/lidarr/webhook", {
        "eventType": "ArtistDelete", "artist": {"path": str(server.root / "Björk")},
        "deletedFiles": True})
    assert r["ok"] and r["rescanning"] == [str(server.root / "Björk")]
    assert _wait_scan(server.engine)["stats"]["artists"] == 1


def test_dotdot_paths_cannot_escape_the_library(server, tmp_path):
    outside = tmp_path / "private"
    _wav(outside / "secret.wav")
    _enable(server, [str(server.root / "Nightwish")])
    sneaky = str(server.root / "Nightwish" / ".." / ".." / "private")
    assert _post(server.url + "/api/library/scan", {"paths": [sneaky]})["ok"] is False
    r = _post(server.url + "/api/lidarr/webhook", {
        "eventType": "Download", "trackFiles": [{"path": sneaky + "/secret.wav"}]})
    assert r["indexed"] == 0 and r["outside_library"] == [sneaky + "/secret.wav"]
    assert _json(server.url + "/api/library")["stats"]["tracks"] == 3
    assert not library.within(sneaky, [server.root / "Nightwish"])


def test_a_missing_library_folder_keeps_its_tracks(server):
    _enable(server)
    hidden = server.root.with_name("music-unmounted")
    server.root.rename(hidden)                       # the share "unmounts"
    _post(server.url + "/api/library/scan", {})
    st = _wait_scan(server.engine)
    assert st["stats"]["tracks"] == 5 and st["missing"] == [str(server.root)]
    # A webhook for a folder on the missing share mustn't prune it either.
    _post(server.url + "/api/lidarr/webhook", {
        "eventType": "AlbumDelete", "artist": {"path": str(server.root / "Nightwish")}})
    assert _json(server.url + "/api/library")["stats"]["tracks"] == 5
    hidden.rename(server.root)


def test_untagged_compilation_stays_one_album(lib):
    pytest.importorskip("mutagen")
    comp = lib.root / "Various Artists" / "Now 80 (2010)"
    for i, artist in enumerate(["A-ha", "Toto", "Europe"], 1):
        f = _wav(comp / f"0{i} - Song {i}.wav")
        _tag(f, title=f"Song {i}", artist=artist, album="Now 80", track=str(i))  # no albumartist
    lib.index.scan([lib.root])
    found = lib.index.search("now 80", kinds=("albums",))["albums"]
    assert len(found) == 1 and found[0]["track_count"] == 3
    assert found[0]["album_artist"] == "Various Artists"
    tracks = lib.index.album_tracks(found[0]["album_id"])
    assert [t["artist"] for t in tracks] == ["A-ha", "Toto", "Europe"]


def test_library_tracks_refused_via_a_peer(server):
    _enable(server)
    t = {"service": "local", "id": "x", "title": "Nemo"}
    for url, body in ((server.url + "/api/devices/10.0.0.9/queue/load", {"tracks": [t], "via": "peer:8080"}),
                      (server.url + "/api/devices/10.0.0.9/play", {"service": "local", "id": "x", "via": "peer:8080"})):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(url, body)
        assert exc.value.code == 400 and b"only on this instance" in exc.value.read()


def test_relay_serves_library_m4a_as_a_file_even_when_icy_is_asked(tmp_path):
    from harmony.models import StreamSource
    from harmony.playback.relay import RelayServer

    p = tmp_path / "alac.m4a"
    p.write_bytes(b"\x00\x00\x00\x20ftypM4A " + bytes(200))
    relay = RelayServer(bind_host="127.0.0.1", port=0)
    relay.start()
    try:
        src = StreamSource(url=p.as_uri(), mime_type="audio/mp4", container="m4a")
        token = relay.register(lambda: src, title="T", artist="A", allow_icy=True)
        req = urllib.request.Request(f"http://127.0.0.1:{relay.port}/play/{token}",
                                     headers={"Icy-MetaData": "1"})
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            assert resp.headers.get("icy-metaint") is None
            assert resp.read() == p.read_bytes()
    finally:
        relay.stop()


def test_connect_library_saves_path_map_and_scans_once(server, monkeypatch):
    from harmony.config import Settings

    fake = _FakeLidarr()
    monkeypatch.setattr(server.engine, "_lidarr_client", lambda: (fake, Settings.load()))
    scans = []
    real = server.engine.library_scan
    monkeypatch.setattr(server.engine, "library_scan", lambda *a, **k: scans.append(a) or real(*a, **k))
    r = _post(server.url + "/api/lidarr/connect-library", {
        "callback_url": "http://h:1", "path_map": [{"remote": "/data/music", "local": str(server.root)}]})
    assert r["roots"] == [str(server.root)] and r["missing"] == []
    assert len(scans) == 1
    assert Settings.load().lidarr_path_map == [{"remote": "/data/music", "local": str(server.root)}]
    assert _wait_scan(server.engine)["stats"]["tracks"] == 5
