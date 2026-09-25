package io.github.marthofdoom.harmony

import android.content.Context
import android.net.Uri
import android.os.SystemClock
import android.util.Log
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

const val PHONE = "phone"

/** Everything the UI and the media session need to render playback. */
data class PlayerState(
    val queue: List<Track> = emptyList(),
    val index: Int = -1,
    val shuffle: Boolean = false,
    val repeat: RepeatMode = RepeatMode.OFF,
    // Output: PHONE or a device host (+ the peer it is reached through, if any).
    val target: String = PHONE,
    val targetVia: String? = null,
    val targetName: String? = null,
    // True while playback is (meant to be) audible on the current output.
    val isPlaying: Boolean = false,
    val buffering: Boolean = false,
    val positionMs: Long = 0,
    val durationMs: Long = 0,
    // Device output only.
    val deviceVolume: Int? = null,
    val devicePaused: Boolean = false,
    // "Play here": the phone is streaming the hub's live audio (not the queue).
    val monitoring: Boolean = false,
) {
    val current: Track? get() = queue.getOrNull(index)
    val onDevice: Boolean get() = target != PHONE
}

/**
 * The one owner of playback, scoped to the PROCESS (not an Activity/ViewModel) so
 * music keeps playing and auto-advancing after the UI goes away. It holds the
 * phone's ExoPlayer + local [PlayQueue], and for a device output mirrors the
 * server-owned device queue (the instance next to the device auto-advances it).
 *
 * All methods must be called on the main thread. [PlaybackService] wraps it in a
 * MediaSession (notification, lockscreen, headset/Bluetooth buttons).
 */
class PlaybackController private constructor(ctx: Context) {
    private val app = ctx.applicationContext
    private val prefs = Prefs(app)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val q = PlayQueue()

    private val _state = MutableStateFlow(PlayerState())
    val state: StateFlow<PlayerState> = _state.asStateFlow()
    private val _messages = MutableSharedFlow<String>(extraBufferCapacity = 8)
    val messages: SharedFlow<String> = _messages.asSharedFlow()

    /** The connected instance (null while disconnected). */
    var api: HarmonyApi? = null
        set(v) { field = v; if (v != null && _state.value.onDevice) ensurePolling() }

    /** Set by the Activity: poll the device fast only while someone is looking
     *  (or the device queue is running, so the notification stays current). */
    var uiVisible = false
        set(v) { field = v; if (v && _state.value.onDevice) ensurePolling() }

    // -- phone ----------------------------------------------------------------
    private var startJob: Job? = null
    private var startAutoplay = false
    private var failures = 0
    private var pendingStartMs = 0L       // where the current track starts on the next Play
    private var phoneEnded = false        // the queue ran out (Play restarts the current track)
    private var halted = false            // stopped / gave up: adding tracks starts them

    // -- device ---------------------------------------------------------------
    private var pollJob: Job? = null
    private var opSeq = 0                 // discards polls that raced a queue op
    private var devicePosMs = 0L
    private var devicePosAt = 0L
    private var deviceDurMs = 0L
    private var deviceRunning = false     // the server's queue is running
    private var deviceHasQueue = false
    private var ownsDeviceQueue = false   // we loaded (or adopted a running) device queue
    private var lastDeviceError: String? = null

    val exo: ExoPlayer = ExoPlayer.Builder(app)
        .setAudioAttributes(
            AudioAttributes.Builder().setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(),
            /* handleAudioFocus = */ true)
        .setHandleAudioBecomingNoisy(true)
        .setWakeMode(C.WAKE_MODE_NETWORK)
        .build().apply {
            addListener(object : Player.Listener {
                override fun onEvents(player: Player, events: Player.Events) = refreshPhone()
                override fun onIsPlayingChanged(isPlaying: Boolean) {
                    if (isPlaying) failures = 0
                }
                override fun onPlaybackStateChanged(playbackState: Int) {
                    if (playbackState == Player.STATE_ENDED && !_state.value.monitoring &&
                        !_state.value.onDevice) onLocalEnded()
                }
                override fun onPlayerError(error: PlaybackException) {
                    Log.w(TAG, "player error", error)
                    if (_state.value.monitoring) {
                        _state.value = _state.value.copy(monitoring = false)
                        say("Hub audio stopped (${error.errorCodeName}).")
                    } else if (!_state.value.onDevice) {
                        onLocalFailure()
                    }
                }
            })
        }

