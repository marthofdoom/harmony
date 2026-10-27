package io.github.marthofdoom.harmony

import org.json.JSONObject

/** Audio quality as the server describes it (src/harmony/quality.py), the same
 *  for a library file and a Qobuz/YouTube stream. [tier] is hires | lossless | lossy. */
data class Quality(
    val codec: String?,
    val bitDepth: Int?,
    val sampleRate: Int?,   // Hz
    val bitrate: Int?,      // kbps
    val lossless: Boolean,
    val tier: String,
    val label: String,
) {
    val isHiRes: Boolean get() = tier == "hires"

    /** The label without its trailing "Hi-Res" (shown as a chip instead). */
    val plainLabel: String get() = label.removeSuffix(" · Hi-Res").removeSuffix("Hi-Res").trim()

    /** A very short form for tight rows: "FLAC 24/96", "FLAC 16/44.1", "AAC 256k". */
    val short: String get() {
        val c = codec ?: "Audio"
        return when {
            lossless && (bitDepth != null || sampleRate != null) -> {
                val khz = sampleRate?.let { if (it % 1000 == 0) "${it / 1000}" else (it / 1000.0).toString() }
                "$c " + listOfNotNull(bitDepth?.toString(), khz).joinToString("/")
            }
            bitrate != null -> "$c ${bitrate}k"
            else -> c
        }
    }

    fun toJson(): JSONObject = JSONObject()
        .put("codec", codec ?: JSONObject.NULL).put("bit_depth", bitDepth ?: JSONObject.NULL)
        .put("sample_rate", sampleRate ?: JSONObject.NULL).put("bitrate", bitrate ?: JSONObject.NULL)
        .put("lossless", lossless).put("tier", tier).put("label", label)

    companion object {
        /** Parse the server's quality dict; null when absent or unusable. */
        fun fromJson(o: JSONObject?): Quality? {
            if (o == null) return null
            fun str(k: String): String? = if (o.isNull(k)) null else o.optString(k).ifEmpty { null }
            fun int(k: String): Int? = if (o.isNull(k) || !o.has(k)) null else o.optInt(k)
            val codec = str("codec")
            val depth = int("bit_depth"); val rate = int("sample_rate"); val kbps = int("bitrate")
            val label = str("label")
            if (label == null && codec == null && depth == null && rate == null && kbps == null) return null
            val lossless = o.optBoolean("lossless")
            val tier = str("tier") ?: if (lossless) "lossless" else "lossy"
            return Quality(codec, depth, rate, kbps, lossless, tier, label ?: codec ?: "Audio")
        }
    }
}

/** Where a resolved stream comes from (GET /api/resolve "source"). */
data class SourceInfo(
    val service: String,
    val label: String?,
    val instance: String?,
    val own: Boolean,
    val fromLibrary: Boolean,
) {
    fun toJson(): JSONObject = JSONObject().put("service", service)
        .put("label", label ?: JSONObject.NULL).put("instance", instance ?: JSONObject.NULL)
        .put("own", own).put("from_library", fromLibrary)

    companion object {
        fun fromJson(o: JSONObject?): SourceInfo? {
            if (o == null) return null
            val service = o.optString("service").ifEmpty { return null }
            fun str(k: String): String? = if (o.isNull(k)) null else o.optString(k).ifEmpty { null }
            return SourceInfo(service, str("label"), str("instance"),
                o.optBoolean("own"), o.optBoolean("from_library"))
        }
    }
}

/** What a network output is actually fed, and whether that is bit-perfect
 *  (the server's "output" in GET /api/resolve and the device-queue snapshot).
 *  [bitPerfect] null means the server could not tell. */
data class OutputInfo(
    val quality: Quality?,
    val bitPerfect: Boolean?,
    val reason: String?,
    val label: String?,
) {
    companion object {
        /** Parse the server's output dict; null when absent. */
        fun fromJson(o: JSONObject?): OutputInfo? {
            if (o == null) return null
            fun str(k: String): String? = if (o.isNull(k)) null else o.optString(k).ifEmpty { null }
            return OutputInfo(
                Quality.fromJson(o.optJSONObject("quality")),
                if (o.isNull("bit_perfect") || !o.has("bit_perfect")) null else o.optBoolean("bit_perfect"),
                str("reason"), str("label"))
        }
    }
}

/** The current stream's source + quality (+ the output it ends up as): /api/resolve
 *  on the phone, the queue snapshot's "stream" + "output" on a device. */
data class StreamInfo(val quality: Quality?, val source: SourceInfo?, val output: OutputInfo? = null) {
    companion object {
        /** [o] is the stream/resolve object; [outputFallback] (the snapshot's top-level
         *  "output") is used when the stream object carries none. */
        fun fromJson(o: JSONObject?, outputFallback: JSONObject? = null): StreamInfo? {
            val q = Quality.fromJson(o?.optJSONObject("quality"))
            val s = SourceInfo.fromJson(o?.optJSONObject("source"))
            val out = OutputInfo.fromJson(o?.optJSONObject("output") ?: outputFallback)
            return if (q == null && s == null && out == null) null else StreamInfo(q, s, out)
        }
    }
}

/** What the queue was loaded from — "Playing from <title>". [kind] is one of
 *  album | playlist | artist | genre | songs | search | track. */
data class PlayContext(val kind: String, val title: String, val service: String? = null, val id: String? = null) {
    fun toJson(): JSONObject = JSONObject().put("kind", kind).put("title", title)
        .apply { service?.let { put("service", it) }; id?.let { put("id", it) } }

    companion object {
        /** Canonical search context: title and id both carry the query. */
        fun search(q: String) = PlayContext("search", q, id = q)

        fun fromJson(o: JSONObject?): PlayContext? {
            if (o == null) return null
            val kind = o.optString("kind").ifEmpty { return null }
            val title = o.optString("title").ifEmpty { return null }
            return PlayContext(kind, title,
                if (o.isNull("service")) null else o.optString("service").ifEmpty { null },
                if (o.isNull("id")) null else o.optString("id").ifEmpty { null })
        }
    }
}

/** A search context's query. Older versions stored the label ("Search: q" /
 *  "Search “q”") as the title, so strip that. */
fun PlayContext.searchQuery(): String {
    val raw = (id ?: title).trim()
    Regex("^Search\\s*[“\"](.*)[”\"]$").find(raw)?.let { return it.groupValues[1].trim() }
    Regex("^Search:\\s*(.*)$").find(raw)?.let { return it.groupValues[1].trim() }
    return raw
}
