package io.github.marthofdoom.harmony

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

enum class ConnState { DISCONNECTED, CONNECTING, CONNECTED }

data class Playback(
    val track: Track? = null,
    val isPlaying: Boolean = false,
    val positionMs: Long = 0,
    val durationMs: Long = 0,
)

data class UiState(
    val conn: ConnState = ConnState.DISCONNECTED,
    val instanceName: String? = null,
    val discovered: List<Instance> = emptyList(),
    val query: String = "",
    val results: List<Track> = emptyList(),
    val searching: Boolean = false,
    val playback: Playback = Playback(),
    val message: String? = null,
)

class HarmonyViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = Prefs(app)
    private val discovery = Discovery(app)
    private var api: HarmonyApi? = null

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    val player: ExoPlayer = ExoPlayer.Builder(app).build().apply {
        addListener(object : Player.Listener {
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                _state.value = _state.value.copy(playback = _state.value.playback.copy(isPlaying = isPlaying))
            }
        })
    }

    init {
        viewModelScope.launch {
            discovery.instances.collect { list ->
                _state.value = _state.value.copy(discovered = list)
            }
        }
        discovery.start()
        // Reconnect to the last instance if we have one saved.
        val saved = prefs.baseUrl
        if (saved != null) connect(saved, prefs.key)
        startProgressTicker()
    }

    fun startDiscovery() = discovery.start()

    fun connect(baseUrl: String, key: String?) {
        _state.value = _state.value.copy(conn = ConnState.CONNECTING, message = null)
        viewModelScope.launch {
            val client = HarmonyApi(baseUrl, key)
            val result = withContext(Dispatchers.IO) {
                runCatching { if (!client.health()) error("No Harmony server at $baseUrl"); client.accounts() }
            }
            result.onSuccess {
                api = client
                prefs.baseUrl = baseUrl; prefs.key = key
                val name = _state.value.discovered.firstOrNull { it.baseUrl == baseUrl }?.name ?: baseUrl
                _state.value = _state.value.copy(conn = ConnState.CONNECTED, instanceName = name)
            }.onFailure {
                _state.value = _state.value.copy(conn = ConnState.DISCONNECTED, message = it.message ?: "Connection failed")
            }
        }
    }

    fun disconnect() {
        api = null
        prefs.baseUrl = null
        player.stop(); player.clearMediaItems()
        _state.value = _state.value.copy(conn = ConnState.DISCONNECTED, instanceName = null,
            results = emptyList(), query = "", playback = Playback())
    }

    fun setQuery(q: String) { _state.value = _state.value.copy(query = q) }

    fun search() {
        val client = api ?: return
        val q = _state.value.query.trim()
        if (q.isEmpty()) return
        _state.value = _state.value.copy(searching = true, message = null)
        viewModelScope.launch {
            val result = withContext(Dispatchers.IO) { runCatching { client.search(q) } }
            result.onSuccess { _state.value = _state.value.copy(results = it, searching = false) }
                .onFailure { _state.value = _state.value.copy(searching = false, message = it.message) }
        }
    }

    fun play(track: Track) {
        val client = api ?: return
        _state.value = _state.value.copy(playback = _state.value.playback.copy(track = track))
        viewModelScope.launch {
            val url = withContext(Dispatchers.IO) { runCatching { client.streamUrl(track) } }
            url.onSuccess {
                player.setMediaItem(MediaItem.fromUri(it))
                player.prepare(); player.play()
            }.onFailure { _state.value = _state.value.copy(message = it.message ?: "Could not play track") }
        }
    }

    fun togglePlayPause() { if (player.isPlaying) player.pause() else player.play() }

    fun seekTo(ms: Long) = player.seekTo(ms)

    fun clearMessage() { _state.value = _state.value.copy(message = null) }

    private fun startProgressTicker() {
        viewModelScope.launch {
            while (true) {
                if (player.playbackState != Player.STATE_IDLE) {
                    val p = _state.value.playback
                    _state.value = _state.value.copy(
                        playback = p.copy(
                            positionMs = player.currentPosition,
                            durationMs = player.duration.coerceAtLeast(0),
                        )
                    )
                }
                delay(500)
            }
        }
    }

    override fun onCleared() {
        discovery.stop()
        player.release()
        super.onCleared()
    }
}