    /** The MediaSession's player (created lazily on the main thread). */
    val sessionPlayer: HarmonyPlayer by lazy { HarmonyPlayer(this) }

    init {
        restore()
        startTicker()
    }

    // ── public controls (output-agnostic) ───────────────────────────────────

    /** Replace the queue with [tracks] and play from [start] (null = shuffle-play
     *  from a random track). [shuffle] null keeps the current shuffle mode. */
    fun playList(tracks: List<Track>, start: Int?, shuffle: Boolean? = null) {
        if (tracks.isEmpty()) return
        val s = _state.value
        if (s.onDevice) {
            val body = JSONObject().put("tracks", apiTracks(tracks))
                .put("start", start ?: JSONObject.NULL)
                .put("shuffle", shuffle ?: q.shuffle)
                .put("repeat", q.repeat.wire)
            deviceOp("load", body, "Couldn't start playback on the device.")
            return
        }
        stopMonitorQuietly()
        val r = q.load(tracks, start, shuffle) ?: return
        startLocal(r)
    }

    fun play() {
        val s = _state.value
        if (s.onDevice) { devicePlay(); return }
        if (s.monitoring) { exo.play(); return }
        if (q.current() == null) return
        when {
            exo.playbackState == Player.STATE_IDLE || exo.currentMediaItem == null ->
                if (startJob?.isActive != true) startLocal(q.index, if (phoneEnded) 0 else pendingStartMs)
            phoneEnded || exo.playbackState == Player.STATE_ENDED -> {
                // The queue ran out: Play restarts from the current index.
                phoneEnded = false
                exo.seekTo(0); exo.play()
            }
            else -> exo.play()
        }
    }

    fun pause() {
        val s = _state.value
        if (s.onDevice) { if (deviceOwnsQueue()) deviceControl("pause"); return }
        if (startJob?.isActive == true && startAutoplay) {
            startJob?.cancel(); startAutoplay = false
            refreshPhone()
        }
        exo.pause()
    }

    fun togglePlayPause() { if (_state.value.isPlaying) pause() else play() }

    /** Stop playback on the current output; the queue is kept. */
    fun stop() {
        val s = _state.value
        if (s.onDevice) { deviceOp("stop", JSONObject(), "Couldn't stop the device."); return }
        if (s.monitoring) { stopMonitor(); return }
        startJob?.cancel(); startAutoplay = false
        pendingStartMs = 0; phoneEnded = false; halted = true
        exo.stop()
        publish { it.copy(positionMs = 0) }
    }

    fun next() {
        if (remote()) { deviceOp("next"); return }
        val r = q.advance(manual = true) ?: run { say("End of the queue."); return }
        startAt(r)
    }

    fun prev() {
        if (remote()) { deviceOp("prev"); return }
        val before = q.index
        val loaded = !_state.value.onDevice &&
            exo.currentMediaItem != null && exo.playbackState != Player.STATE_IDLE
        val pos = if (loaded) exo.currentPosition else pendingStartMs
        val r = q.previous(pos) ?: return
        if (r == before && loaded) {
            // Restart the current track.
            phoneEnded = false
            exo.seekTo(0)
            if (!exo.playWhenReady) exo.play()
        } else startAt(r)
    }

    fun jump(index: Int) {
        if (remote()) { deviceOp("jump", JSONObject().put("index", index)); return }
        if (q.jump(index) != null) startAt(index)
    }

    fun enqueue(tracks: List<Track>) {
        if (tracks.isEmpty()) return
        val msg = if (tracks.size == 1) "Added to queue" else "${tracks.size} tracks added to queue"
        if (remote()) {
            deviceOp("enqueue", JSONObject().put("tracks", apiTracks(tracks)), okMsg = msg); return
        }
        val r = q.enqueue(tracks, idle = localIdle())
        publish(); say(msg)
        if (r != null) startAt(r)
    }

