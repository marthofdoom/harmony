package io.github.marthofdoom.harmony

import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

data class Instance(val name: String, val host: String, val port: Int) {
    val baseUrl get() = "http://$host:$port"
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
