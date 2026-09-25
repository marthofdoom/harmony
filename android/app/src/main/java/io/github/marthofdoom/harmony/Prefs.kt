package io.github.marthofdoom.harmony

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/** Remembers the last-connected instance and personal key so the app reconnects
 *  on launch without rediscovery, plus the play queue so it survives a restart. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("harmony", Context.MODE_PRIVATE)

    var baseUrl: String?
        get() = sp.getString("base_url", null)
        set(v) = sp.edit().putString("base_url", v).apply()

    var key: String?
        get() = sp.getString("key", null)
        set(v) = sp.edit().putString("key", v).apply()

    /** The persisted play state (queue, index, position, modes, output). */
    data class SavedPlayback(
        val queue: List<Track>,
        val index: Int,
        val positionMs: Long,
        val shuffle: Boolean,
        val repeat: RepeatMode,
        val target: String,
        val targetVia: String?,
        val targetName: String?,
    )

    fun savePlayback(p: SavedPlayback) {
        val arr = JSONArray()
        p.queue.forEach { t ->
            arr.put(JSONObject().put("service", t.service).put("id", t.id).put("title", t.title)
                .put("artist", t.artist).put("album", t.album ?: JSONObject.NULL)
                .put("duration_s", t.durationS ?: JSONObject.NULL)
                .put("artwork_url", t.artworkUrl ?: JSONObject.NULL))
        }
        sp.edit()
            .putString("pb_queue", arr.toString())
            .putInt("pb_index", p.index)
            .putLong("pb_pos", p.positionMs)
            .putBoolean("pb_shuffle", p.shuffle)
            .putString("pb_repeat", p.repeat.wire)
            .putString("pb_target", p.target)
            .putString("pb_target_via", p.targetVia)
            .putString("pb_target_name", p.targetName)
            .apply()
    }

    fun savePosition(ms: Long) = sp.edit().putLong("pb_pos", ms).apply()

    fun loadPlayback(): SavedPlayback? = runCatching {
        val raw = sp.getString("pb_queue", null) ?: return null
        val arr = JSONArray(raw)
        val queue = (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Track(
                service = o.optString("service"), id = o.optString("id"),
                title = o.optString("title"), artist = o.optString("artist"),
                album = if (o.isNull("album")) null else o.optString("album"),
                durationS = if (o.isNull("duration_s")) null else o.optInt("duration_s"),
                artworkUrl = if (o.isNull("artwork_url")) null else o.optString("artwork_url"),
            )
        }
        SavedPlayback(
            queue = queue,
            index = sp.getInt("pb_index", -1),
            positionMs = sp.getLong("pb_pos", 0),
            shuffle = sp.getBoolean("pb_shuffle", false),
            repeat = RepeatMode.fromWire(sp.getString("pb_repeat", "off")),
            target = sp.getString("pb_target", "phone") ?: "phone",
            targetVia = sp.getString("pb_target_via", null),
            targetName = sp.getString("pb_target_name", null),
        )
    }.getOrNull()
}