    fun playNext(tracks: List<Track>) {
        if (tracks.isEmpty()) return
        val msg = if (tracks.size == 1) "Playing next" else "${tracks.size} tracks playing next"
        if (remote()) {
            deviceOp("play_next", JSONObject().put("tracks", apiTracks(tracks)), okMsg = msg); return
        }
        val r = q.playNext(tracks, idle = localIdle())
        publish(); say(msg)
        if (r != null) startAt(r)
    }

    fun move(from: Int, to: Int) {
        if (remote()) { deviceOp("move", JSONObject().put("from", from).put("to", to)); return }
        q.move(from, to); publish()
    }

    fun remove(index: Int) {
        if (remote()) { deviceOp("remove", JSONObject().put("index", index)); return }
        val wasPlaying = _state.value.isPlaying
        val (removedCurrent, start) = q.remove(index)
        if (removedCurrent) {
            pendingStartMs = 0; phoneEnded = false
            if (!_state.value.onDevice) {
                startJob?.cancel(); startAutoplay = false
                if (start != null && wasPlaying) { startLocal(start); return }
                exo.stop()
            }
        }
        publish()
    }

    /** Drop everything but the current track. */
    fun clear() {
        if (remote()) { deviceOp("clear"); return }
        q.clear(); publish()
    }

    fun setShuffle(on: Boolean) {
        if (remote()) { deviceOp("shuffle", JSONObject().put("on", on)); return }
        q.setShuffle(on); publish()
    }

    fun setRepeat(mode: RepeatMode) {
        if (remote()) { deviceOp("repeat", JSONObject().put("mode", mode.wire)); return }
        q.repeat = mode; publish()
    }

    fun seekTo(ms: Long) {
        val s = _state.value
        if (s.onDevice) {
            devicePosMs = ms; devicePosAt = SystemClock.elapsedRealtime()
            if (deviceOwnsQueue()) {
                publish { it.copy(positionMs = ms) }
                deviceControl("seek", (ms / 1000).toInt())
            } else {
                pendingStartMs = ms
                publish { it.copy(positionMs = ms) }
            }
            return
        }
        if (exo.currentMediaItem == null || exo.playbackState == Player.STATE_IDLE) {
            pendingStartMs = ms
            publish { it.copy(positionMs = ms) }
        } else {
            phoneEnded = false
            exo.seekTo(ms)
        }
    }

    /** Ops go to the server only once the device holds (or is running) the queue;
     *  until then the local queue is the pending one and Play hands it over. */
    private fun remote() = _state.value.onDevice && deviceOwnsQueue()

    private fun deviceOwnsQueue() = deviceRunning || ownsDeviceQueue

    /** Start index [i] on the current output (phone: resolve + play; device that
     *  hasn't taken the queue yet: hand the whole queue over at [i]). */
    private fun startAt(i: Int, startMs: Long = 0) {
        if (_state.value.onDevice) {
            if (q.jump(i) == null) return
            publish()
            handoffLoad(startMs)
        } else startLocal(i, startMs)
    }

    /** Device volume 0..100 (the phone's own volume is the system volume keys). */
    fun setVolume(level: Int) {
        if (!_state.value.onDevice) return
        publish { it.copy(deviceVolume = level) }
        deviceControl("volume", level.coerceIn(0, 100))
    }

    /** Live position (extrapolated between device polls). */
    fun currentPositionMs(): Long {
        val s = _state.value
        if (s.onDevice) {
            var p = devicePosMs
            if (s.isPlaying && devicePosAt > 0) p += SystemClock.elapsedRealtime() - devicePosAt
            return if (deviceDurMs > 0) p.coerceAtMost(deviceDurMs) else p
        }
        if (exo.currentMediaItem != null && exo.playbackState != Player.STATE_IDLE) return exo.currentPosition
        return pendingStartMs
    }

    // ── output switching (hand-off) ─────────────────────────────────────────

