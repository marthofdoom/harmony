package io.github.marthofdoom.harmony

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

data class Instance(val name: String, val host: String, val port: Int) {
    // Bracket IPv6 literals so the URL is well-formed (http://[::1]:8080).
    val baseUrl get() = "http://${if (host.contains(':')) "[$host]" else host}:$port"
}

data class Track(
    val service: String,
    val id: String,
    val title: String,
    val artist: String,
    val album: String?,
    val durationS: Int?,
    val artworkUrl: String?,
    // Entity-navigation extras (best-effort; null on plain search results).
    val trackNumber: Int? = null,
    val year: Int? = null,
    val isrc: String? = null,
)

// ── Entity navigation + smart search models (see api-contract) ─────────────

/** A navigable reference to an artist (service + provider id + display name). */
data class ArtistRef(val service: String, val id: String, val name: String)

/** A navigable reference to an album. */
data class AlbumRef(val service: String, val id: String, val title: String)

/** A Wikipedia-sourced blurb; any MB-optional field may be absent. */
data class Bio(val text: String, val url: String?, val source: String?)

/** An album row. `id` is null for a person's informational "performed-on" rows
 *  (show year + band, but they aren't navigable/playable). */
data class Album(
    val id: String?,
    val title: String,
    val service: String,
    val artist: String,
    val year: Int?,
    val date: String?,
    val artworkUrl: String?,
    val trackCount: Int? = null,
)

/** One inclusive span [start, end]; a null end means "to the present". */
data class Member(
    val name: String,
    val mbid: String?,
    val instruments: List<String>,
    val spans: List<Pair<Int, Int?>>,
    val isCurrent: Boolean,
)

data class Band(
    val name: String,
    val mbid: String?,
    val spans: List<Pair<Int, Int?>>,
    val ref: ArtistRef?,
)

data class ChronoMember(
    val name: String,
    val mbid: String?,
    val instruments: List<String>,
    val spans: List<Pair<Int, Int?>>,
)

data class ChronoAlbum(val title: String, val year: Int, val ref: AlbumRef?)

/** Timeline data backing the member-chronology chart. */
data class Chronology(
    val startYear: Int,
    val endYear: Int,
    val members: List<ChronoMember>,
    val albums: List<ChronoAlbum>,
)

data class ArtistInfo(
    val id: String,
    val name: String,
    val service: String,
    val imageUrl: String?,
    val bio: Bio?,
)

/** Full /api/artist response. `albums` are chronological; for a PERSON they are
 *  the performed-on discography. `chronology` is present only for a dated GROUP. */
data class ArtistDetail(
    val artist: ArtistInfo,
    val kind: String,          // "group" | "person" | "unknown"
    val mbid: String?,
    val albums: List<Album>,
    val singles: List<Album>,
    val topTracks: List<Track>,
    val members: List<Member>,
    val memberOf: List<Band>,
    val chronology: Chronology?,
)

data class AlbumDetail(
    val album: Album,
    val artistRef: ArtistRef?,
    val bio: Bio?,
    val tracks: List<Track>,
)

data class Performer(val name: String, val mbid: String?, val roles: List<String>)

data class TrackDetail(
    val track: Track,
    val albumRef: AlbumRef?,
    val artistRefs: List<ArtistRef>,
    val performers: List<Performer>,
    val mbid: String?,
)

/** The confident artist/person match at the top of a smart-search result. */
data class SmartArtist(
    val ref: ArtistRef,
    val kind: String,
    val mbid: String?,
    val albums: List<Album>,
)

data class Incidental(
    val tracks: List<Track>,
    val artists: List<ArtistRef>,
    val playlists: List<Playlist>,
)

data class SmartSearch(
    val query: String,
    val artist: SmartArtist?,
    val albums: List<Album>,
    val incidental: Incidental,
)

data class Account(val service: String, val authenticated: Boolean, val account: String?, val stale: Boolean)

data class Playlist(val service: String, val id: String, val title: String,
                    val trackCount: Int?, val artworkUrl: String?)

/** A playback renderer. [via] ("host:port") is set for a federated device that is
 *  reached through a peer instance; every call for it must carry that via. */
