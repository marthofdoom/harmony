package io.github.marthofdoom.harmony

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** "Playing from" context persistence and the server's quality/source parsing. */
class PlayContextQualityTest {
    private fun t(n: Int) = Track("local", n.toString(), "T$n", "A", null, 100, null)
    private val hires = """{"codec":"FLAC","bit_depth":24,"sample_rate":96000,"bitrate":2304,
        "lossless":true,"tier":"hires","label":"FLAC · 24-bit / 96 kHz · Hi-Res"}"""

    @Test fun contextRoundTripsThroughJson() {
        val c = PlayContext("album", "Northern Lights", "local", "al1")
        assertEquals(c, PlayContext.fromJson(JSONObject(c.toJson().toString())))
        val songs = PlayContext("songs", "Songs")
        val back = PlayContext.fromJson(JSONObject(songs.toJson().toString()))
        assertEquals(songs, back); assertNull(back!!.service)
    }

    @Test fun contextWithoutKindOrTitleIsDropped() {
        assertNull(PlayContext.fromJson(null))
        assertNull(PlayContext.fromJson(JSONObject("""{"title":"x"}""")))
        assertNull(PlayContext.fromJson(JSONObject("""{"kind":"album"}""")))
    }

    @Test fun queueLoadStoresContextAndEnqueueKeepsIt() {
        val q = PlayQueue(); val ctx = PlayContext("songs", "Songs")
        q.load((0 until 3).map(::t), 1, context = ctx)
        assertEquals(ctx, q.context)
        q.enqueue(listOf(t(9)), idle = false); q.playNext(listOf(t(8)), idle = false)
        assertEquals(ctx, q.context)
        val other = PlayContext("album", "X", "local", "a")
        q.load(listOf(t(1)), 0, context = other)
        assertEquals(other, q.context)
        q.load(listOf(t(1)), 0)            // no context given: "playing from" is cleared
        assertNull(q.context)
        q.reset(); assertNull(q.context)
    }

    @Test fun restoreBringsTheContextBack() {
        val q = PlayQueue(); val ctx = PlayContext("playlist", "Mix", "qobuz", "p1")
        q.restore((0 until 3).map(::t), 2, false, RepeatMode.OFF, ctx)
        assertEquals(ctx, q.context)
        assertEquals(2, q.index)
    }

    @Test fun savedTrackKeepsQualityAndGenre() {
        val q = Quality.fromJson(JSONObject(hires))
        val tr = t(1).copy(genre = "Ambient", quality = q)
        val back = Prefs.trackFromJson(JSONObject(Prefs.trackToJson(tr).toString()))
        assertEquals(tr, back)
        assertEquals(t(2), Prefs.trackFromJson(JSONObject(Prefs.trackToJson(t(2)).toString())))
    }

    @Test fun qualityParsesTheServerDict() {
        val q = Quality.fromJson(JSONObject(hires))!!
        assertEquals("FLAC", q.codec); assertEquals(24, q.bitDepth); assertEquals(96000, q.sampleRate)
        assertTrue(q.isHiRes); assertTrue(q.lossless)
        assertEquals("FLAC · 24-bit / 96 kHz", q.plainLabel)
        assertEquals("FLAC 24/96", q.short)
    }

    @Test fun lossyAndCdQualityAreNotHiRes() {
        val cd = Quality.fromJson(JSONObject("""{"codec":"FLAC","bit_depth":16,"sample_rate":44100,
            "bitrate":null,"lossless":true,"tier":"lossless","label":"FLAC · 16-bit / 44.1 kHz"}"""))!!
        assertFalse(cd.isHiRes); assertEquals("FLAC 16/44.1", cd.short)
        assertEquals("FLAC · 16-bit / 44.1 kHz", cd.plainLabel)
        val aac = Quality.fromJson(JSONObject("""{"codec":"AAC","bit_depth":null,"sample_rate":null,
            "bitrate":256,"lossless":false,"tier":"lossy","label":"AAC · 256 kbps"}"""))!!
        assertFalse(aac.isHiRes); assertEquals("AAC 256k", aac.short)
    }

    @Test fun missingQualityIsNull() {
        assertNull(Quality.fromJson(null))
        assertNull(Quality.fromJson(JSONObject("{}")))
    }

    @Test fun streamInfoParsesResolveAndSnapshot() {
        val o = JSONObject("""{"token":"x","quality":$hires,
            "source":{"service":"local","label":"Library","instance":"harmony-server","own":true,"from_library":false}}""")
        val s = StreamInfo.fromJson(o)!!
        assertEquals("harmony-server", s.source!!.instance)
        assertEquals("Library · harmony-server · FLAC · 24-bit / 96 kHz",
            sourceQualityLine(s, null, null))
        assertNull(StreamInfo.fromJson(JSONObject("""{"token":"x"}""")))
    }

    @Test fun lineFallsBackToTheTrackWhenNoStreamInfo() {
        val q = Quality.fromJson(JSONObject(hires))
        assertEquals("Library · FLAC · 24-bit / 96 kHz", sourceQualityLine(null, q, "Library"))
        assertNull(sourceQualityLine(null, null, null))
    }
}

class PlayingFromTest {
    private fun stream(svc: String, label: String?) =
        StreamInfo(null, SourceInfo(svc, label, "harmony-server", true, false))

    @Test fun playingFromNamesTheSourceNotTheCollection() {
        assertEquals("Library", playingFromSource(stream("local", "Library"), "local"))
        assertEquals("Library", playingFromSource(stream("local", "local"), "local"))
        assertEquals("Qobuz", playingFromSource(stream("qobuz", "Qobuz"), "qobuz"))
        assertEquals("YouTube Music", playingFromSource(stream("ytmusic", "YouTube Music"), "ytmusic"))
        assertEquals("Qobuz", playingFromSource(null, "qobuz"))
        assertEquals("Library", playingFromSource(null, "qobuz", hasLibraryCopy = true))
    }

    @Test fun contextLinkNeverSaysPlayingFrom() {
        listOf("album", "playlist", "artist", "genre", "songs", "search", "track").forEach {
            assertTrue(!PlayContext(it, "X").linkLabel().contains("Playing from"))
        }
    }
}
