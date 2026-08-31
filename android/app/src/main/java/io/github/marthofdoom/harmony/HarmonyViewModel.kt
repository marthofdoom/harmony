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
    // audio routing
    val peers: List<Instance> = emptyList(),
    val playingHere: Boolean = false,
    val routeStatus: String? = null,
    // phone-bridge: relay a hub track to a renderer on the phone's local network
    val renderers: List<UpnpRenderer> = emptyList(),
    val discoveringRenderers: Boolean = false,
    val bridgingTo: String? = null,
)

class HarmonyViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = Prefs(app)
    private val discovery = Discovery(app)
    private val rtp = RtpReceiver()
    private val relay = LocalRelay()
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
            // Hit the API directly so the real failure surfaces (a blocked
            // cleartext call, a refused connection, or a 401 for a wrong key)
            // instead of a generic "not found".
            val result = withContext(Dispatchers.IO) { runCatching { client.accounts() } }
            result.onSuccess {
                api = client
                prefs.baseUrl = baseUrl; prefs.key = key
                val name = _state.value.discovered.firstOrNull { it.baseUrl == baseUrl }?.name ?: baseUrl
                _state.value = _state.value.copy(conn = ConnState.CONNECTED, instanceName = name)
                refreshPeers()
            }.onFailure {
                _state.value = _state.value.copy(conn = ConnState.DISCONNECTED, message = it.message ?: "Connection failed")
            }
        }
    }

    fun disconnect() {
        rtp.stop(); relay.stop()
        api = null
        prefs.baseUrl = null
        player.stop(); player.clearMediaItems()
        _state.value = _state.value.copy(conn = ConnState.DISCONNECTED, instanceName = null,
            results = emptyList(), query = "", playback = Playback(),
            peers = emptyList(), playingHere = false, routeStatus = null,
            renderers = emptyList(), bridgingTo = null)
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

    // -- audio routing ------------------------------------------------------

    fun refreshPeers() {
        val client = api ?: return
        viewModelScope.launch {
            val res = withContext(Dispatchers.IO) { runCatching { client.instances() } }
            res.onSuccess { _state.value = _state.value.copy(peers = it) }
        }
    }

    /** Play the connected hub's live audio on this phone by *pulling* an MP3
     *  stream over HTTP (ExoPlayer buffers it; works over Wi-Fi, VPN, or
     *  cellular — unlike inbound UDP, which a phone rarely receives). */
    fun playHere() {
        val client = api ?: return
        player.setMediaItem(MediaItem.fromUri(client.monitorUrl()))
        player.prepare()
        player.play()
        _state.value = _state.value.copy(playingHere = true,
            playback = Playback(track = null, isPlaying = true),
            routeStatus = "Playing ${_state.value.instanceName ?: "this hub"}'s audio.")
    }

    fun stopPlayHere() {
        player.stop()
        player.clearMediaItems()
        _state.value = _state.value.copy(playingHere = false, routeStatus = "Stopped.")
    }

    /** Route audio between the connected hub and a discovered peer (both hubs). */
    fun route(direction: String, peer: Instance) {
        val client = api ?: return
        _state.value = _state.value.copy(routeStatus = "Setting up…")
        viewModelScope.launch {
            val res = withContext(Dispatchers.IO) {
                runCatching { client.audioRoute(direction, peer.host, peer.port) }
            }
            val verb = if (direction == "send") "Sending this hub → ${peer.name}"
                       else "Playing ${peer.name} on this hub"
            res.onSuccess { _state.value = _state.value.copy(routeStatus = "$verb.") }
                .onFailure { _state.value = _state.value.copy(routeStatus = "Couldn't route: ${it.message}") }
        }
    }

    // -- phone-bridge: cast a hub track to a local-network renderer ---------

    fun discoverRenderers() {
        _state.value = _state.value.copy(discoveringRenderers = true)
        viewModelScope.launch {
            val found = withContext(Dispatchers.IO) {
                val wifi = getApplication<Application>()
                    .getSystemService(android.content.Context.WIFI_SERVICE) as android.net.wifi.WifiManager
                val lock = wifi.createMulticastLock("harmony-ssdp").apply {
                    setReferenceCounted(false); acquire()
                }
                try { Upnp.discover() } finally { runCatching { lock.release() } }
            }
            _state.value = _state.value.copy(renderers = found, discoveringRenderers = false)
        }
    }

    /** Relay the current track through the phone to a local renderer, so it plays
     *  on a device on the phone's LAN even when the hub is VPN-remote. */
    fun bridgeToRenderer(renderer: UpnpRenderer) {
        val client = api ?: return
        val track = _state.value.playback.track
        if (track == null) {
            _state.value = _state.value.copy(routeStatus = "Play a track first, then bridge it.")
            return
        }
        player.pause()
        _state.value = _state.value.copy(routeStatus = "Bridging to ${renderer.name}…")
        viewModelScope.launch {
            val res = withContext(Dispatchers.IO) {
                runCatching {
                    val streamUrl = client.streamUrl(track)
                    val port = relay.start(streamUrl)
                    val ip = localIpTowards(renderer.host)
                    if (ip.isEmpty()) error("no local route to ${renderer.host}")
                    if (!Upnp.setUriAndPlay(renderer, "http://$ip:$port/stream")) {
                        error("the renderer rejected the stream")
                    }
                }
            }
            res.onSuccess {
                _state.value = _state.value.copy(bridgingTo = renderer.name,
                    routeStatus = "Playing on ${renderer.name}.")
            }.onFailure {
                relay.stop()
                _state.value = _state.value.copy(bridgingTo = null,
                    routeStatus = "Bridge failed: ${it.message}")
            }
        }
    }

    fun stopBridge() {
        val target = _state.value.bridgingTo?.let { name -> _state.value.renderers.firstOrNull { it.name == name } }
        viewModelScope.launch {
            withContext(Dispatchers.IO) {
                target?.let { runCatching { Upnp.stop(it) } }
                relay.stop()
            }
            _state.value = _state.value.copy(bridgingTo = null, routeStatus = "Bridge stopped.")
        }
    }

    private fun localIpTowards(host: String): String = try {
        java.net.DatagramSocket().use { s ->
            s.connect(java.net.InetSocketAddress(host, 9))
            s.localAddress.hostAddress ?: ""
        }
    } catch (e: Exception) { "" }

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
        rtp.stop()
        relay.stop()
        discovery.stop()
        player.release()
        super.onCleared()
    }
}
