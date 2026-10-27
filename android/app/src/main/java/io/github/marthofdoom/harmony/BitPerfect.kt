package io.github.marthofdoom.harmony

import android.media.AudioDeviceInfo
import android.media.AudioFormat

/*
 * The "is this bit-perfect, and what exactly does the output get" model, shared
 * by Now Playing, the mini player and the notification. Everything here is pure
 * (no Context), so the state mapping is unit-tested. The Android side that
 * produces the inputs is PhoneBitPerfect.kt.
 *
 * Rule: bit-perfect is only ever claimed when something verified it — the
 * server said so for a device, or the phone's preferred mixer attributes are
 * active AND the player's real AudioTrack matches them.
 */

enum class BpState { YES, NO, UNKNOWN }

/** What the UI shows: [badge] is the chip text, [detail] the full sentence,
 *  [outputQuality] what the output is fed (null = unknown / same as the source). */
data class OutputIndicator(
    val state: BpState,
    val badge: String,
    val detail: String,
    val outputQuality: Quality? = null,
) {
    companion object {
        const val YES = "Bit-perfect"
        const val NO = "Not bit-perfect"
        const val UNKNOWN = "Unknown"
        fun unknown(detail: String = "Bit-perfect status unknown") = OutputIndicator(BpState.UNKNOWN, UNKNOWN, detail)
    }
}

// ── network devices: render the server's `output` ──────────────────────────

fun deviceIndicator(out: OutputInfo?): OutputIndicator {
    if (out == null) return OutputIndicator.unknown()
    return when (out.bitPerfect) {
        true -> OutputIndicator(BpState.YES, OutputIndicator.YES, out.label ?: "Bit-perfect", out.quality)
        false -> {
            val label = out.label
            val detail = when {
                label != null && label.contains("not bit-perfect", ignoreCase = true) -> label
                label != null && out.reason != null -> "Not bit-perfect — ${out.reason} ($label)"
                else -> "Not bit-perfect" + (out.reason ?: label)?.let { " — $it" }.orEmpty()
            }
            OutputIndicator(BpState.NO, OutputIndicator.NO, detail, out.quality)
        }
        null -> OutputIndicator.unknown(out.label ?: "Bit-perfect status unknown").copy(outputQuality = out.quality)
    }
}

// ── the phone's own output ─────────────────────────────────────────────────

/** Declaration order is the routing priority used by [classifySink]. */
enum class SinkKind { USB, WIRED, BLUETOOTH, BUILTIN }

/** Where the phone's media audio is routed right now. */
data class PhoneSink(
    val kind: SinkKind,
    val name: String? = null,
    /** The Android mixer's output rate (AudioManager PROPERTY_OUTPUT_SAMPLE_RATE). */
    val mixerRateHz: Int? = null,
    /** Bluetooth codec when the platform exposes it (it rarely does). */
    val btCodec: String? = null,
)

/** One PCM format a USB device offers as a bit-perfect mixer. [encoding] is an AudioFormat.ENCODING_*. */
data class MixerFormat(val sampleRate: Int, val encoding: Int, val channelCount: Int)

/** Preferred mixer attributes we set. [active]: the platform reports them as set;
 *  [matchesTrack]: the player's actual AudioTrack uses exactly this format. */
data class MixerApplied(val format: MixerFormat, val active: Boolean, val matchesTrack: Boolean)

data class PhoneOutput(val sink: PhoneSink? = null, val mixer: MixerApplied? = null)

fun encodingDepth(encoding: Int): Int? = when (encoding) {
    AudioFormat.ENCODING_PCM_8BIT -> 8
    AudioFormat.ENCODING_PCM_16BIT -> 16
    AudioFormat.ENCODING_PCM_24BIT_PACKED -> 24
    AudioFormat.ENCODING_PCM_32BIT -> 32
    AudioFormat.ENCODING_PCM_FLOAT -> 32
    else -> null
}

/** Pick the bit-perfect format for a track: same rate and channel count, and the
 *  narrowest encoding that holds the track's bit depth losslessly (an exact int
 *  match first, float last). Null when the DAC offers nothing that fits. */
fun pickMixerFormat(supported: List<MixerFormat>, rateHz: Int?, bitDepth: Int?, channels: Int = 2): MixerFormat? {
    if (rateHz == null || rateHz <= 0) return null
    val need = bitDepth ?: 16
    return supported
        .filter { it.sampleRate == rateHz && it.channelCount == channels }
        .filter { (encodingDepth(it.encoding) ?: 0) >= need }
        .minByOrNull { (encodingDepth(it.encoding) ?: 99) * 10 + if (it.encoding == AudioFormat.ENCODING_PCM_FLOAT) 5 else 0 }
}

/** Route classification from AudioDeviceInfo.TYPE_* values (USB > wired > Bluetooth > built-in). */
fun classifySink(types: List<Int>): SinkKind? {
    fun kind(t: Int): SinkKind = when (t) {
        AudioDeviceInfo.TYPE_USB_DEVICE, AudioDeviceInfo.TYPE_USB_HEADSET,
        AudioDeviceInfo.TYPE_USB_ACCESSORY -> SinkKind.USB
        AudioDeviceInfo.TYPE_WIRED_HEADPHONES, AudioDeviceInfo.TYPE_WIRED_HEADSET,
        AudioDeviceInfo.TYPE_LINE_ANALOG, AudioDeviceInfo.TYPE_LINE_DIGITAL,
        AudioDeviceInfo.TYPE_AUX_LINE -> SinkKind.WIRED
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
        AudioDeviceInfo.TYPE_BLE_HEADSET, AudioDeviceInfo.TYPE_BLE_SPEAKER,
        AudioDeviceInfo.TYPE_BLE_BROADCAST, AudioDeviceInfo.TYPE_HEARING_AID -> SinkKind.BLUETOOTH
        else -> SinkKind.BUILTIN
    }
    return types.map(::kind).minByOrNull { it.ordinal }   // enum order is the priority
}