data class Device(val host: String, val name: String, val kind: String,
                  val via: String? = null, val viaName: String? = null)

/** GET/POST /api/devices/{host}/queue[...] — the server-owned device queue plus
 *  the device's live status (position already corrected server-side). */
data class DeviceSnapshot(
    val tracks: List<Track>,
    val index: Int,
    val shuffle: Boolean,
    val repeat: RepeatMode,
    val playing: Boolean,
    val error: String?,
    val state: String?,
    val positionS: Double?,
    val durationS: Double?,
    val volume: Int?,
)

/** POST /api/credentials/adopt — which services were adopted, which were kept
 *  (already working here), and which were rolled back (didn't work here). */
data class AdoptResult(val synced: List<String>, val kept: List<String>,
                       val rolledBack: List<String>, val imported: List<String>)

data class SyncPlan(val token: String?, val adds: Int, val removes: Int,
                    val unmatched: Int, val notes: List<String>)

data class SyncResult(val added: Int, val removed: Int, val failed: Int)

/** Blocking HTTP client for a Harmony instance's API. Call from a background
 *  dispatcher. Every request carries the personal key (if set) so a key-gated
 *  instance authorizes it. */
class HarmonyApi(var baseUrl: String, var key: String?) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private fun url(path: String) = baseUrl.trimEnd('/') + path

    /** Library artwork comes back as a server path ("/art/<album>.<sig>"); make
     *  it a URL the image loader + media session can fetch. Remote art is untouched. */
    private fun absUrl(u: String?): String? =
        if (u != null && u.startsWith("/")) url(u) else u

    private fun get(path: String): String {
        val b = Request.Builder().url(url(path))
        key?.takeIf { it.isNotEmpty() }?.let { b.header("X-Harmony-Key", it) }
        client.newCall(b.build()).execute().use { resp ->
            val body = resp.body?.string().orEmpty()
            if (resp.code == 401) throw ApiError("This instance requires a personal key.", 401)
            if (!resp.isSuccessful) throw ApiError(errorText(body) ?: "HTTP ${resp.code}", resp.code)
            return body
        }
    }

    private fun post(path: String, json: JSONObject): String {
        val b = Request.Builder().url(url(path))
            .post(json.toString().toRequestBody("application/json".toMediaType()))
        key?.takeIf { it.isNotEmpty() }?.let { b.header("X-Harmony-Key", it) }
        client.newCall(b.build()).execute().use { resp ->
            val body = resp.body?.string().orEmpty()
            if (resp.code == 401) throw ApiError("This instance requires a personal key.", 401)
            if (!resp.isSuccessful) throw ApiError(errorText(body) ?: "HTTP ${resp.code}", resp.code)
            return body
        }
    }

    private fun errorText(body: String): String? =
        runCatching { JSONObject(body).optString("error").ifEmpty { null } }.getOrNull()

    fun health(): Boolean = runCatching { JSONObject(get("/healthz")).optString("status") == "ok" }.getOrDefault(false)

    fun accounts(): List<Account> {
        val arr = JSONObject(get("/api/accounts")).getJSONArray("accounts")
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Account(o.getString("service"), o.optBoolean("authenticated"),
                o.optString("account").ifEmpty { null }, o.optBoolean("stale"))
        }
    }

    fun search(query: String): List<Track> {
        val q = URLEncoder.encode(query, "UTF-8")
        return parseTracks(JSONObject(get("/api/search?q=$q")).optJSONArray("tracks"))
    }

    /** Spec-ordered search: a confident artist match (+ discography) first, then
     *  album-title matches, then incidental tracks/artists/playlists. Fires on
     *  submit only — MB discography is rate-limited to ~1 req/s upstream. */
    fun smartSearch(query: String, service: String): SmartSearch {
        val q = URLEncoder.encode(query, "UTF-8")
        val s = URLEncoder.encode(service, "UTF-8")
        val root = JSONObject(get("/api/search/smart?q=$q&service=$s"))
        val artistObj = root.optJSONObject("artist")
        val artist = if (artistObj == null) null else SmartArtist(
            ref = parseArtistRef(artistObj.optJSONObject("ref"))
                ?: ArtistRef(service, "", artistObj.optString("name")),
            kind = artistObj.optString("kind").ifEmpty { "unknown" },
            mbid = optStr(artistObj, "mbid"),
            albums = parseAlbums(artistObj.optJSONArray("albums")),
        )
        val inc = root.optJSONObject("incidental")
        val incidental = Incidental(
            tracks = parseTracks(inc?.optJSONArray("tracks")),
            artists = parseArtistRefs(inc?.optJSONArray("artists")),
            playlists = parsePlaylists(inc?.optJSONArray("playlists")),
        )
        return SmartSearch(
            query = root.optString("query"),
            artist = artist,
            albums = parseAlbums(root.optJSONArray("albums")),
            incidental = incidental,
        )
    }

    fun artist(service: String, id: String): ArtistDetail {
        val root = JSONObject(get("/api/artist/${enc(service)}/${enc(id)}"))
        val a = root.getJSONObject("artist")
        val info = ArtistInfo(
            id = a.optString("id"), name = a.optString("name"), service = a.optString("service"),
            imageUrl = absUrl(optStr(a, "image_url")), bio = parseBio(a.optJSONObject("bio")),
        )
        return ArtistDetail(
            artist = info,
            kind = root.optString("kind").ifEmpty { "unknown" },
            mbid = optStr(root, "mbid"),
            albums = parseAlbums(root.optJSONArray("albums")),
            singles = parseAlbums(root.optJSONArray("singles")),
            topTracks = parseTracks(root.optJSONArray("top_tracks")),
            members = parseMembers(root.optJSONArray("members")),
            memberOf = parseBands(root.optJSONArray("member_of")),
            chronology = parseChronology(root.optJSONObject("chronology")),
        )
    }

    fun album(service: String, id: String): AlbumDetail {
        val root = JSONObject(get("/api/album/${enc(service)}/${enc(id)}"))
        return AlbumDetail(
            album = parseAlbum(root.getJSONObject("album")),
            artistRef = parseArtistRef(root.optJSONObject("artist_ref")),
            bio = parseBio(root.optJSONObject("bio")),
            tracks = parseTracks(root.optJSONArray("tracks")),
        )
    }

    fun track(service: String, id: String): TrackDetail {
        val root = JSONObject(get("/api/track/${enc(service)}/${enc(id)}"))
        val perfArr = root.optJSONArray("performers")
        val performers = if (perfArr == null) emptyList() else
            (0 until perfArr.length()).map { i ->
                val p = perfArr.getJSONObject(i)
                Performer(p.optString("name"), optStr(p, "mbid"), parseStrList(p.optJSONArray("roles")))
            }
        return TrackDetail(
            track = parseTrack(root.getJSONObject("track")),
            albumRef = parseAlbumRef(root.optJSONObject("album_ref")),
            artistRefs = parseArtistRefs(root.optJSONArray("artist_refs")),
            performers = performers,
            mbid = optStr(root, "mbid"),
        )
    }

    fun playlistTracks(service: String, id: String): List<Track> {
        val s = URLEncoder.encode(service, "UTF-8"); val i = URLEncoder.encode(id, "UTF-8")
        return parseTracks(JSONObject(get("/api/playlists/$s/$i/tracks")).optJSONArray("tracks"))
    }

    /** Resolve a track to a same-origin stream URL the player can hand to
     *  ExoPlayer (the personal key travels as ?key= so the stream is authorized). */
    fun streamUrl(track: Track): String {
        val s = URLEncoder.encode(track.service, "UTF-8"); val id = URLEncoder.encode(track.id, "UTF-8")
        val token = JSONObject(get("/api/resolve?service=$s&id=$id")).getString("token")
        val keyParam = key?.takeIf { it.isNotEmpty() }?.let { "?key=" + URLEncoder.encode(it, "UTF-8") } ?: ""
        return url("/stream/$token$keyParam")
    }

    /** Other instances this hub sees on the LAN (for routing between them). */
    fun instances(): List<Instance> {
        val arr = JSONObject(get("/api/instances")).optJSONArray("instances") ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val o = arr.getJSONObject(i)
            val host = o.optString("host").ifEmpty { return@mapNotNull null }
            Instance(o.optString("name").ifEmpty { host }, host, o.optInt("port", 8080))
        }
    }

    /** Ask this instance to broadcast its audio to [toHost]. A phone passes
     *  transport="rtp" so it can play the stream without a native ROC library. */
    fun audioSend(toHost: String, transport: String = "rtp", latencyMs: Int = 150) {
        post("/api/audio/send", JSONObject().put("to_host", toHost)
            .put("transport", transport).put("latency_ms", latencyMs))
    }

    /** Route audio between this instance and a peer (both are Harmony hubs). */
    fun audioRoute(direction: String, peerHost: String, peerPort: Int, latencyMs: Int = 150) {
        post("/api/audio/route", JSONObject().put("direction", direction)
            .put("peer_host", peerHost).put("peer_port", peerPort).put("latency_ms", latencyMs))
    }

    fun audioStop() { post("/api/audio/stop", JSONObject()) }

    /** Same-origin URL that streams the hub's live audio output as MP3, for the
     *  phone to pull and play (reliable over any network, unlike pushed UDP). */
    fun monitorUrl(): String {
        val keyParam = key?.takeIf { it.isNotEmpty() }?.let { "?key=" + URLEncoder.encode(it, "UTF-8") } ?: ""
        return url("/api/audio/monitor$keyParam")
    }

    // -- playlists / library ------------------------------------------------

    fun playlists(): List<Playlist> =
        parsePlaylists(JSONObject(get("/api/playlists")).optJSONArray("playlists"))

    fun createPlaylist(service: String, title: String) {
        post("/api/playlists", JSONObject().put("service", service).put("title", title))
    }

    fun renamePlaylist(service: String, id: String, title: String) {
        post("/api/playlists/${enc(service)}/${enc(id)}/rename", JSONObject().put("title", title))
    }

    fun deletePlaylist(service: String, id: String) {
        post("/api/playlists/${enc(service)}/${enc(id)}/delete", JSONObject())
    }

    fun addTracks(service: String, id: String, trackIds: List<String>) {
        post("/api/playlists/${enc(service)}/${enc(id)}/add", JSONObject().put("track_ids", JSONArray(trackIds)))
    }

    fun removeTracks(service: String, id: String, trackIds: List<String>) {
        post("/api/playlists/${enc(service)}/${enc(id)}/remove", JSONObject().put("track_ids", JSONArray(trackIds)))
    }

    // -- sync ---------------------------------------------------------------

    fun syncPlan(src: Pair<String, String>, tgt: Pair<String, String>, direction: String): SyncPlan {
        val body = JSONObject()
            .put("source", JSONObject().put("service", src.first).put("id", src.second))
            .put("target", JSONObject().put("service", tgt.first).put("id", tgt.second))
            .put("direction", direction)
        val o = JSONObject(post("/api/sync/plan", body))
        val notes = o.optJSONArray("notes")?.let { (0 until it.length()).map { i -> it.getString(i) } } ?: emptyList()
        return SyncPlan(o.optString("token").ifEmpty { null }, o.optInt("adds"),
            o.optInt("removes"), o.optInt("unmatched"), notes)
    }

    fun syncApply(token: String): SyncResult {
        val o = JSONObject(post("/api/sync/apply", JSONObject().put("token", token)))
        return SyncResult(o.optInt("added"), o.optInt("removed"), o.optInt("failed"))
    }

    // -- credentials (adopt / "sync accounts") ------------------------------

    /** Ask the connected instance to pull [peerHost]:[peerPort]'s encrypted
     *  streaming credentials and decrypt them with the shared personal key. Both
     *  instances must carry the same key. Returns the imported secret keys;
     *  throws ApiError (the peer/decrypt failure surfaces as a 502 with `error`). */
    fun adoptCredentials(peerHost: String, peerPort: Int): AdoptResult {
        val o = JSONObject(post("/api/credentials/adopt",
            JSONObject().put("host", peerHost).put("port", peerPort)))
        return AdoptResult(
            synced = parseStrList(o.optJSONArray("synced")),
            kept = parseStrList(o.optJSONArray("kept")),
            rolledBack = parseStrList(o.optJSONArray("rolled_back")),
            imported = parseStrList(o.optJSONArray("imported")),
        )
    }

    // -- cast to a hub device ----------------------------------------------

    /** This instance's devices plus every mesh peer's (federated ones carry `via`). */
    fun devices(): List<Device> {
        val arr = JSONObject(get("/api/devices?peers=1")).optJSONArray("devices") ?: return emptyList()
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Device(o.getString("host"), o.optString("name").ifEmpty { o.getString("host") },
                o.optString("kind").ifEmpty { "device" }, optStr(o, "via"), optStr(o, "via_name"))
        }
    }

    /** The server-owned queue + live status for a device. */
    fun deviceQueue(host: String, via: String?): DeviceSnapshot {
        val q = if (via.isNullOrEmpty()) "" else "?via=" + enc(via)
        return parseSnapshot(JSONObject(get("/api/devices/${enc(host)}/queue$q")))
    }

    /** Apply one queue op (load/jump/next/prev/enqueue/play_next/move/remove/clear/
     *  shuffle/repeat/stop) on the instance that owns the device's queue. */
    fun deviceQueueOp(host: String, op: String, body: JSONObject = JSONObject(), via: String?): DeviceSnapshot {
        if (!via.isNullOrEmpty()) body.put("via", via)
        return parseSnapshot(JSONObject(post("/api/devices/${enc(host)}/queue/$op", body)))
    }

    /** pause / resume / stop / volume {level 0..100} / seek {level seconds}. */
    fun deviceControl(host: String, action: String, level: Int? = null, via: String? = null) {
        val body = JSONObject()
        if (level != null) body.put("level", level)
        if (!via.isNullOrEmpty()) body.put("via", via)
        post("/api/devices/${enc(host)}/$action", body)
    }

    companion object {
        /** A track as the server's queue expects it. */
        fun trackJson(t: Track): JSONObject = JSONObject()
            .put("service", t.service).put("id", t.id)
            .put("title", t.title).put("artist", t.artist)
            .put("album", t.album ?: JSONObject.NULL)
            .put("art_url", t.artworkUrl?.let { serverRelative(it) } ?: JSONObject.NULL)
            .put("duration_s", t.durationS ?: JSONObject.NULL)

        private val ART_PATH = Regex("^https?://[^/]+(/art/[^/?#]+)$")

        /** Library art back to its server path, so the instance next to a cast
         *  device re-addresses it for THAT device (the phone's view of the server
         *  — a tailnet IP, say — may not be reachable from the speaker). */
        fun serverRelative(u: String): String = ART_PATH.find(u)?.groupValues?.get(1) ?: u

        fun tracksJson(ts: List<Track>): JSONArray = JSONArray().apply { ts.forEach { put(trackJson(it)) } }
    }

    private fun parseSnapshot(o: JSONObject): DeviceSnapshot {
        if (o.has("error") && !o.has("tracks") && !o.isNull("error")) {
            throw ApiError(o.optString("error"), 502)
        }
        fun optDouble(k: String): Double? = if (o.isNull(k) || !o.has(k)) null else o.optDouble(k).takeIf { !it.isNaN() }
        return DeviceSnapshot(
            tracks = parseTracks(o.optJSONArray("tracks")),
            index = o.optInt("index", -1),
            shuffle = o.optBoolean("shuffle"),
            repeat = RepeatMode.fromWire(optStr(o, "repeat")),
            playing = o.optBoolean("playing"),
            error = optStr(o, "error"),
            state = optStr(o, "state")?.lowercase(),
            positionS = optDouble("position_s"),
            durationS = optDouble("duration_s"),
            volume = optDouble("volume")?.toInt(),
        )
    }

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    // ── JSON helpers (org.json, matching the rest of this client) ───────────

    private fun optStr(o: JSONObject, key: String): String? =
        if (o.isNull(key)) null else o.optString(key).ifEmpty { null }

    private fun optInt(o: JSONObject, key: String): Int? =
        if (o.isNull(key)) null else o.optInt(key)

    private fun parseStrList(arr: JSONArray?): List<String> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).mapNotNull { i -> arr.optString(i).ifEmpty { null } }
    }

    private fun parseTrack(o: JSONObject): Track = Track(
        service = o.optString("service"), id = o.optString("id"),
        title = o.optString("title"), artist = o.optString("artist"),
        album = optStr(o, "album"),
        durationS = optInt(o, "duration_s"),
        artworkUrl = absUrl(optStr(o, "artwork_url") ?: optStr(o, "art_url")),
        trackNumber = optInt(o, "track_number"),
        year = optInt(o, "year"),
        isrc = optStr(o, "isrc"),
    )

    private fun parseTracks(arr: JSONArray?): List<Track> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).map { i -> parseTrack(arr.getJSONObject(i)) }
    }

    private fun parseAlbum(o: JSONObject): Album = Album(
        id = optStr(o, "id"),
        title = o.optString("title"),
        service = o.optString("service"),
        artist = o.optString("artist"),
        year = optInt(o, "year"),
        date = optStr(o, "date"),
        artworkUrl = absUrl(optStr(o, "artwork_url")),
        trackCount = optInt(o, "track_count"),
    )

    private fun parseAlbums(arr: JSONArray?): List<Album> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).map { i -> parseAlbum(arr.getJSONObject(i)) }
    }

    private fun parsePlaylists(arr: JSONArray?): List<Playlist> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Playlist(o.optString("service"), o.optString("id"), o.optString("title"),
                optInt(o, "track_count"), absUrl(optStr(o, "artwork_url")))
        }
    }

    private fun parseArtistRef(o: JSONObject?): ArtistRef? {
        if (o == null) return null
        val id = o.optString("id")
        val name = o.optString("name")
        if (id.isEmpty() && name.isEmpty()) return null
        return ArtistRef(o.optString("service"), id, name)
    }

    private fun parseArtistRefs(arr: JSONArray?): List<ArtistRef> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).mapNotNull { i -> parseArtistRef(arr.optJSONObject(i)) }
    }

    private fun parseAlbumRef(o: JSONObject?): AlbumRef? {
        if (o == null) return null
        val id = o.optString("id")
        if (id.isEmpty()) return null
        return AlbumRef(o.optString("service"), id, o.optString("title"))
    }

    private fun parseBio(o: JSONObject?): Bio? {
        if (o == null) return null
        val text = o.optString("text")
        if (text.isEmpty()) return null
        return Bio(text, optStr(o, "url"), optStr(o, "source"))
    }

    /** MB spans arrive as [[start, end|null], ...]. */
    private fun parseSpans(arr: JSONArray?): List<Pair<Int, Int?>> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val span = arr.optJSONArray(i) ?: return@mapNotNull null
            if (span.length() == 0 || span.isNull(0)) return@mapNotNull null
            val start = span.optInt(0)
            val end = if (span.length() < 2 || span.isNull(1)) null else span.optInt(1)
            start to end
        }
    }

    private fun parseMembers(arr: JSONArray?): List<Member> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Member(o.optString("name"), optStr(o, "mbid"),
                parseStrList(o.optJSONArray("instruments")),
                parseSpans(o.optJSONArray("spans")), o.optBoolean("is_current"))
        }
    }

    private fun parseBands(arr: JSONArray?): List<Band> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Band(o.optString("name"), optStr(o, "mbid"),
                parseSpans(o.optJSONArray("spans")), parseArtistRef(o.optJSONObject("ref")))
        }
    }

    private fun parseChronology(o: JSONObject?): Chronology? {
        if (o == null) return null
        val membersArr = o.optJSONArray("members")
        val members = if (membersArr == null) emptyList() else
            (0 until membersArr.length()).map { i ->
                val m = membersArr.getJSONObject(i)
                ChronoMember(m.optString("name"), optStr(m, "mbid"),
                    parseStrList(m.optJSONArray("instruments")), parseSpans(m.optJSONArray("spans")))
            }
        val albumsArr = o.optJSONArray("albums")
        val albums = if (albumsArr == null) emptyList() else
            (0 until albumsArr.length()).mapNotNull { i ->
                val al = albumsArr.getJSONObject(i)
                if (al.isNull("year")) null
                else ChronoAlbum(al.optString("title"), al.optInt("year"), parseAlbumRef(al.optJSONObject("ref")))
            }
        return Chronology(o.optInt("start_year"), o.optInt("end_year"), members, albums)
    }
}

class ApiError(message: String, val code: Int) : Exception(message)
