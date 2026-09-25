package io.github.marthofdoom.harmony

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/** Mirrors tests/test_playqueue.py so the phone follows the same queue rules as
 *  the desktop and the server's device queues. */
class PlayQueueTest {
    private fun t(n: Int) = Track("qobuz", n.toString(), "T$n", "A", null, 100, null)
    private fun ids(q: PlayQueue) = q.tracks.map { it.id }

    @Test fun loadPlaysTheWholeListFromTheClickedTrack() {
        val q = PlayQueue(); val ts = (0 until 5).map(::t)
        assertEquals(2, q.load(ts, 2))
        assertSame(ts[2], q.current()!!.track)
    }

    @Test fun shufflePlayKeepsEveryTrackOnce() {
        val q = PlayQueue(); val ts = (0 until 20).map(::t)
        q.load(ts, null, shuffle = true)
        assertEquals(0, q.index)
        assertEquals(ts.map { it.id }.sorted(), ids(q).sorted())
    }

    @Test fun shuffleKeepsTheClickedTrackFirst() {
        val q = PlayQueue(); val ts = (0 until 10).map(::t)
        q.load(ts, 7, shuffle = true)
        assertSame(ts[7], q.current()!!.track)
    }

    @Test fun keepOrderLoadsAsArranged() {
        val q = PlayQueue(); val ts = (0 until 10).map(::t)
        q.load(ts, 4, shuffle = true, keepOrder = true)
        assertEquals(ts.map { it.id }, ids(q)); assertEquals(4, q.index); assertTrue(q.shuffle)
    }

    @Test fun repeatOneHoldsOnNaturalEndButNextMovesOn() {
        val q = PlayQueue(); q.load(listOf(t(0), t(1)), 0)
        q.repeat = RepeatMode.ONE
        assertEquals(0, q.following())
        assertEquals(1, q.advance(manual = true))
    }

    @Test fun repeatAllWrapsAndOffEnds() {
        val q = PlayQueue(); q.load(listOf(t(0), t(1)), 1)
        assertNull(q.following())
        q.repeat = RepeatMode.ALL
        assertEquals(0, q.following())
    }

    @Test fun previousRestartsAfterThreeSeconds() {
        val q = PlayQueue(); q.load(listOf(t(0), t(1)), 1)
        assertEquals(1, q.previous(30_000))
        assertEquals(0, q.previous(1_000))
    }

    @Test fun enqueueWhilePlayingAppendsAndDoesntInterrupt() {
        val q = PlayQueue(); q.load(listOf(t(0)), 0)
        assertNull(q.enqueue(listOf(t(1), t(2)), idle = false))
        assertEquals(0, q.index); assertEquals(listOf("0", "1", "2"), ids(q))
    }

    @Test fun enqueueWhenIdleStartsTheAddedTracks() {
        assertEquals(0, PlayQueue().enqueue(listOf(t(1)), idle = true))
    }

    @Test fun playNextInsertsAfterCurrent() {
        val q = PlayQueue(); q.load(listOf(t(0), t(1)), 0)
        q.playNext(listOf(t(9)), idle = false)
        assertEquals(listOf("0", "9", "1"), ids(q))
    }

    @Test fun moveKeepsTheCurrentTrackPlaying() {
        val q = PlayQueue(); q.load(listOf(t(0), t(1), t(2)), 1)
        q.move(2, 0)
        assertEquals("1", q.current()!!.track.id); assertEquals(2, q.index)
    }

    @Test fun removeCurrentHandsOffToTheNextTrack() {
        val q = PlayQueue(); q.load(listOf(t(0), t(1), t(2)), 1)
        val (removedCurrent, start) = q.remove(1)
        assertTrue(removedCurrent); assertEquals(1, start); assertEquals("2", q.current()!!.track.id)
    }

    @Test fun clearKeepsOnlyWhatsPlaying() {
        val q = PlayQueue(); q.load(listOf(t(0), t(1), t(2)), 1)
        q.clear()
        assertEquals(listOf("1"), ids(q)); assertEquals(0, q.index)
    }

    @Test fun shuffleOffRestoresLoadOrderOnTheSameTrack() {
        val q = PlayQueue(); val ts = (0 until 8).map(::t)
        q.load(ts, 3)
        q.setShuffle(true); q.setShuffle(false)
        assertEquals(ts.map { it.id }, ids(q)); assertSame(ts[3], q.current()!!.track)
    }

    // The old Android bug: shuffle-off restored a stale snapshot, undoing edits.
    @Test fun shuffleOffKeepsRemovalsAndEarlierReorders() {
        val q = PlayQueue(); val ts = (0 until 6).map(::t)
        q.load(ts, 0)
        q.move(5, 1)                      // manual order: 0 5 1 2 3 4
        q.setShuffle(true)
        val victim = q.tracks.indexOfFirst { it.id == "3" }
        q.remove(victim)
        q.setShuffle(false)
        assertEquals(listOf("0", "5", "1", "2", "4"), ids(q))
        assertEquals("0", q.current()!!.track.id)
    }

    @Test fun duplicatesTrackByIdentity() {
        val q = PlayQueue(); val a = t(1)
        q.load(listOf(a, t(2), a), 2)
        q.move(0, 1)                      // the first copy moves; current stays the last copy
        assertEquals(2, q.index)
    }
}