/** A rate-only [Quality] for "what the mixer outputs". */
fun rateOnlyQuality(rateHz: Int, bitDepth: Int? = null) =
    Quality(null, bitDepth, rateHz, null, true, "lossless", "")

fun phoneIndicator(out: PhoneOutput?, src: Quality?): OutputIndicator {
    val sink = out?.sink ?: return OutputIndicator.unknown()
    val mixer = out.mixer
    if (sink.kind == SinkKind.USB && mixer != null && mixer.active && mixer.matchesTrack) {
        val depth = src?.bitDepth ?: encodingDepth(mixer.format.encoding)
        val rate = mixer.format.sampleRate
        val q = rateOnlyQuality(rate, depth)
        val fmt = listOfNotNull(depth?.toString(), khzText(rate)).joinToString("/")
        return OutputIndicator(BpState.YES, OutputIndicator.YES, "Bit-perfect · USB DAC · $fmt", q)
    }
    if (sink.kind == SinkKind.BLUETOOTH) {
        val codec = sink.btCodec?.let { " ($it)" }.orEmpty()
        return OutputIndicator(BpState.NO, OutputIndicator.NO, "Bluetooth$codec — re-encoded")
    }
    val rate = sink.mixerRateHz?.takeIf { it > 0 }
    val detail = if (rate != null) "Not bit-perfect — Android mixer resamples to $rate Hz"
                 else "Not bit-perfect — Android mixer resamples the audio"
    return OutputIndicator(BpState.NO, OutputIndicator.NO, detail, rate?.let { rateOnlyQuality(it) })
}

/** The indicator for the current playback state; null when there is nothing to describe. */
fun outputIndicator(s: PlayerState, enabled: Boolean = Features.BIT_PERFECT): OutputIndicator? {
    if (!enabled) return null
    if (s.monitoring || s.current == null) return null
    return if (s.onDevice) {
        if (s.stream == null) null else deviceIndicator(s.stream.output)
    } else {
        if (s.stream == null) null
        else phoneIndicator(s.phoneOutput, s.stream.quality ?: s.current?.quality)
    }
}

// ── formatting ─────────────────────────────────────────────────────────────

fun khzText(hz: Int): String {
    val k = Math.round(hz / 100.0) / 10.0
    return if (k % 1.0 == 0.0) k.toInt().toString() else k.toString()
}

private fun Quality.rateOnly() = codec == null && bitDepth == null && sampleRate != null

private fun differs(src: Quality?, out: Quality): Boolean {
    if (src == null) return true
    return (out.codec != null && out.codec != src.codec) ||
        (out.bitDepth != null && out.bitDepth != src.bitDepth) ||
        (out.sampleRate != null && out.sampleRate != src.sampleRate) ||
        (out.bitrate != null && out.bitrate != src.bitrate)
}

/** "→ 24/96" / "→ 48 kHz": the output side of "FLAC 24/192 → 24/96", or null
 *  when the output is unknown or the same as the source. [compact] is the phone form. */
fun outputArrow(src: Quality?, out: Quality?, compact: Boolean): String? {
    if (out == null || !differs(src, out)) return null
    val text = when {
        out.rateOnly() && out.bitDepth == null -> if (compact) "${khzText(out.sampleRate!!)} kHz" else "${out.sampleRate} Hz"
        out.rateOnly() -> "${out.bitDepth}/${khzText(out.sampleRate!!)}"
        compact -> out.short.let { s -> if (src?.codec != null && out.codec == src.codec && out.lossless) s.removePrefix(out.codec ?: "").trim() else s }
        else -> out.plainLabel.let { l ->
            if (src?.codec != null && out.codec == src.codec) l.removePrefix("${out.codec} · ").trim() else l
        }
    }
    return if (text.isBlank()) null else "→ $text"
}

/** Source quality + the output side when it differs: "FLAC · 24-bit / 192 kHz → 24-bit / 96 kHz". */
fun qualityText(src: Quality?, ind: OutputIndicator?, compact: Boolean): String? {
    val base = src?.let { if (compact) it.short else it.plainLabel }?.ifBlank { null }
    val arrow = outputArrow(src, ind?.outputQuality, compact)
    return listOfNotNull(base, arrow).joinToString(" ").ifBlank { null }
}

/** Line 4 on Now Playing: "Library · harmony-server · FLAC · 24-bit / 96 kHz" (no "Playing from").
 *  [compact] drops the instance and uses the short quality (mini player / notification);
 *  [shortQuality] alone keeps the instance but shortens the quality so the "→ output" side fits. */
fun sourceLine(stream: StreamInfo?, trackQuality: Quality?, fallbackSource: String?, ind: OutputIndicator?,
               compact: Boolean = false, shortQuality: Boolean = compact): String? {
    val src = stream?.source
    val sourceLabel = src?.label?.let { if (it.equals("local", true)) "Library" else it }
        ?: src?.service?.let { serviceLabel(it) } ?: fallbackSource
    val instance = if (compact) null else src?.instance?.takeIf { src.service == "local" }
    val q = stream?.quality ?: trackQuality
    return listOfNotNull(sourceLabel, instance, qualityText(q, ind, shortQuality)).joinToString(" · ").ifEmpty { null }
}
