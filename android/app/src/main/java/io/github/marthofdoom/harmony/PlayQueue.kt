package io.github.marthofdoom.harmony

/** Active-queue repeat mode: no repeat, loop the whole queue, or repeat one track. */
enum class RepeatMode(val wire: String) {
    OFF("off"), ALL("all"), ONE("one");

    companion object {
        fun fromWire(s: String?): RepeatMode = entries.firstOrNull { it.wire == s } ?: OFF
    }
}

/** One queue slot. A plain class (not a data class) so identity tracks the
 *  current item through reorders even when the same track appears twice —
 *  the same rule as the Python model's `is` checks. */
class QEntry(val track: Track)

/**
 * The phone's local play queue: a Kotlin port of `src/harmony/playqueue.py`, the
 * one queue model every Harmony surface shares. Index-based: [entries] is the
 * whole list (history + current + up next) and [index] points at what's playing.
 *
 * Pure data + rules: every op returns the index to start now, or null to leave
 * playback alone. The owner ([PlaybackController]) decides when to start a track.
 */
class PlayQueue {
    var entries: MutableList<QEntry> = mutableListOf(); private set
    var index: Int = -1; private set
    private var original: MutableList<QEntry> = mutableListOf()  // load order, restored when shuffle goes off
    var shuffle: Boolean = false; private set
    var repeat: RepeatMode = RepeatMode.OFF

    val tracks: List<Track> get() = entries.map { it.track }
    /** Load order (what shuffle-off restores), sent along on a hand-off. */
    val originalTracks: List<Track> get() = original.map { it.track }
    val size: Int get() = entries.size

    fun current(): QEntry? = entries.getOrNull(index)

    /** Index after the current one (null = the queue is done). Repeat-one only
     *  holds on a natural track end — Next still moves on. */
    fun following(manual: Boolean = false): Int? {
        if (entries.isEmpty()) return null
        if (repeat == RepeatMode.ONE && !manual && index >= 0) return index
        if (index + 1 < entries.size) return index + 1
        if (repeat == RepeatMode.ALL) return 0
        return null
    }

    /** Replace the queue. [start] null = "shuffle play" (random first track when
     *  shuffle is on). [keepOrder] takes the list as already arranged. */
    fun load(tracks: List<Track>, start: Int?, shuffle: Boolean? = null, keepOrder: Boolean = false): Int? {
        if (tracks.isEmpty()) return null
        if (shuffle != null) this.shuffle = shuffle
        val list = tracks.map { QEntry(it) }
        original = list.toMutableList()
        if (this.shuffle && !keepOrder) {
            val rest: MutableList<QEntry>
            if (start == null) {
                rest = list.shuffled().toMutableList()
            } else {
                val s = start.coerceIn(0, list.size - 1)
                rest = (list.subList(0, s) + list.subList(s + 1, list.size)).shuffled().toMutableList()
                rest.add(0, list[s])
            }
            entries = rest; index = 0
        } else {
            entries = list.toMutableList(); index = (start ?: 0).coerceIn(0, list.size - 1)
        }
        return index
    }

    /** Restore a persisted/adopted queue verbatim (no reshuffle). */
    fun restore(tracks: List<Track>, index: Int, shuffle: Boolean, repeat: RepeatMode) {
        entries = tracks.map { QEntry(it) }.toMutableList()
        original = entries.toMutableList()
        this.index = if (entries.isEmpty()) -1 else index.coerceIn(0, entries.size - 1)
        this.shuffle = shuffle
        this.repeat = repeat
    }

    fun jump(i: Int): Int? {
        if (i !in entries.indices) return null
        index = i
        return i
    }

    /** Move to the following track (natural end or Next). */
    fun advance(manual: Boolean = false): Int? {
        val nxt = following(manual) ?: return null
        index = nxt
        return nxt
    }

    /** Previous restarts the current track once it's [RESTART_AFTER_MS] in. */
    fun previous(positionMs: Long): Int? {
        if (entries.isEmpty()) return null
        if (positionMs > RESTART_AFTER_MS || index < 0) return index.coerceAtLeast(0)
        if (index > 0) index -= 1
        else if (repeat == RepeatMode.ALL) index = entries.size - 1
        return index
    }

    /** Append; if nothing is playing ([idle]), start the first added track. */
    fun enqueue(tracks: List<Track>, idle: Boolean): Int? {
        if (tracks.isEmpty()) return null
        val first = entries.size
        val add = tracks.map { QEntry(it) }
        entries.addAll(add); original.addAll(add)
        if (idle) { index = first; return first }
        return null
    }

    /** Insert right after the current track; if idle, start them now. */
    fun playNext(tracks: List<Track>, idle: Boolean): Int? {
        if (tracks.isEmpty()) return null
        val at = if (index >= 0) index + 1 else 0
        val add = tracks.map { QEntry(it) }
        entries.addAll(at, add); original.addAll(add)
        if (idle) { index = at; return at }
        return null
    }

    fun move(src: Int, dst: Int) {
        val n = entries.size
        if (src !in 0 until n || dst !in 0 until n || src == dst) return
        val cur = current()
        entries.add(dst, entries.removeAt(src))
        reanchor(cur)
        if (!shuffle) original = entries.toMutableList()  // a manual order IS the order now
    }

    /** Remove one item. Returns (removedCurrent, indexToStart). */
    fun remove(i: Int): Pair<Boolean, Int?> {
        if (i !in entries.indices) return false to null
        val item = entries.removeAt(i)
        original.removeAll { it === item }
        if (i < index) { index -= 1; return false to null }
        if (i > index) return false to null
        // Removed what's playing: whatever slid into this slot plays next.
        if (index < entries.size) return true to index
        index = entries.size - 1
        return true to null
    }

    /** Drop everything but the current track (it keeps playing). */
    fun clear() {
        val cur = current()
        entries = if (cur != null) mutableListOf(cur) else mutableListOf()
        original = entries.toMutableList()
        index = if (cur != null) 0 else -1
    }

    fun reset() {
        entries = mutableListOf(); original = mutableListOf(); index = -1
    }

    fun setShuffle(on: Boolean) {
        if (on == shuffle) return
        shuffle = on
        val cur = current()
        if (on) {
            val head = if (cur != null) entries.subList(0, index + 1).toList() else emptyList()
            val rest = (if (index >= 0) entries.subList(index + 1, entries.size) else entries).toList().shuffled()
            entries = (head + rest).toMutableList()
        } else {
            // Back to load order (plus anything added since), still on the same track.
            val seen = original.toSet()
            entries = (original + entries.filter { it !in seen }).toMutableList()
            reanchor(cur)
        }
    }

    private fun reanchor(cur: QEntry?) {
        if (cur == null) return
        val i = entries.indexOfFirst { it === cur }
        if (i >= 0) index = i
    }

    companion object {
        const val RESTART_AFTER_MS = 3000L
    }
}
