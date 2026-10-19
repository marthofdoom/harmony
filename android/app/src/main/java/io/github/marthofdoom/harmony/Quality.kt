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

/** The current stream's source + quality: /api/resolve on the phone, the queue
 *  snapshot's "stream" on a device. */
data class StreamInfo(val quality: Quality?, val source: SourceInfo?) {
    companion object {
        fun fromJson(o: JSONObject?): StreamInfo? {
            if (o == null) return null
            val q = Quality.fromJson(o.optJSONObject("quality"))
            val s = SourceInfo.fromJson(o.optJSONObject("source"))
            return if (q == null && s == null) null else StreamInfo(q, s)
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

/** The "Library · harmony-server · FLAC · 24-bit / 96 kHz" line for Now Playing.
 *  [fallbackSource] is used when the stream's source is not known yet. */
fun sourceQualityLine(stream: StreamInfo?, trackQuality: Quality?, fallbackSource: String?): String? {
    val src = stream?.source
    val sourceLabel = src?.label?.let { if (it.equals("local", true)) "Library" else it }
        ?: src?.service?.let { serviceLabel(it) } ?: fallbackSource
    val instance = src?.instance?.takeIf { src.service == "local" }
    val q = stream?.quality ?: trackQuality
    val parts = listOfNotNull(sourceLabel, instance, q?.plainLabel)
    return parts.joinToString(" · ").ifEmpty { null }
}

/** The source named after "Playing from": Library / YouTube Music / Qobuz. */
fun playingFromSource(stream: StreamInfo?, trackService: String?, hasLibraryCopy: Boolean = false): String? {
    val src = stream?.source
    val raw = src?.label ?: src?.service
    if (raw != null) return if (raw.equals("local", true) || raw.equals("library", true)) "Library"
        else if (src?.label != null) src.label else serviceLabel(raw)
    if (hasLibraryCopy) return "Library"
    return trackService?.let { serviceLabel(it) }
}


/** A search context's query. Older versions stored the label ("Search: q" /
 *  "Search “q”") as the title, so strip that. */
fun PlayContext.searchQuery(): String {
    val raw = (id ?: title).trim()
    Regex("^Search\\s*[“\"](.*)[”\"]$").find(raw)?.let { return it.groupValues[1].trim() }
    Regex("^Search:\\s*(.*)$").find(raw)?.let { return it.groupValues[1].trim() }
    return raw
}
