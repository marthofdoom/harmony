package io.github.marthofdoom.harmony

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

enum class ConnState { DISCONNECTED, CONNECTING, CONNECTED }

enum class DetailKind { ARTIST, ALBUM, TRACK }

/** Active-queue repeat mode: no repeat, loop the whole queue, or repeat one track. */
enum class RepeatMode { OFF, ALL, ONE }

/** One entry on the entity-navigation back stack. It carries its own loaded
 *  payload so going back never refetches. `key` disambiguates duplicate routes. */
data class DetailEntry(
    val key: Long,
    val kind: DetailKind,
    val service: String,
    val id: String,
    val loading: Boolean = true,
    val error: String? = null,
    val artist: ArtistDetail? = null,
    val album: AlbumDetail? = null,
    val track: TrackDetail? = null,
)

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
    // which bottom tab is showing (VM-held so navigation can switch it)
    val tab: Int = 0,
    // smart search (spec-ordered sections; fires on submit only)
    val searchService: String = "both",   // both | ytmusic | qobuz
    val smart: SmartSearch? = null,
    val smartSearching: Boolean = false,
    // entity-navigation back stack (overlays the tabs when non-empty)
    val detailStack: List<DetailEntry> = emptyList(),
    val playback: Playback = Playback(),
    // The ephemeral ACTIVE PLAY QUEUE (ordered tracks) the player advances through,
    // separate from any browsed list. Shown on Now Playing with the current track
    // highlighted/tappable; mutated only by play / enqueue / play-next / reorder /
    // auto-advance — never as a side effect of navigation.
    val queue: List<Track> = emptyList(),
    // Index of the currently-playing track within [queue] (-1 when nothing is queued).
    val activeIndex: Int = -1,
    val shuffle: Boolean = false,
    val repeatMode: RepeatMode = RepeatMode.OFF,
    val message: String? = null,
    // audio routing
    val peers: List<Instance> = emptyList(),
    val playingHere: Boolean = false,
    val routeStatus: String? = null,
    // phone-bridge: relay a hub track to a renderer on the phone's local network
    val renderers: List<UpnpRenderer> = emptyList(),
    val discoveringRenderers: Boolean = false,
    val bridgingTo: String? = null,
    // library
    val playlists: List<Playlist> = emptyList(),
    val openPlaylist: Playlist? = null,
    val playlistTracks: List<Track> = emptyList(),
    val libraryLoading: Boolean = false,
    // the track just removed from the open playlist, offered as an undo
    val undoableRemove: Track? = null,
    // cast target: "phone" (this device) or a hub device's host
    val devices: List<Device> = emptyList(),
    val target: String = "phone",
    val devicePaused: Boolean = false,
    // sync
    val syncPlan: SyncPlan? = null,
    val syncBusy: Boolean = false,
    val syncMsg: String? = null,
    // account credential adopt ("Sync accounts" — pull logins from a peer)
    val accountSyncBusy: Boolean = false,
    val accountSyncMsg: String? = null,
)

class HarmonyViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = Prefs(app)
    private val discovery = Discovery(app)
    private val rtp = RtpReceiver()
    private val relay = LocalRelay()
    private var api: HarmonyApi? = null
    private var detailKeySeq = 0L

    // Advertise this phone on the mesh so the desktop/server see it as an
    // instance (e.g. "harmony-<phone>") instead of an invisible client.
    private val instanceName = "harmony-${android.os.Build.MODEL.replace(' ', '-')}"
    private val instanceServer = InstanceServer(instanceName, appVersion(app))

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    // The job resolving/starting the current track; cancelled when we jump elsewhere
    // so a slow stream-URL resolve can't stomp a newer selection.
    private var advanceJob: Job? = null
    // Snapshot of the pre-shuffle order, so toggling shuffle off restores it.
    private var preShuffleOrder: List<Track>? = null

    val player: ExoPlayer = ExoPlayer.Builder(app).build().apply {
        addListener(object : Player.Listener {
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                _state.value = _state.value.copy(playback = _state.value.playback.copy(isPlaying = isPlaying))
            }
            // Auto-advance: when the current track finishes, move through the active
            // queue (respecting repeat/shuffle). Only for local "play here" playback —
            // hub-audio streaming and casting are handled elsewhere.
            override fun onPlaybackStateChanged(playbackState: Int) {
                if (playbackState == Player.STATE_ENDED) onTrackEnded()
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
        // Stand up the phone's mesh presence, then advertise the port it bound.
        runCatching { discovery.advertise(instanceServer.start(), instanceName) }
        // Reconnect to the last instance if we have one saved.
        val saved = prefs.baseUrl
        if (saved != null) connect(saved, prefs.key)
        startProgressTicker()
    }

    private fun appVersion(app: Application): String =
        runCatching { app.packageManager.getPackageInfo(app.packageName, 0).versionName ?: "0" }
            .getOrDefault("0")

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
                refreshPeers(); loadLibrary(); loadDevices()
            }.onFailure {
                _state.value = _state.value.copy(conn = ConnState.DISCONNECTED,
                    message = friendly(it, "Couldn't connect. Check the address and key, then try again."))
            }
        }
    }

    fun disconnect() {
        rtp.stop(); relay.stop()
        api = null
        prefs.baseUrl = null
        player.stop(); player.clearMediaItems()
        advanceJob?.cancel(); preShuffleOrder = null
        _state.value = _state.value.copy(conn = ConnState.DISCONNECTED, instanceName = null,
            results = emptyList(), query = "", playback = Playback(),
            queue = emptyList(), activeIndex = -1,
            smart = null, detailStack = emptyList(), tab = 0,
            peers = emptyList(), playingHere = false, routeStatus = null,
            renderers = emptyList(), bridgingTo = null,
            playlists = emptyList(), openPlaylist = null, playlistTracks = emptyList(),
            devices = emptyList(), target = "phone", syncPlan = null, syncMsg = null,
            accountSyncBusy = false, accountSyncMsg = null)
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
                .onFailure { _state.value = _state.value.copy(searching = false,
                    message = friendly(it, "Couldn't search right now. Try again.")) }
        }
    }

    // -- smart search + entity navigation -----------------------------------

    fun setTab(i: Int) { _state.value = _state.value.copy(tab = i) }

    fun setSearchService(service: String) {
        _state.value = _state.value.copy(searchService = service)
    }

    /** Spec-ordered search; fires on submit only (never per keystroke). */
    fun smartSearch() {
        val client = api ?: return
        val q = _state.value.query.trim()
        if (q.isEmpty()) return
        val service = _state.value.searchService
        _state.value = _state.value.copy(smartSearching = true, message = null)
        viewModelScope.launch {
            val res = withContext(Dispatchers.IO) { runCatching { client.smartSearch(q, service) } }
            res.onSuccess { _state.value = _state.value.copy(smart = it, smartSearching = false) }
                .onFailure { _state.value = _state.value.copy(smartSearching = false,
                    message = friendly(it, "Couldn't search right now. Try again.")) }
        }
    }

    /** Tapping a member/band name runs a smart search for it. Clears any open
     *  detail and returns to the Search tab so the results are visible. */
    fun searchName(name: String) {
        _state.value = _state.value.copy(query = name, tab = 0, detailStack = emptyList())
        smartSearch()
    }

    fun openArtist(service: String, id: String) = pushDetail(DetailKind.ARTIST, service, id)
    fun openArtist(ref: ArtistRef) = openArtist(ref.service, ref.id)
    fun openAlbum(service: String, id: String) = pushDetail(DetailKind.ALBUM, service, id)
    fun openAlbum(ref: AlbumRef) = openAlbum(ref.service, ref.id)
    fun openTrack(service: String, id: String) = pushDetail(DetailKind.TRACK, service, id)

    /** Pop one detail screen; backs the system Back button on a detail. */
    fun popDetail() {
        val stack = _state.value.detailStack
        if (stack.isNotEmpty()) _state.value = _state.value.copy(detailStack = stack.dropLast(1))
    }

    private fun pushDetail(kind: DetailKind, service: String, id: String) {
        if (api == null) return
        val entry = DetailEntry(key = detailKeySeq++, kind = kind, service = service, id = id)
        _state.value = _state.value.copy(detailStack = _state.value.detailStack + entry)
        loadDetail(entry)
    }

    private fun loadDetail(entry: DetailEntry) {
        val client = api ?: return
        viewModelScope.launch {
            val res = withContext(Dispatchers.IO) {
                runCatching {
                    when (entry.kind) {
                        DetailKind.ARTIST -> client.artist(entry.service, entry.id)
                        DetailKind.ALBUM -> client.album(entry.service, entry.id)
                        DetailKind.TRACK -> client.track(entry.service, entry.id)
                    }
                }
            }
            val updated = res.fold(
                onSuccess = { data ->
                    when (data) {
                        is ArtistDetail -> entry.copy(loading = false, artist = data)
                        is AlbumDetail -> entry.copy(loading = false, album = data)
                        is TrackDetail -> entry.copy(loading = false, track = data)
                        else -> entry.copy(loading = false)
                    }
                },
                onFailure = { entry.copy(loading = false,
                    error = friendly(it, "Couldn't load that. Try again.")) },
            )
            // Replace by key (the stack may have changed while loading).
            _state.value = _state.value.copy(
                detailStack = _state.value.detailStack.map { if (it.key == entry.key) updated else it })
        }
    }

    /** Play [track], loading [queue] as the ACTIVE PLAY QUEUE (a copy of the album,
     *  playlist or search list it came from). This REPLACES the active queue and jumps
     *  to [track]; the player then auto-advances through it. Defaults to a 1-item queue
     *  when there's no surrounding list. Honours the current shuffle state. */
    fun play(track: Track, queue: List<Track> = listOf(track)) {
        if (api == null) return
        val idx = queue.indexOfFirst { it.id == track.id && it.service == track.service }
            .coerceAtLeast(0)
        preShuffleOrder = null
        if (_state.value.shuffle && queue.size > 1) {
            val cur = queue[idx]
            preShuffleOrder = queue
            val shuffled = listOf(cur) + queue.filterIndexed { i, _ -> i != idx }.shuffled()
            _state.value = _state.value.copy(queue = shuffled)
            playIndex(0)
        } else {
            _state.value = _state.value.copy(queue = queue)
            playIndex(idx)
        }
    }

    /** Start the track at [index] in the active queue — the single funnel for
     *  play / jump / prev / next / auto-advance. Resolves the per-track stream URL
     *  off the main thread (the same resolve the old play() used), or casts it when a
     *  hub device is the target. */
    private fun playIndex(index: Int) {
        val q = _state.value.queue
        if (index !in q.indices) return
        val client = api ?: return
        val track = q[index]
        _state.value = _state.value.copy(activeIndex = index, playingHere = false,
            playback = _state.value.playback.copy(track = track))
        val target = _state.value.target
        advanceJob?.cancel()
        advanceJob = viewModelScope.launch {
            if (target != "phone") {  // cast to a hub device instead of playing here
                _state.value = _state.value.copy(devicePaused = false)
                withContext(Dispatchers.IO) { runCatching { client.castPlay(target, track) } }
                    .onFailure { _state.value = _state.value.copy(
                        message = friendly(it, "Couldn't cast to the device. Try again.")) }
                return@launch
            }
            val url = withContext(Dispatchers.IO) { runCatching { client.streamUrl(track) } }
            url.onSuccess {
                player.setMediaItem(MediaItem.fromUri(it))
                player.prepare(); player.play()
            }.onFailure { _state.value = _state.value.copy(
                message = friendly(it, "Couldn't play that track. Try again.")) }
        }
    }

    /** A track finished on the local player: advance through the active queue. */
    private fun onTrackEnded() {
        if (_state.value.playingHere || _state.value.target != "phone") return
        val q = _state.value.queue
        if (q.isEmpty()) return
        val i = _state.value.activeIndex
        when (_state.value.repeatMode) {
            RepeatMode.ONE -> playIndex(i)
            else -> {
                val next = i + 1
                when {
                    next < q.size -> playIndex(next)
                    _state.value.repeatMode == RepeatMode.ALL -> playIndex(0)
                    else -> { /* end of queue: stop, keep the last track shown */ }
                }
            }
        }
    }

    /** Skip to the next track in the active queue (wraps only when repeat=all). */
    fun next() {
        val q = _state.value.queue
        if (q.isEmpty()) return
        val i = _state.value.activeIndex
        val target = when {
            i + 1 < q.size -> i + 1
            _state.value.repeatMode == RepeatMode.ALL -> 0
            else -> return
        }
        playIndex(target)
    }

    /** Go to the previous track; restarts the current one if we're already past 3s. */
    fun prev() {
        val q = _state.value.queue
        if (q.isEmpty()) return
        if (_state.value.target == "phone" && player.currentPosition > 3000) {
            player.seekTo(0); return
        }
        val i = _state.value.activeIndex
        val target = when {
            i - 1 >= 0 -> i - 1
            _state.value.repeatMode == RepeatMode.ALL -> q.size - 1
            else -> { if (_state.value.target == "phone") player.seekTo(0); return }
        }
        playIndex(target)
    }

    /** Tap a queue row to jump straight to it. */
    fun jumpTo(index: Int) = playIndex(index)

    /** Append [tracks] to the end of the active queue (does not interrupt playback).
     *  If nothing is queued yet, starts playing from the first added track. */
    fun addToQueue(tracks: List<Track>) {
        if (tracks.isEmpty()) return
        val wasEmpty = _state.value.queue.isEmpty()
        _state.value = _state.value.copy(queue = _state.value.queue + tracks,
            message = if (tracks.size == 1) "Added to queue" else "${tracks.size} tracks added to queue")
        preShuffleOrder = preShuffleOrder?.plus(tracks)
        if (wasEmpty) playIndex(0)
    }

    fun addToQueue(track: Track) = addToQueue(listOf(track))

    /** Insert [tracks] immediately after the current track in the active queue. */
    fun playNext(tracks: List<Track>) {
        if (tracks.isEmpty()) return
        val q = _state.value.queue
        if (q.isEmpty()) { addToQueue(tracks); return }
        val at = (_state.value.activeIndex + 1).coerceIn(0, q.size)
        val newQ = q.toMutableList().apply { addAll(at, tracks) }
        _state.value = _state.value.copy(queue = newQ, message = "Playing next")
        preShuffleOrder = preShuffleOrder?.plus(tracks)
    }

    fun playNext(track: Track) = playNext(listOf(track))

    /** Move an active-queue item, keeping the currently-playing track current. */
    fun moveQueueItem(from: Int, to: Int) {
        val q = _state.value.queue
        if (from !in q.indices || to !in q.indices || from == to) return
        val cur = q.getOrNull(_state.value.activeIndex)
        val m = q.toMutableList()
        m.add(to, m.removeAt(from))
        val ni = if (cur != null) m.indexOfFirst { it === cur }.coerceAtLeast(0)
                 else _state.value.activeIndex
        _state.value = _state.value.copy(queue = m, activeIndex = ni)
    }

    /** Remove an upcoming track from the active queue (the current track is kept). */
    fun removeFromQueue(index: Int) {
        val q = _state.value.queue
        val cur = q.getOrNull(_state.value.activeIndex) ?: return
        if (index !in q.indices || index == _state.value.activeIndex) return
        val m = q.toMutableList().apply { removeAt(index) }
        _state.value = _state.value.copy(queue = m,
            activeIndex = m.indexOfFirst { it === cur }.coerceAtLeast(0))
    }

    /** Toggle shuffle. On: keep the current track first, shuffle the rest. Off:
     *  restore the pre-shuffle order, staying on the current track. Does not restart
     *  playback — only the upcoming order changes. */
    fun toggleShuffle() {
        val on = !_state.value.shuffle
        val q = _state.value.queue
        val i = _state.value.activeIndex
        if (q.isEmpty() || i !in q.indices) {
            _state.value = _state.value.copy(shuffle = on); return
        }
        val cur = q[i]
        if (on) {
            preShuffleOrder = q
            val shuffled = listOf(cur) + q.filterIndexed { idx, _ -> idx != i }.shuffled()
            _state.value = _state.value.copy(shuffle = true, queue = shuffled, activeIndex = 0)
        } else {
            val restore = preShuffleOrder ?: q
            val ni = restore.indexOfFirst { it === cur }
                .let { if (it < 0) restore.indexOfFirst { t -> t.id == cur.id && t.service == cur.service } else it }
                .coerceAtLeast(0)
            preShuffleOrder = null
            _state.value = _state.value.copy(shuffle = false, queue = restore, activeIndex = ni)
        }
    }

    /** Cycle repeat: off → all → one → off. */
    fun cycleRepeat() {
        val next = when (_state.value.repeatMode) {
            RepeatMode.OFF -> RepeatMode.ALL
            RepeatMode.ALL -> RepeatMode.ONE
            RepeatMode.ONE -> RepeatMode.OFF
        }
        _state.value = _state.value.copy(repeatMode = next)
    }

    fun setTarget(target: String) { _state.value = _state.value.copy(target = target) }

    // -- library / playlists ------------------------------------------------

    fun loadLibrary() {
        val client = api ?: return
        _state.value = _state.value.copy(libraryLoading = true)
        viewModelScope.launch {
            val res = withContext(Dispatchers.IO) { runCatching { client.playlists() } }
            res.onSuccess { _state.value = _state.value.copy(playlists = it, libraryLoading = false) }
                .onFailure { _state.value = _state.value.copy(libraryLoading = false,
                    message = friendly(it, "Couldn't load your library. Try again.")) }
        }
    }

    fun openPlaylist(pl: Playlist) {
        val client = api ?: return
        _state.value = _state.value.copy(openPlaylist = pl, playlistTracks = emptyList(), libraryLoading = true)
        viewModelScope.launch {
            val res = withContext(Dispatchers.IO) { runCatching { client.playlistTracks(pl.service, pl.id) } }
            res.onSuccess { _state.value = _state.value.copy(playlistTracks = it, libraryLoading = false) }
                .onFailure { _state.value = _state.value.copy(libraryLoading = false,
                    message = friendly(it, "Couldn't open that playlist. Try again.")) }
        }
    }

    fun closePlaylist() {
        _state.value = _state.value.copy(openPlaylist = null, playlistTracks = emptyList())
    }

    fun createPlaylist(service: String, title: String) =
        mutate("Playlist created", "Couldn't create the playlist. Try again.") {
            it.createPlaylist(service, title)
        }

    fun renamePlaylist(pl: Playlist, title: String) =
        mutate("Playlist renamed", "Couldn't rename the playlist. Try again.") {
            it.renamePlaylist(pl.service, pl.id, title)
        }

    fun deletePlaylist(pl: Playlist) = viewModelScope.launch {
        val client = api ?: return@launch
        withContext(Dispatchers.IO) { runCatching { client.deletePlaylist(pl.service, pl.id) } }
            .onSuccess { _state.value = _state.value.copy(openPlaylist = null, message = "Playlist deleted"); loadLibrary() }
            .onFailure { _state.value = _state.value.copy(
                message = friendly(it, "Couldn't delete the playlist. Try again.")) }
    }

    fun addToPlaylist(track: Track, pl: Playlist) = viewModelScope.launch {
        val client = api ?: return@launch
        withContext(Dispatchers.IO) { runCatching { client.addTracks(pl.service, pl.id, listOf(track.id)) } }
            .onSuccess { _state.value = _state.value.copy(message = "Added to ${pl.title}") }
            .onFailure { _state.value = _state.value.copy(
                message = friendly(it, "Couldn't add to ${pl.title}. Try again.")) }
    }

    fun removeFromPlaylist(track: Track) = viewModelScope.launch {
        val client = api ?: return@launch
        val pl = _state.value.openPlaylist ?: return@launch
        withContext(Dispatchers.IO) { runCatching { client.removeTracks(pl.service, pl.id, listOf(track.id)) } }
            .onSuccess {
                _state.value = _state.value.copy(
                    playlistTracks = _state.value.playlistTracks.filterNot { it.id == track.id },
                    undoableRemove = track)
            }.onFailure { _state.value = _state.value.copy(
                message = friendly(it, "Couldn't remove the track. Try again.")) }
    }

    /** Re-add the last track removed from the open playlist (backs an undo Snackbar). */
    fun undoRemove() {
        val track = _state.value.undoableRemove ?: return
        val pl = _state.value.openPlaylist
        _state.value = _state.value.copy(undoableRemove = null)
        if (pl == null) return
        viewModelScope.launch {
            val client = api ?: return@launch
            withContext(Dispatchers.IO) { runCatching { client.addTracks(pl.service, pl.id, listOf(track.id)) } }
                .onSuccess { openPlaylist(pl) }
                .onFailure { _state.value = _state.value.copy(
                    message = "Couldn't restore the track. Try adding it again.") }
        }
    }

    fun clearUndo() { _state.value = _state.value.copy(undoableRemove = null) }

    /** Run a mutating call, then refresh the playlist list. */
    private fun mutate(okMsg: String, failMsg: String, block: (HarmonyApi) -> Unit) = viewModelScope.launch {
        val client = api ?: return@launch
        withContext(Dispatchers.IO) { runCatching { block(client) } }
            .onSuccess { _state.value = _state.value.copy(message = okMsg); loadLibrary() }
            .onFailure { _state.value = _state.value.copy(message = friendly(it, failMsg)) }
    }

    /** Map common network/auth failures to friendly copy; keep the raw message for logs. */
    private fun friendly(t: Throwable, fallback: String): String {
        android.util.Log.w("Harmony", fallback, t)
        val msg = t.message ?: ""
        return when {
            t is java.net.UnknownHostException ->
                "Couldn't reach the server. Check the address, then try again."
            t is java.net.ConnectException ->
                "Couldn't connect. Check the address and key, then try again."
            "401" in msg || "403" in msg ->
                "That key wasn't accepted. Check your personal key and try again."
            else -> fallback
        }
    }

    // -- devices ------------------------------------------------------------

    fun loadDevices() {
        val client = api ?: return
        viewModelScope.launch {
            withContext(Dispatchers.IO) { runCatching { client.devices() } }
                .onSuccess { _state.value = _state.value.copy(devices = it) }
        }
    }

    // -- sync ---------------------------------------------------------------

    fun syncPreview(src: Pair<String, String>, tgt: Pair<String, String>, direction: String) {
        val client = api ?: return
        _state.value = _state.value.copy(syncBusy = true, syncPlan = null, syncMsg = "Planning…")
        viewModelScope.launch {
            val res = withContext(Dispatchers.IO) { runCatching { client.syncPlan(src, tgt, direction) } }
            res.onSuccess {
                val note = if (it.notes.isEmpty()) "" else " " + it.notes.joinToString(" ")
                _state.value = _state.value.copy(syncBusy = false, syncPlan = it,
                    syncMsg = "${it.adds} to add, ${it.removes} to remove, ${it.unmatched} unmatched.$note")
            }.onFailure { _state.value = _state.value.copy(syncBusy = false, syncMsg = "Plan failed: ${it.message}") }
        }
    }

    fun syncApply() {
        val client = api ?: return
        val token = _state.value.syncPlan?.token ?: return
        _state.value = _state.value.copy(syncBusy = true, syncMsg = "Applying…")
        viewModelScope.launch {
            val res = withContext(Dispatchers.IO) { runCatching { client.syncApply(token) } }
            res.onSuccess {
                _state.value = _state.value.copy(syncBusy = false, syncPlan = null,
                    syncMsg = "Added ${it.added}, removed ${it.removed}" +
                        if (it.failed > 0) ", ${it.failed} failed." else ".")
            }.onFailure { _state.value = _state.value.copy(syncBusy = false, syncMsg = "Apply failed: ${it.message}") }
        }
    }

    // -- accounts: adopt credentials from a peer ("Sync accounts") ----------

    /** Ask the connected instance to pull [peerHost]:[peerPort]'s streaming
     *  credentials (both instances must share the same personal key). Runs off the
     *  main thread and reports the outcome into [UiState.accountSyncMsg]. */
    fun syncAccounts(peerHost: String, peerPort: Int) {
        val client = api ?: return
        val host = peerHost.trim()
        if (host.isEmpty()) {
            _state.value = _state.value.copy(accountSyncMsg = "Enter the server's address first.")
            return
        }
        _state.value = _state.value.copy(accountSyncBusy = true, accountSyncMsg = "Syncing…")
        viewModelScope.launch {
            val res = withContext(Dispatchers.IO) { runCatching { client.adoptCredentials(host, peerPort) } }
            res.onSuccess {
                val n = it.size
                _state.value = _state.value.copy(accountSyncBusy = false,
                    accountSyncMsg = "Synced $n credential${if (n == 1) "" else "s"} from $host.")
            }.onFailure {
                _state.value = _state.value.copy(accountSyncBusy = false,
                    accountSyncMsg = friendly(it, "Couldn't sync accounts. Try again."))
            }
        }
    }

    fun togglePlayPause() {
        val target = _state.value.target
        if (target != "phone") {  // control the cast device
            val client = api ?: return
            val paused = _state.value.devicePaused
            _state.value = _state.value.copy(devicePaused = !paused)
            viewModelScope.launch {
                withContext(Dispatchers.IO) {
                    runCatching { client.deviceControl(target, if (paused) "resume" else "pause") }
                }
            }
            return
        }
        if (player.isPlaying) player.pause() else player.play()
    }

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
        advanceJob?.cancel(); preShuffleOrder = null
        player.setMediaItem(MediaItem.fromUri(client.monitorUrl()))
        player.prepare()
        player.play()
        _state.value = _state.value.copy(playingHere = true,
            playback = Playback(track = null, isPlaying = true),
            queue = emptyList(), activeIndex = -1,
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
        instanceServer.stop()
        discovery.stop()
        player.release()
        super.onCleared()
    }
}
