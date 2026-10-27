package io.github.marthofdoom.harmony

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.media.AudioMixerAttributes
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.annotation.RequiresApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Bit-perfect output for the phone's own playback.
 *
 * On Android 14+ with a USB audio device routed for media, we ask the platform
 * for a BIT_PERFECT mixer on that device (AudioManager.setPreferredMixerAttributes),
 * in the format of the track about to play (rate, narrowest lossless encoding).
 * The player must then open an AudioTrack in exactly that format for the mixer
 * to stay out of the way — so "Bit-perfect" is only reported once the platform
 * confirms the attributes are set AND the real AudioTrack matches them
 * ([onAudioTrack]). Everything else is reported as not bit-perfect.
 *
 * Software volume must stay off: the player's volume is never touched (volume
 * is the device's), float output is disabled on the ExoPlayer, and there is no
 * offload. The attributes are re-applied per track when the rate changes and
 * cleared when playback stops or the USB device goes away.
 *
 * Main-thread only. Below API 34 this only reports the route (never claims bit-perfect).
 */
class PhoneBitPerfect(ctx: Context) {
    private val am = ctx.applicationContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val handler = Handler(Looper.getMainLooper())
    private val attrs = AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build()

    private val _output = MutableStateFlow(PhoneOutput())
    val output: StateFlow<PhoneOutput> = _output.asStateFlow()

    private var appliedDevice: AudioDeviceInfo? = null
    private var appliedFormat: MixerFormat? = null
    private var trackFormat: MixerFormat? = null   // the player's real AudioTrack, once opened
    private var started = false

    private val callback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>) = refresh()
        override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>) {
            val dev = appliedDevice
            if (dev != null && removedDevices.any { it.id == dev.id }) dropApplied()
            refresh()
        }
    }

    /** Begin tracking the route (idempotent). */
    fun start() {
        if (started) return
        started = true
        am.registerAudioDeviceCallback(callback, handler)
        if (Build.VERSION.SDK_INT >= 34) listen()
        refresh()
    }

    /** A new phone track is about to be prepared: (re)apply the mixer for its format. */
    fun onTrack(q: Quality?) {
        start()
        trackFormat = null
        if (Build.VERSION.SDK_INT >= 34) {
            val usb = usbDevice()
            val rate = q?.sampleRate
            if (usb == null || q == null || !q.lossless || rate == null) clearPlatform()
            else apply34(usb, rate, q.bitDepth)
        }
        refresh()
    }

    /** The player opened an AudioTrack: record its real format so a match can be verified. */
    fun onAudioTrack(sampleRate: Int, encoding: Int, channelCount: Int) {
        trackFormat = MixerFormat(sampleRate, encoding, channelCount)
        refresh()
    }

    /** Playback stopped / ended / the output moved away: give the mixer back. */
    fun release() {
        trackFormat = null
        if (Build.VERSION.SDK_INT >= 34) clearPlatform()
        refresh()
    }

    // ── API 34 mixer attributes ────────────────────────────────────────────

    @RequiresApi(34)
    private fun apply34(usb: AudioDeviceInfo, rateHz: Int, bitDepth: Int?) {
        try {
            val offered = am.getSupportedMixerAttributes(usb)
                .filter { it.mixerBehavior == AudioMixerAttributes.MIXER_BEHAVIOR_BIT_PERFECT }
            val byFormat = offered.associateBy { MixerFormat(it.format.sampleRate, it.format.encoding, it.format.channelCount) }
            val pick = pickMixerFormat(byFormat.keys.toList(), rateHz, bitDepth)
            if (pick == null) { clearPlatform(); return }
            if (pick == appliedFormat && appliedDevice?.id == usb.id) return   // same rate again: keep it
            if (appliedDevice != null) clearPlatform()
            if (am.setPreferredMixerAttributes(attrs, usb, byFormat.getValue(pick))) {
                appliedDevice = usb; appliedFormat = pick
            } else Log.w(TAG, "setPreferredMixerAttributes refused for $pick")
        } catch (e: Exception) {
            Log.w(TAG, "bit-perfect mixer setup failed", e)
            dropApplied()
        }
    }

    @RequiresApi(34)
    private fun clearPlatform() {
        val dev = appliedDevice
        if (dev != null) runCatching { am.clearPreferredMixerAttributes(attrs, dev) }
        dropApplied()
    }

    @RequiresApi(34)
    private fun listen() {
        am.addOnPreferredMixerAttributesChangedListener({ handler.post(it) },
            AudioManager.OnPreferredMixerAttributesChangedListener { _, _, _ -> refresh() })
    }

    private fun dropApplied() { appliedDevice = null; appliedFormat = null }

    // ── route + published state ────────────────────────────────────────────

    private fun routed(): List<AudioDeviceInfo> =
        if (Build.VERSION.SDK_INT >= 33) am.getAudioDevicesForAttributes(attrs)
        else am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).toList()

    private fun usbDevice(): AudioDeviceInfo? =
        routed().firstOrNull { classifySink(listOf(it.type)) == SinkKind.USB }

    private fun refresh() {
        val devs = routed()
        val kind = classifySink(devs.map { it.type })
        val sink = kind?.let {
            PhoneSink(it, devs.firstOrNull { d -> classifySink(listOf(d.type)) == it }?.productName?.toString(),
                am.getProperty(AudioManager.PROPERTY_OUTPUT_SAMPLE_RATE)?.toIntOrNull())
        }
        val fmt = appliedFormat
        var mixer: MixerApplied? = null
        if (fmt != null && Build.VERSION.SDK_INT >= 34) {
            val dev = appliedDevice
            val live = dev != null && sink?.kind == SinkKind.USB && platformHas(dev, fmt)
            mixer = MixerApplied(fmt, live, live && trackFormat == fmt)
        }
        val next = PhoneOutput(sink, mixer)
        if (next != _output.value) _output.value = next
    }

    @RequiresApi(34)
    private fun platformHas(dev: AudioDeviceInfo, fmt: MixerFormat): Boolean = runCatching {
        val cur = am.getPreferredMixerAttributes(attrs, dev) ?: return@runCatching false
        cur.mixerBehavior == AudioMixerAttributes.MIXER_BEHAVIOR_BIT_PERFECT &&
            MixerFormat(cur.format.sampleRate, cur.format.encoding, cur.format.channelCount) == fmt
    }.getOrDefault(false)

    private companion object { const val TAG = "HarmonyBitPerfect" }
}
