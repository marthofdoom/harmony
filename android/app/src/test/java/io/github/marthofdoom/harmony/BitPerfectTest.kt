package io.github.marthofdoom.harmony

import android.media.AudioDeviceInfo
import android.media.AudioFormat
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The bit-perfect indicator: server `output` mapping, the phone's USB mixer, formatting. */
class BitPerfectTest {
    private fun q(codec: String?, depth: Int?, rate: Int?, tier: String = "hires", lossless: Boolean = true,
                  label: String = "") = Quality(codec, depth, rate, null, lossless, tier, label)
    private val flac192 = q("FLAC", 24, 192000, label = "FLAC · 24-bit / 192 kHz · Hi-Res")
    private val flac96 = q("FLAC", 24, 96000, label = "FLAC · 24-bit / 96 kHz · Hi-Res")

    // ── server output ───────────────────────────────────────────────────────
    private fun out(json: String) = OutputInfo.fromJson(JSONObject(json))

    @Test fun deviceBitPerfectTrue() {
        val i = deviceIndicator(out("""{"quality":{"codec":"FLAC","bit_depth":24,"sample_rate":96000,"lossless":true,
            "tier":"hires","label":"FLAC · 24-bit / 96 kHz · Hi-Res"},"bit_perfect":true,"reason":null,"label":"Bit-perfect to device"}"""))
        assertEquals(BpState.YES, i.state); assertEquals("Bit-perfect", i.badge)
        assertEquals("Bit-perfect to device", i.detail); assertEquals(96000, i.outputQuality?.sampleRate)
    }

    @Test fun deviceBitPerfectFalseCarriesTheReason() {
        val i = deviceIndicator(out("""{"bit_perfect":false,"reason":"Chromecast caps at 24/96","label":"Transcoded to 24/96 for Cast"}"""))
        assertEquals(BpState.NO, i.state); assertEquals("Not bit-perfect", i.badge)
        assertEquals("Not bit-perfect — Chromecast caps at 24/96 (Transcoded to 24/96 for Cast)", i.detail)
        assertEquals("Not bit-perfect — just because",
            deviceIndicator(out("""{"bit_perfect":false,"reason":"just because"}""")).detail)
    }

    @Test fun deviceNullOrAbsentIsUnknownNeverBitPerfect() {
        assertEquals(BpState.UNKNOWN, deviceIndicator(null).state)
        val i = deviceIndicator(out("""{"bit_perfect":null,"label":"Decoded by device"}"""))
        assertEquals(BpState.UNKNOWN, i.state); assertEquals("Unknown", i.badge)
        assertEquals(BpState.UNKNOWN, deviceIndicator(out("""{"quality":null}""")).state)
        assertNull(OutputInfo.fromJson(null))
    }

    @Test fun snapshotAndResolveCarryOutput() {
        val snap = StreamInfo.fromJson(JSONObject("""{"quality":{"codec":"FLAC","label":"FLAC"}}"""),
            JSONObject("""{"bit_perfect":true}"""))!!
        assertEquals(true, snap.output?.bitPerfect)
        val inline = StreamInfo.fromJson(JSONObject("""{"output":{"bit_perfect":false}}"""))!!
        assertEquals(false, inline.output?.bitPerfect)
        assertNull(StreamInfo.fromJson(JSONObject("{}"), null))
        // output alone (no stream object) still yields a StreamInfo
        assertEquals(true, StreamInfo.fromJson(null, JSONObject("""{"bit_perfect":true}"""))?.output?.bitPerfect)
    }

    // ── phone ───────────────────────────────────────────────────────────────
    private val fmt96 = MixerFormat(96000, AudioFormat.ENCODING_PCM_24BIT_PACKED, 2)
    private val usb = PhoneSink(SinkKind.USB, "My DAC", 48000)

    @Test fun usbWithMatchingActiveMixerIsBitPerfect() {
        val i = phoneIndicator(PhoneOutput(usb, MixerApplied(fmt96, active = true, matchesTrack = true)), flac96)
        assertEquals(BpState.YES, i.state)
        assertEquals("Bit-perfect · USB DAC · 24/96", i.detail)
        assertEquals(96000, i.outputQuality?.sampleRate)
    }

    @Test fun usbButMixerNotActiveOrNotMatchingIsNotBitPerfect() {
        for (m in listOf(MixerApplied(fmt96, active = false, matchesTrack = true),
                         MixerApplied(fmt96, active = true, matchesTrack = false), null)) {
            val i = phoneIndicator(PhoneOutput(usb, m), flac96)
            assertEquals(BpState.NO, i.state)
            assertEquals("Not bit-perfect — Android mixer resamples to 48000 Hz", i.detail)
        }
    }