    /** Switch output to [host] (null = this phone). Hands the queue off so exactly
     *  one output plays: phone→device loads the device at the current index and
     *  position; device→phone stops the device and resumes locally; device→device
     *  stops the old one and loads the new one. */
    fun setOutput(host: String?, via: String?, name: String?, resume: Boolean = true) {
        val old = _state.value
        val newTarget = host ?: PHONE
        if (newTarget == old.target && via == old.targetVia) return
        val client = api
        val wasPlaying = resume && old.isPlaying && !old.monitoring
        val posMs = if (old.monitoring) pendingStartMs else currentPositionMs()
        opSeq++
        pollJob?.cancel(); pollJob = null

        // 1) silence the old output
        if (old.onDevice) {
            if (client != null) {
                val oldHost = old.target; val oldVia = old.targetVia
                scope.launch(Dispatchers.IO) {
                    runCatching { client.deviceQueueOp(oldHost, "stop", JSONObject(), oldVia) }
                        .onFailure { Log.w(TAG, "stop old device", it) }
                }
            }
        } else {
            startJob?.cancel(); startAutoplay = false
            if (old.monitoring) { exo.stop(); exo.clearMediaItems() } else exo.stop()
        }
        deviceRunning = false; deviceHasQueue = false; ownsDeviceQueue = false; lastDeviceError = null
        devicePosMs = posMs; devicePosAt = SystemClock.elapsedRealtime()
        deviceDurMs = (q.current()?.track?.durationS ?: 0) * 1000L
        pendingStartMs = posMs; phoneEnded = false
        _state.value = old.copy(target = newTarget, targetVia = via, targetName = name,
            isPlaying = false, buffering = false, monitoring = false, positionMs = posMs,
            deviceVolume = null, devicePaused = false)
        publish()

        // 2) start the new one where the old one was
        if (host == null) {
            if (q.current() != null && wasPlaying) startLocal(q.index, posMs)
        } else {
            if (q.current() != null && wasPlaying && client != null) handoffLoad(posMs)
            ensurePolling()
        }
    }

    // ── hub audio ("Play here") ─────────────────────────────────────────────

    /** Stream the hub's live output on the phone. The queue is kept (paused at
     *  its position) and resumes with Play. */
    fun startMonitor(url: String) {
        val s = _state.value
        if (s.onDevice) setOutput(null, null, null, resume = false)
        if (exo.currentMediaItem != null && exo.playbackState != Player.STATE_IDLE && !s.monitoring) {
            pendingStartMs = exo.currentPosition
        }
        startJob?.cancel(); startAutoplay = false
        exo.setMediaItem(MediaItem.Builder().setUri(url)
            .setMediaMetadata(MediaMetadata.Builder().setTitle("Hub audio").build()).build())
        exo.prepare(); exo.play()
        _state.value = _state.value.copy(monitoring = true)
        refreshPhone()
    }

    fun stopMonitor() {
        if (!_state.value.monitoring) return
        exo.stop(); exo.clearMediaItems()
        _state.value = _state.value.copy(monitoring = false)
        publish { it.copy(positionMs = pendingStartMs) }
    }

    private fun stopMonitorQuietly() {
        if (_state.value.monitoring) {
            _state.value = _state.value.copy(monitoring = false)
            exo.stop(); exo.clearMediaItems()
        }
    }

    /** Called on disconnect: stop talking to the instance but keep the queue. */
    fun onDisconnected() {
        if (!_state.value.onDevice) {
            if (exo.currentMediaItem != null && exo.playbackState != Player.STATE_IDLE) {
                pendingStartMs = exo.currentPosition
            }
            startJob?.cancel(); startAutoplay = false
            stopMonitorQuietly()
            exo.stop()
        }
        pollJob?.cancel(); pollJob = null
        api = null
        publish { it.copy(isPlaying = false, buffering = false, positionMs = pendingStartMs) }
    }

    /** Persist now (Activity stop, etc.). */
    fun persist() = save()

    // ── phone internals ─────────────────────────────────────────────────────

    /** Nothing is playing (the Python model's `idle`): enqueue/play-next start
     *  the added tracks. A paused track is NOT idle. */
    private fun localIdle() = q.current() == null || phoneEnded || halted

