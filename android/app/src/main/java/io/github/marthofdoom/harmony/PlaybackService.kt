package io.github.marthofdoom.harmony

import android.app.PendingIntent
import android.content.Intent
import android.os.Looper
import androidx.annotation.OptIn
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.common.SimpleBasePlayer
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch

/**
 * The media session's view of playback. It fronts [PlaybackController] rather than
 * the ExoPlayer directly, so the notification / lockscreen / headset buttons work
 * the same whether the phone or a network device is the output, and Next/Previous
 * follow the Harmony queue rules (the ExoPlayer only ever holds one track).
 */
@OptIn(UnstableApi::class)
class HarmonyPlayer(private val c: PlaybackController) : SimpleBasePlayer(Looper.getMainLooper()) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    // Declared before `init`: the collector below runs synchronously (Main.immediate)
    // and getState() reads this — declared after, it is still null (NPE on launch).
    private val commands = Player.Commands.Builder().addAll(
        COMMAND_PLAY_PAUSE, COMMAND_PREPARE, COMMAND_STOP,
        COMMAND_SEEK_TO_DEFAULT_POSITION, COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM,
        COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM, COMMAND_SEEK_TO_PREVIOUS,
        COMMAND_SEEK_TO_NEXT_MEDIA_ITEM, COMMAND_SEEK_TO_NEXT, COMMAND_SEEK_TO_MEDIA_ITEM,
        COMMAND_GET_CURRENT_MEDIA_ITEM, COMMAND_GET_TIMELINE, COMMAND_GET_METADATA,
        COMMAND_SET_REPEAT_MODE, COMMAND_RELEASE,
    ).build()

    init {
        // Re-read state on every meaningful change (not on position ticks — the
        // position is supplied live).
        scope.launch {
            c.state.map { it.copy(positionMs = 0) }.distinctUntilChanged().collect { invalidateState() }
        }
    }

    override fun getState(): State {
        val s = c.state.value
        val b = State.Builder().setAvailableCommands(commands)
        if (s.monitoring) {
            val item = MediaItemData.Builder("hub-audio")
                .setMediaItem(MediaItem.Builder().setMediaId("hub-audio")
                    .setMediaMetadata(MediaMetadata.Builder().setTitle("Hub audio")
                        .setArtist(s.targetName ?: "Harmony").build()).build())
                .setIsSeekable(false).setIsDynamic(true).build()
            return b.setPlaylist(listOf(item)).setCurrentMediaItemIndex(0)
                .setPlaybackState(if (s.buffering) STATE_BUFFERING else STATE_READY)
                .setPlayWhenReady(s.isPlaying, PLAY_WHEN_READY_CHANGE_REASON_USER_REQUEST)
                .setContentPositionMs { c.currentPositionMs() }
                .build()
        }
        if (s.queue.isEmpty() || s.index < 0) {
            return b.setPlaybackState(STATE_IDLE)
                .setPlayWhenReady(false, PLAY_WHEN_READY_CHANGE_REASON_USER_REQUEST).build()
        }
        val playlist = s.queue.mapIndexed { i, t ->
            val durUs = if (i == s.index && s.durationMs > 0) s.durationMs * 1000
                        else t.durationS?.let { it * 1_000_000L } ?: C.TIME_UNSET
            MediaItemData.Builder("$i|${t.service}|${t.id}")
                .setMediaItem(MediaItem.Builder().setMediaId("${t.service}:${t.id}")
                    .setMediaMetadata(c.metadataFor(t)).build())
                .setDurationUs(durUs)
                .setIsSeekable(true)
                .build()
        }
        return b.setPlaylist(playlist)
            .setCurrentMediaItemIndex(s.index)
            // Never ENDED/IDLE while a queue exists: Play always means "play the
            // current track" (the controller restarts an ended queue).
            .setPlaybackState(if (s.buffering) STATE_BUFFERING else STATE_READY)
            .setPlayWhenReady(s.isPlaying, PLAY_WHEN_READY_CHANGE_REASON_USER_REQUEST)
            .setRepeatMode(when (s.repeat) {
                RepeatMode.OFF -> REPEAT_MODE_OFF
                RepeatMode.ALL -> REPEAT_MODE_ALL
                RepeatMode.ONE -> REPEAT_MODE_ONE
            })
            .setShuffleModeEnabled(false)  // the queue is already in play order
            .setContentPositionMs { c.currentPositionMs() }
            .build()
    }

    private fun done(): ListenableFuture<*> = Futures.immediateVoidFuture()

    override fun handleSetPlayWhenReady(playWhenReady: Boolean): ListenableFuture<*> {
        if (playWhenReady) c.play() else c.pause()
        return done()
    }

    override fun handlePrepare(): ListenableFuture<*> = done()

    override fun handleStop(): ListenableFuture<*> { c.stop(); return done() }

    override fun handleRelease(): ListenableFuture<*> = done()

    override fun handleSetRepeatMode(repeatMode: Int): ListenableFuture<*> {
        c.setRepeat(when (repeatMode) {
            REPEAT_MODE_ALL -> RepeatMode.ALL
            REPEAT_MODE_ONE -> RepeatMode.ONE
            else -> RepeatMode.OFF
        })
        return done()
    }

    override fun handleSeek(mediaItemIndex: Int, positionMs: Long, seekCommand: Int): ListenableFuture<*> {
        val cur = c.state.value.index
        when (seekCommand) {
            COMMAND_SEEK_TO_NEXT, COMMAND_SEEK_TO_NEXT_MEDIA_ITEM -> c.next()
            COMMAND_SEEK_TO_PREVIOUS -> c.prev()
            COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM -> c.seekTo(positionMs.coerceAtLeast(0))
            else -> {  // SEEK_TO_PREVIOUS_MEDIA_ITEM / SEEK_TO_MEDIA_ITEM / DEFAULT_POSITION
                if (mediaItemIndex == cur || mediaItemIndex == C.INDEX_UNSET) {
                    c.seekTo(if (positionMs == C.TIME_UNSET) 0 else positionMs.coerceAtLeast(0))
                } else c.jump(mediaItemIndex)
            }
        }
        return done()
    }
}

/**
 * Foreground media service: owns the MediaSession (notification with transport,
 * lockscreen controls, Bluetooth/headset buttons) so playback outlives the
 * Activity. The player itself lives in the process-scoped [PlaybackController].
 */
@OptIn(UnstableApi::class)
class PlaybackService : MediaSessionService() {
    private var session: MediaSession? = null

    override fun onCreate() {
        super.onCreate()
        val controller = PlaybackController.get(this)
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        session = MediaSession.Builder(this, controller.sessionPlayer)
            .setSessionActivity(open)
            .build()
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    override fun onTaskRemoved(rootIntent: Intent?) {
        // Swiped away from recents: keep going if music is playing, else stop.
        val p = session?.player
        if (p == null || !p.playWhenReady || p.mediaItemCount == 0) {
            PlaybackController.get(this).persist()
            stopSelf()
        }
    }

    override fun onDestroy() {
        PlaybackController.get(this).persist()
        session?.release()
        session = null
        super.onDestroy()
    }
}