    @Test fun mixerOnANonUsbSinkNeverClaimsBitPerfect() {
        val i = phoneIndicator(PhoneOutput(PhoneSink(SinkKind.BUILTIN, null, 48000),
            MixerApplied(fmt96, active = true, matchesTrack = true)), flac96)
        assertEquals(BpState.NO, i.state)
    }

    @Test fun builtInSpeakerReportsTheMixerRate() {
        val i = phoneIndicator(PhoneOutput(PhoneSink(SinkKind.BUILTIN, null, 44100)), flac96)
        assertEquals("Not bit-perfect", i.badge)
        assertEquals("Not bit-perfect — Android mixer resamples to 44100 Hz", i.detail)
        assertEquals(44100, i.outputQuality?.sampleRate)
        assertEquals("Not bit-perfect — Android mixer resamples the audio",
            phoneIndicator(PhoneOutput(PhoneSink(SinkKind.WIRED)), flac96).detail)
    }

    @Test fun bluetoothIsReEncoded() {
        assertEquals("Bluetooth — re-encoded",
            phoneIndicator(PhoneOutput(PhoneSink(SinkKind.BLUETOOTH, "Buds", 48000)), flac96).detail)
        assertEquals("Bluetooth (LDAC) — re-encoded",
            phoneIndicator(PhoneOutput(PhoneSink(SinkKind.BLUETOOTH, btCodec = "LDAC")), flac96).detail)
        assertEquals(BpState.NO, phoneIndicator(PhoneOutput(PhoneSink(SinkKind.BLUETOOTH)), null).state)
    }

    @Test fun noRouteYetIsUnknown() {
        assertEquals(BpState.UNKNOWN, phoneIndicator(null, flac96).state)
        assertEquals(BpState.UNKNOWN, phoneIndicator(PhoneOutput(), flac96).state)
    }

    // ── mixer format choice + routing ───────────────────────────────────────
    private val offered = listOf(
        MixerFormat(44100, AudioFormat.ENCODING_PCM_16BIT, 2),
        MixerFormat(96000, AudioFormat.ENCODING_PCM_16BIT, 2),
        MixerFormat(96000, AudioFormat.ENCODING_PCM_FLOAT, 2),
        MixerFormat(96000, AudioFormat.ENCODING_PCM_32BIT, 2),
        MixerFormat(96000, AudioFormat.ENCODING_PCM_24BIT_PACKED, 2),
        MixerFormat(192000, AudioFormat.ENCODING_PCM_32BIT, 2),
        MixerFormat(96000, AudioFormat.ENCODING_PCM_24BIT_PACKED, 6))

    @Test fun picksTheNarrowestLosslessEncodingAtTheTrackRate() {
        assertEquals(fmt96, pickMixerFormat(offered, 96000, 24))
        assertEquals(MixerFormat(96000, AudioFormat.ENCODING_PCM_16BIT, 2), pickMixerFormat(offered, 96000, 16))
        assertEquals(MixerFormat(192000, AudioFormat.ENCODING_PCM_32BIT, 2), pickMixerFormat(offered, 192000, 24))
        assertEquals(MixerFormat(44100, AudioFormat.ENCODING_PCM_16BIT, 2), pickMixerFormat(offered, 44100, null))
    }

    @Test fun noFormatWhenTheRateOrDepthIsNotOffered() {
        assertNull(pickMixerFormat(offered, 88200, 24))      // rate not offered
        assertNull(pickMixerFormat(offered, 44100, 24))      // only 16-bit at 44.1: would truncate
        assertNull(pickMixerFormat(offered, null, 24))
        assertNull(pickMixerFormat(emptyList(), 96000, 24))
    }

    @Test fun routeClassificationPrefersUsbThenWiredThenBluetooth() {
        assertEquals(SinkKind.USB, classifySink(listOf(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, AudioDeviceInfo.TYPE_USB_DEVICE)))
        assertEquals(SinkKind.USB, classifySink(listOf(AudioDeviceInfo.TYPE_USB_HEADSET)))
        assertEquals(SinkKind.WIRED, classifySink(listOf(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, AudioDeviceInfo.TYPE_WIRED_HEADPHONES)))
        assertEquals(SinkKind.BLUETOOTH, classifySink(listOf(AudioDeviceInfo.TYPE_BUILTIN_SPEAKER, AudioDeviceInfo.TYPE_BLE_HEADSET)))
        assertEquals(SinkKind.BUILTIN, classifySink(listOf(AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)))
        assertNull(classifySink(emptyList()))
    }

    // ── player state → indicator, and the text ──────────────────────────────
    private fun track() = Track("local", "1", "T", "A", "Alb", 100, null)
    private fun st(onDevice: Boolean, stream: StreamInfo?, phone: PhoneOutput? = null) =
        PlayerState(queue = listOf(track()), index = 0, target = if (onDevice) "wiim" else PHONE, stream = stream, phoneOutput = phone)