    /** Resolve + start the track at [i] of the local queue. */
    private fun startLocal(i: Int, startMs: Long = 0, autoplay: Boolean = true) {
        if (q.jump(i) == null) return
        val track = q.current()!!.track
        stopMonitorQuietly()
        pendingStartMs = startMs; phoneEnded = false; halted = false
        startJob?.cancel()
        val client = api
        if (client == null) {
            exo.stop()
            publish { it.copy(positionMs = startMs, durationMs = (track.durationS ?: 0) * 1000L, isPlaying = false) }
            say("Connect to an instance to play.")
            return
        }
        startAutoplay = autoplay
        startJob = scope.launch {
            val url = withContext(Dispatchers.IO) { runCatching { client.streamUrl(track) } }
            url.onSuccess { u ->
                exo.setMediaItem(
                    MediaItem.Builder().setUri(u).setMediaId("${track.service}:${track.id}")
                        .setMediaMetadata(metadataFor(track)).build(),
                    startMs)
                exo.prepare()
                exo.playWhenReady = autoplay
            }.onFailure {
                Log.w(TAG, "resolve failed for ${track.title}", it)
                onLocalFailure()
            }
        }
        // Stop the previous track right away so the old one doesn't keep playing
        // while the next URL resolves.
        exo.stop()
        publish { it.copy(positionMs = startMs, durationMs = (track.durationS ?: 0) * 1000L) }
        refreshPhone()
        startJob?.invokeOnCompletion { scope.launch { refreshPhone() } }
    }

    private fun onLocalEnded() {
        val before = q.index
        val nxt = q.advance(manual = false)
        if (nxt == null) {
            phoneEnded = true
            refreshPhone()
            return
        }
        if (nxt == before && exo.currentMediaItem != null) {  // repeat-one / 1-item repeat-all
            exo.seekTo(0); exo.play()
        } else startLocal(nxt)
    }

    /** A track failed to resolve or play: skip to the next; halt once every
     *  track in the queue has failed in a row (don't spin). */
    private fun onLocalFailure() {
        failures++
        val title = q.current()?.track?.title
        val nxt = q.following(manual = true)
        if (nxt == null || failures >= q.size) {
            failures = 0; halted = true
            startAutoplay = false
            exo.stop()
            say(if (q.size > 1) "Couldn't play the queue — no track would start." else "Couldn't play that track.")
            refreshPhone()
            return
        }
        say("Couldn't play “${title ?: "track"}” — skipping.")
        startLocal(nxt)
    }

    private fun refreshPhone() {
        val s = _state.value
        if (s.onDevice) return
        val resolving = startJob?.isActive == true
        val st = exo.playbackState
        val pw = exo.playWhenReady
        val playing = (resolving && startAutoplay) ||
            (pw && (st == Player.STATE_READY || st == Player.STATE_BUFFERING))
        val buffering = (resolving && startAutoplay) || (pw && st == Player.STATE_BUFFERING)
        val dur = exo.duration.takeIf { it != C.TIME_UNSET && it > 0 }
            ?: ((q.current()?.track?.durationS ?: 0) * 1000L)
        if (playing != s.isPlaying || buffering != s.buffering || dur != s.durationMs) {
            _state.value = s.copy(isPlaying = playing, buffering = buffering, durationMs = dur)
        }
    }

    // ── device internals ────────────────────────────────────────────────────

    private fun apiTracks(ts: List<Track>) = HarmonyApi.tracksJson(ts)

    /** Run a queue op on the device's owning instance and adopt the snapshot. */
    private fun deviceOp(op: String, body: JSONObject = JSONObject(),
                         failMsg: String = "The device didn't respond. Try again.",
                         okMsg: String? = null) {
        val s = _state.value
        val client = api ?: run { say("Connect to an instance first."); return }
        val host = s.target; val via = s.targetVia
        val seq = ++opSeq
        scope.launch {
            val res = withContext(Dispatchers.IO) { runCatching { client.deviceQueueOp(host, op, body, via) } }
            if (_state.value.target != host) return@launch
            res.onSuccess { snap ->
                if (op == "load") { ownsDeviceQueue = true; pendingStartMs = 0 }
                if (seq == opSeq) adoptSnapshot(snap)
                okMsg?.let { say(it) }
            }.onFailure { say(friendlyError(it, failMsg)) }
            ensurePolling()
        }
    }

    private fun deviceControl(action: String, level: Int? = null) {
        val s = _state.value
        val client = api ?: return
        val host = s.target; val via = s.targetVia
        opSeq++
        if (action == "pause") {
            devicePosMs = currentPositionMs(); devicePosAt = SystemClock.elapsedRealtime()
            publish { it.copy(isPlaying = false, devicePaused = true) }
        } else if (action == "resume") {
            devicePosAt = SystemClock.elapsedRealtime()
            publish { it.copy(isPlaying = true, devicePaused = false) }
        }
        scope.launch {
            withContext(Dispatchers.IO) { runCatching { client.deviceControl(host, action, level, via) } }
                .onFailure { say(friendlyError(it, "The device didn't respond. Try again.")) }
        }
    }

