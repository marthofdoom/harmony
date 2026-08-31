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
)

data class Account(val service: String, val authenticated: Boolean, val account: String?, val stale: Boolean)

data class Playlist(val service: String, val id: String, val title: String,
                    val trackCount: Int?, val artworkUrl: String?)

data class Device(val host: String, val name: String, val kind: String)

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

    fun playlists(): List<Playlist> {
        val arr = JSONObject(get("/api/playlists")).optJSONArray("playlists") ?: return emptyList()
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Playlist(o.getString("service"), o.getString("id"), o.optString("title"),
                if (o.isNull("track_count")) null else o.optInt("track_count"),
                o.optString("artwork_url").ifEmpty { null })
        }
    }

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

    // -- cast to a hub device ----------------------------------------------

    fun devices(): List<Device> {
        val arr = JSONObject(get("/api/devices")).optJSONArray("devices") ?: return emptyList()
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Device(o.getString("host"), o.optString("name").ifEmpty { o.getString("host") },
                o.optString("kind").ifEmpty { "device" })
        }
    }

    fun castPlay(host: String, track: Track) {
        val meta = JSONObject().put("title", track.title).put("artist", track.artist)
            .put("album", track.album ?: JSONObject.NULL).put("art_url", track.artworkUrl ?: JSONObject.NULL)
            .put("duration_s", track.durationS ?: JSONObject.NULL)
        post("/api/devices/${enc(host)}/play",
            JSONObject().put("service", track.service).put("id", track.id).put("meta", meta))
    }

    fun deviceControl(host: String, action: String, level: Int? = null) {
        val body = JSONObject()
        if (level != null) body.put("level", level)
        post("/api/devices/${enc(host)}/$action", body)
    }

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    private fun parseTracks(arr: org.json.JSONArray?): List<Track> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Track(
                service = o.getString("service"), id = o.getString("id"),
                title = o.optString("title"), artist = o.optString("artist"),
                album = o.optString("album").ifEmpty { null },
                durationS = if (o.isNull("duration_s")) null else o.optInt("duration_s"),
                artworkUrl = o.optString("artwork_url").ifEmpty { null },
            )
        }
    }
}

class ApiError(message: String, val code: Int) : Exception(message)