    @Test fun indicatorFollowsTheOutput() {
        val src = StreamInfo(flac96, null, OutputInfo(flac96, true, null, "Bit-perfect to device"))
        assertEquals(BpState.YES, outputIndicator(st(true, src), enabled = true)!!.state)
        // the server's "output" is ignored for the phone: its own route decides
        assertEquals(BpState.NO, outputIndicator(st(false, src, PhoneOutput(PhoneSink(SinkKind.BUILTIN, null, 48000))), enabled = true)!!.state)
        assertNull(outputIndicator(st(false, null), enabled = true))                    // not resolved yet
        assertNull(outputIndicator(st(true, src).copy(monitoring = true), enabled = true))
        assertNull(outputIndicator(PlayerState(), enabled = true))
    }

    // ── feature flag: bit-perfect is on hold ────────────────────────────────
    @Test fun flagIsOffByDefault() { assertEquals(false, Features.BIT_PERFECT) }

    @Test fun flagOffProducesNoBadgeNoArrowNoNotificationBadge() {
        val src = StreamInfo(flac96, null, OutputInfo(flac96, true, null, "Bit-perfect to device"))
        // default (flag off): no indicator for a device or the phone
        assertNull(outputIndicator(st(true, src)))
        assertNull(outputIndicator(st(false, src, PhoneOutput(PhoneSink(SinkKind.USB, "DAC", 96000)))))
        // with no indicator the source line is just source + quality, with no "->" side or badge text
        val line = sourceLine(src, flac96, null, outputIndicator(st(true, src)))!!
        assertTrue(line, !line.contains("→") && !line.contains("bit-perfect", ignoreCase = true))
        assertTrue(line, line.contains("FLAC"))
    }

    @Test fun flagOffNeverTouchesTheMixerApi() {
        // A Context whose every platform lookup throws: a disabled PhoneBitPerfect must not use it.
        val ctx = object : android.content.ContextWrapper(null) {
            override fun getApplicationContext(): android.content.Context = throw AssertionError("touched context")
            override fun getSystemService(name: String): Any = throw AssertionError("touched AudioManager")
        }
        val bp = PhoneBitPerfect(ctx)            // default flag: off
        bp.start(); bp.onTrack(flac96); bp.onAudioTrack(96000, AudioFormat.ENCODING_PCM_24BIT_PACKED, 2); bp.release()
        assertEquals(PhoneOutput(), bp.output.value)
    }

    @Test fun sourceLineHasNoPlayingFromAndNoRepeatedLibrary() {
        val s = StreamInfo(flac96, SourceInfo("local", "Library", "harmony-server", true, false))
        val line = sourceLine(s, null, null, null)!!
        assertEquals("Library · harmony-server · FLAC · 24-bit / 96 kHz", line)
        assertTrue("Playing from" !in line); assertEquals(1, Regex("Library").findAll(line).count())
        assertEquals("Library · FLAC 24/96", sourceLine(s, null, null, null, compact = true))
        assertEquals("Qobuz · FLAC · 24-bit / 96 kHz", sourceLine(
            StreamInfo(flac96, SourceInfo("qobuz", "Qobuz", null, false, false)), null, null, null))
    }

    @Test fun outputQualityShownOnlyWhenItDiffers() {
        val ind = deviceIndicator(OutputInfo(flac96, false, "cast cap", null))
        assertEquals("FLAC · 24-bit / 192 kHz → 24-bit / 96 kHz", qualityText(flac192, ind, compact = false))
        assertEquals("FLAC 24/192 → 24/96", qualityText(flac192, ind, compact = true))
        assertEquals("FLAC · 24-bit / 96 kHz", qualityText(flac96, ind, compact = false))   // same: no arrow
        val mixer = phoneIndicator(PhoneOutput(PhoneSink(SinkKind.BUILTIN, null, 48000)), flac192)
        assertEquals("FLAC 24/192 → 48 kHz", qualityText(flac192, mixer, compact = true))
        assertEquals("FLAC · 24-bit / 192 kHz → 48000 Hz", qualityText(flac192, mixer, compact = false))
        assertEquals("FLAC 24/96", qualityText(flac96, OutputIndicator.unknown(), compact = true))
        assertNull(qualityText(null, null, true))
    }

    @Test fun tierChipTextCoversHiResAndLossless() {
        assertTrue(flac96.isHiRes)
        val cd = q("FLAC", 16, 44100, tier = "lossless")
        assertTrue(!cd.isHiRes && cd.lossless)
        assertEquals("khz", "44.1", khzText(44100)); assertEquals("96", khzText(96000))
    }
}