    private fun devicePlay() {
        when {
            deviceRunning -> deviceControl("resume")
            // Our queue finished / was stopped on the device: restart the current index.
            ownsDeviceQueue && deviceHasQueue && q.index >= 0 ->
                deviceOp("jump", JSONObject().put("index", q.index))
            q.current() != null -> handoffLoad(pendingStartMs)
        }
    }

    /** Load the listener's current queue onto the device as-is (keep_order) at the
     *  current index, then seek to where playback was. */
    private fun handoffLoad(posMs: Long) {
        val s = _state.value
        val client = api ?: return
        val host = s.target; val via = s.targetVia
        val body = JSONObject().put("tracks", HarmonyApi.tracksJson(q.tracks))
            .put("start", q.index.coerceAtLeast(0))
            .put("shuffle", q.shuffle).put("repeat", q.repeat.wire)
            .put("keep_order", true)
            .put("original", HarmonyApi.tracksJson(q.originalTracks.ifEmpty { q.tracks }))
        val seq = ++opSeq
        publish { it.copy(isPlaying = true, buffering = true) }
        scope.launch {
            val res = withContext(Dispatchers.IO) { runCatching { client.deviceQueueOp(host, "load", body, via) } }
            if (_state.value.target != host) return@launch
            res.onFailure {
                publish { it.copy(isPlaying = false, buffering = false) }
                say(friendlyError(it, "Couldn't hand playback to the device."))
                return@launch
            }
            ownsDeviceQueue = true
            pendingStartMs = 0
            res.getOrNull()?.let { if (seq == opSeq) adoptSnapshot(it) }
            if (posMs > 2000) {
                // Give the renderer a moment to start the stream before seeking.
                delay(2500)
                if (_state.value.target != host) return@launch
                withContext(Dispatchers.IO) {
                    runCatching { client.deviceControl(host, "seek", (posMs / 1000).toInt(), via) }
                }
                devicePosMs = posMs; devicePosAt = SystemClock.elapsedRealtime()
            }
            ensurePolling()
        }
    }

    private fun adoptSnapshot(snap: DeviceSnapshot) {
        deviceRunning = snap.playing
        deviceHasQueue = snap.tracks.isNotEmpty()
        // A running device queue (ours, or one another client started) is the
        // truth. A stopped leftover queue we didn't load must not replace the
        // listener's queue: ours stays pending until Play hands it over.
        if (snap.playing && snap.tracks.isNotEmpty()) ownsDeviceQueue = true
        val adoptQueue = snap.tracks.isNotEmpty() && ownsDeviceQueue
        if (adoptQueue) {
            q.restore(snap.tracks, snap.index, snap.shuffle, snap.repeat)
        }
        if (!ownsDeviceQueue) {
            // Just the device's volume / idle state; keep our pending queue + position.
            deviceDurMs = (q.current()?.track?.durationS ?: 0) * 1000L
            publish { it.copy(isPlaying = false, buffering = false, devicePaused = false,
                deviceVolume = snap.volume ?: it.deviceVolume, positionMs = pendingStartMs,
                durationMs = deviceDurMs) }
            return
        }
        val paused = snap.state == "paused"
        val playing = if (snap.playing) !paused else snap.state == "playing"
        snap.positionS?.let { devicePosMs = (it * 1000).toLong(); devicePosAt = SystemClock.elapsedRealtime() }
        deviceDurMs = snap.durationS?.takeIf { it > 0 }?.let { (it * 1000).toLong() }
            ?: ((q.current()?.track?.durationS ?: 0) * 1000L)
        if (snap.error != null && snap.error != lastDeviceError) say("Device: ${snap.error}")
        lastDeviceError = snap.error
        publish {
            it.copy(isPlaying = playing, buffering = false, devicePaused = paused,
                deviceVolume = snap.volume ?: it.deviceVolume,
                positionMs = currentPositionMsFor(playing), durationMs = deviceDurMs)
        }
    }

    private fun currentPositionMsFor(playing: Boolean): Long {
        var p = devicePosMs
        if (playing && devicePosAt > 0) p += SystemClock.elapsedRealtime() - devicePosAt
        return if (deviceDurMs > 0) p.coerceAtMost(deviceDurMs) else p
    }

    private fun ensurePolling() {
        if (pollJob?.isActive == true) return
        if (!_state.value.onDevice) return
        pollJob = scope.launch {
            while (isActive) {
                val s = _state.value
                if (!s.onDevice) break
                val client = api ?: break
                val active = uiVisible || deviceRunning
                if (!active) break
                val seq = opSeq
                val host = s.target; val via = s.targetVia
                val res = withContext(Dispatchers.IO) { runCatching { client.deviceQueue(host, via) } }
                res.onSuccess { if (seq == opSeq && _state.value.target == host) adoptSnapshot(it) }
                    .onFailure { Log.d(TAG, "device poll failed", it) }
                delay(POLL_MS)
            }
        }
    }

    // ── state, persistence, ticker ──────────────────────────────────────────

    private fun publish(extra: (PlayerState) -> PlayerState = { it }) {
        val s = _state.value.copy(queue = q.tracks, index = q.index, shuffle = q.shuffle, repeat = q.repeat)
        _state.value = extra(s)
        refreshPhone()
        saveSoon()
    }

    private var saveJob: Job? = null
    private fun saveSoon() {
        saveJob?.cancel()
        saveJob = scope.launch { delay(400); save() }
    }

    private fun save() {
        val s = _state.value
        prefs.savePlayback(Prefs.SavedPlayback(
            queue = q.tracks, index = q.index, positionMs = currentPositionMs(),
            shuffle = q.shuffle, repeat = q.repeat,
            target = s.target, targetVia = s.targetVia, targetName = s.targetName))
    }

    private fun restore() {
        val saved = prefs.loadPlayback() ?: return
        q.restore(saved.queue, saved.index, saved.shuffle, saved.repeat)
        pendingStartMs = saved.positionMs
        devicePosMs = saved.positionMs
        deviceDurMs = (q.current()?.track?.durationS ?: 0) * 1000L
        _state.value = PlayerState(
            queue = q.tracks, index = q.index, shuffle = q.shuffle, repeat = q.repeat,
            target = saved.target, targetVia = saved.targetVia, targetName = saved.targetName,
            positionMs = saved.positionMs, durationMs = deviceDurMs,
        )
    }

    private fun startTicker() {
        scope.launch {
            var n = 0
            while (isActive) {
                delay(500)
                val s = _state.value
                if (s.current == null && !s.monitoring) continue
                val pos = currentPositionMs()
                if (pos != s.positionMs) _state.value = s.copy(positionMs = pos)
                if (s.isPlaying && ++n % 10 == 0) prefs.savePosition(pos)
            }
        }
    }

    private fun say(msg: String) { _messages.tryEmit(msg) }

    fun metadataFor(t: Track): MediaMetadata = MediaMetadata.Builder()
        .setTitle(t.title).setArtist(t.artist).setAlbumTitle(t.album)
        .setArtworkUri(t.artworkUrl?.let { Uri.parse(it) })
        .setIsPlayable(true).setIsBrowsable(false)
        .build()

    companion object {
        private const val TAG = "HarmonyPlayback"
        private const val POLL_MS = 2000L

        @Volatile private var instance: PlaybackController? = null

        fun get(ctx: Context): PlaybackController =
            instance ?: synchronized(this) {
                instance ?: PlaybackController(ctx.applicationContext).also { instance = it }
            }
    }
}

/** Map common network/auth failures to friendly copy; keep the raw message for logs. */
fun friendlyError(t: Throwable, fallback: String): String {
    android.util.Log.w("Harmony", fallback, t)
    val msg = t.message ?: ""
    return when {
        t is java.net.UnknownHostException ->
            "Couldn't reach the server. Check the address, then try again."
        t is java.net.ConnectException ->
            "Couldn't connect. Check the address and key, then try again."
        "401" in msg || "403" in msg ->
            "That key wasn't accepted. Check your personal key and try again."
        t is ApiError && msg.isNotBlank() -> "$fallback ($msg)"
        else -> fallback
    }
}
