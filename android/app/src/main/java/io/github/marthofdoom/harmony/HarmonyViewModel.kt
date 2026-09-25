package io.github.marthofdoom.harmony

import android.app.Application
import android.content.ComponentName
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import com.google.common.util.concurrent.ListenableFuture
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

enum class ConnState { DISCONNECTED, CONNECTING, CONNECTED }

enum class DetailKind { ARTIST, ALBUM, TRACK }

/** Whole-list actions offered on album / playlist rows. */
enum class ListAction { PLAY, SHUFFLE, PLAY_NEXT, ADD_TO_QUEUE }

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
    // the discovered instance currently being connected to (spinner on that card only)
    val connectingUrl: String? = null,
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
    // Now Playing opened full-screen from the mini player (over any tab/detail)
    val nowPlayingOpen: Boolean = false,
    // ── mirrored from PlaybackController (the process-scoped owner) ──
    val playback: Playback = Playback(),
    // The ACTIVE PLAY QUEUE (history + current + up next); for a device output
    // this mirrors the server-owned device queue.
    val queue: List<Track> = emptyList(),
    // Index of the currently-playing track within [queue] (-1 when nothing is queued).
    val activeIndex: Int = -1,
    val shuffle: Boolean = false,
    val repeatMode: RepeatMode = RepeatMode.OFF,
    val buffering: Boolean = false,
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
    // output: PHONE (this device) or a hub device's host (+ via for a peer's device)
    val devices: List<Device> = emptyList(),
    val target: String = PHONE,
    val targetVia: String? = null,
    val targetName: String? = null,
    val devicePaused: Boolean = false,
    val deviceVolume: Int? = null,
    // sync
    val syncPlan: SyncPlan? = null,
    val syncBusy: Boolean = false,
    val syncMsg: String? = null,
    // account credential adopt ("Sync accounts" — pull logins from a peer)
    val accountSyncBusy: Boolean = false,
    val accountSyncMsg: String? = null,
)

/** Row playing-indicator state for a track. */
enum class RowPlay { NONE, PLAYING, PAUSED }

/** The current track matches by service+id; shows paused vs playing. */
fun UiState.rowPlay(t: Track): RowPlay {
    val cur = playback.track ?: return RowPlay.NONE
    if (cur.id != t.id || cur.service != t.service) return RowPlay.NONE
    return if (playback.isPlaying) RowPlay.PLAYING else RowPlay.PAUSED
}

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
    private var ownPort = 0
    private var ownAddrs: Set<String> = emptySet()

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    /** Playback lives in the process (it outlives this ViewModel / the Activity). */
    private val player = PlaybackController.get(app)
    // Binding a MediaController starts the PlaybackService (media session +
    // foreground notification while playing).
    private var controllerFuture: ListenableFuture<MediaController>? = null

    init {
        viewModelScope.launch {
            discovery.instances.collect { list ->
                _state.value = _state.value.copy(discovered = list.filterNot { isSelf(it) })
            }
        }
        viewModelScope.launch {
            player.state.collect { ps ->
                _state.value = _state.value.copy(
                    playback = Playback(ps.current, ps.isPlaying, ps.positionMs, ps.durationMs),
                    queue = ps.queue, activeIndex = ps.index,
                    shuffle = ps.shuffle, repeatMode = ps.repeat, buffering = ps.buffering,
                    playingHere = ps.monitoring,
                    target = ps.target, targetVia = ps.targetVia, targetName = ps.targetName,
                    devicePaused = ps.devicePaused, deviceVolume = ps.deviceVolume,
                )
            }
        }
        viewModelScope.launch {
            player.messages.collect { _state.value = _state.value.copy(message = it) }
        }
        controllerFuture = runCatching {
            MediaController.Builder(app, SessionToken(app, ComponentName(app, PlaybackService::class.java)))
                .buildAsync()
        }.getOrNull()
        discovery.start()
        // Stand up the phone's mesh presence, then advertise the port it bound.
        runCatching {
            ownPort = instanceServer.start()
            discovery.advertise(ownPort, instanceName)
        }
        ownAddrs = localAddresses()
        // Reconnect to the last instance if we have one saved.
        val saved = prefs.baseUrl
        if (saved != null) connect(saved, prefs.key)
    }

    private fun appVersion(app: Application): String =
        runCatching { app.packageManager.getPackageInfo(app.packageName, 0).versionName ?: "0" }
            .getOrDefault("0")

    /** This phone's own NSD advertisement must not show up as an instance to
     *  connect to (it's a presence stub, not a hub). NSD may rename it on a
     *  collision ("name (2)"), so match by our port on one of our addresses too. */
    private fun isSelf(i: Instance): Boolean {
        if (i.name == instanceName || i.name.startsWith("$instanceName (")) return true
        return ownPort > 0 && i.port == ownPort &&
            (i.host.substringBefore('%') in ownAddrs || i.host == "127.0.0.1" || i.host == "::1")
    }

    private fun localAddresses(): Set<String> = runCatching {
        java.net.NetworkInterface.getNetworkInterfaces().toList()
            .flatMap { it.inetAddresses.toList() }
            .mapNotNull { it.hostAddress?.substringBefore('%') }.toSet()
    }.getOrDefault(emptySet())

    fun startDiscovery() = discovery.start()

    /** Forget the found list and browse again (Rescan button). */
    fun rescan() {
        ownAddrs = localAddresses()
        discovery.rescan()
    }

    fun connect(baseUrl: String, key: String?) {
        _state.value = _state.value.copy(conn = ConnState.CONNECTING, connectingUrl = baseUrl, message = null)
        viewModelScope.launch {
            val client = HarmonyApi(baseUrl, key)
            // Hit the API directly so the real failure surfaces (a blocked
            // cleartext call, a refused connection, or a 401 for a wrong key)
            // instead of a generic "not found".
            val result = withContext(Dispatchers.IO) { runCatching { client.accounts() } }
            result.onSuccess {
                api = client
                player.api = client
                prefs.baseUrl = baseUrl; prefs.key = key
                val name = _state.value.discovered.firstOrNull { it.baseUrl == baseUrl }?.name ?: baseUrl
                _state.value = _state.value.copy(conn = ConnState.CONNECTED, instanceName = name,
                    connectingUrl = null)
                refreshPeers(); loadLibrary(); loadDevices()
            }.onFailure {
                _state.value = _state.value.copy(conn = ConnState.DISCONNECTED, connectingUrl = null,
                    message = friendly(it, "Couldn't connect. Check the address and key, then try again."))
            }
        }
    }

    /** Disconnect from the instance. Playback stops, but the queue is kept (and
     *  persisted) so reconnecting picks up where you were. */
    fun disconnect() {
        rtp.stop(); relay.stop()
        api = null
        player.onDisconnected()
        prefs.baseUrl = null
        _state.value = _state.value.copy(conn = ConnState.DISCONNECTED, instanceName = null,
            results = emptyList(), query = "",
            smart = null, detailStack = emptyList(), tab = 0, nowPlayingOpen = false,
            peers = emptyList(), routeStatus = null,
            renderers = emptyList(), bridgingTo = null,
            playlists = emptyList(), openPlaylist = null, playlistTracks = emptyList(),
            devices = emptyList(), syncPlan = null, syncMsg = null,
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

    // -- playback (delegated to the process-scoped PlaybackController) -------

    /** Play [track], loading [queue] (the album/playlist/search list it came from)
     *  as the ACTIVE PLAY QUEUE, keeping the current shuffle mode. */
    fun play(track: Track, queue: List<Track> = listOf(track)) {
        val idx = queue.indexOfFirst { it.id == track.id && it.service == track.service }
        playFrom(if (idx < 0) listOf(track) else queue, idx.coerceAtLeast(0))
    }

    /** Play [list] starting at [index] (a tapped row), keeping the shuffle mode. */
    fun playFrom(list: List<Track>, index: Int) {
        if (list.isEmpty()) return
        player.playList(list, index.coerceIn(0, list.size - 1), shuffle = null)
    }

    /** Play / Shuffle buttons: the whole list in order, or shuffle-play it. */
    fun playAll(list: List<Track>, shuffle: Boolean) {
        if (list.isEmpty()) return
        player.playList(list, if (shuffle) null else 0, shuffle = shuffle)
    }

    fun next() = player.next()
    fun prev() = player.prev()
    fun jumpTo(index: Int) = player.jump(index)
    fun addToQueue(tracks: List<Track>) = player.enqueue(tracks)
    fun addToQueue(track: Track) = player.enqueue(listOf(track))
    fun playNext(tracks: List<Track>) = player.playNext(tracks)
    fun playNext(track: Track) = player.playNext(listOf(track))
    fun moveQueueItem(from: Int, to: Int) = player.move(from, to)
    fun removeFromQueue(index: Int) = player.remove(index)
    fun clearQueue() = player.clear()
    fun stopPlayback() = player.stop()
    fun toggleShuffle() = player.setShuffle(!_state.value.shuffle)
    fun togglePlayPause() = player.togglePlayPause()
    fun seekTo(ms: Long) = player.seekTo(ms)
    fun setVolume(level: Int) = player.setVolume(level)

    /** Cycle repeat: off → all → one → off. */
    fun cycleRepeat() {
        player.setRepeat(when (_state.value.repeatMode) {
            RepeatMode.OFF -> RepeatMode.ALL
            RepeatMode.ALL -> RepeatMode.ONE
            RepeatMode.ONE -> RepeatMode.OFF
        })
    }

    /** Switch output (null = this phone); the controller hands the queue off. */
    fun setTarget(device: Device?) = player.setOutput(device?.host, device?.via, device?.name)

    fun showNowPlaying(open: Boolean) { _state.value = _state.value.copy(nowPlayingOpen = open) }

    /** Called from the Activity's onStart/onStop. */
    fun setUiVisible(visible: Boolean) {
        player.uiVisible = visible
        if (!visible) player.persist()
    }

    /** Fetch a list (album / playlist) off the main thread, then act on it. */
    private fun withTracks(failMsg: String, fetch: (HarmonyApi) -> List<Track>, block: (List<Track>) -> Unit) {
        val client = api ?: return
        viewModelScope.launch {
            withContext(Dispatchers.IO) { runCatching { fetch(client) } }
                .onSuccess { if (it.isEmpty()) _state.value = _state.value.copy(message = "Nothing to play there.") else block(it) }
                .onFailure { _state.value = _state.value.copy(message = friendly(it, failMsg)) }
        }
    }

    /** Whole-album actions from an album row (search / artist discography). */
    fun albumAction(service: String, id: String, action: ListAction) =
        withTracks("Couldn't load that album. Try again.", { it.album(service, id).tracks }) { runListAction(it, action) }

    /** Whole-playlist actions from a playlist row. */
    fun playlistAction(pl: Playlist, action: ListAction) =
        withTracks("Couldn't load that playlist. Try again.", { it.playlistTracks(pl.service, pl.id) }) { runListAction(it, action) }

    private fun runListAction(tracks: List<Track>, action: ListAction) = when (action) {
        ListAction.PLAY -> playAll(tracks, shuffle = false)
        ListAction.SHUFFLE -> playAll(tracks, shuffle = true)
        ListAction.PLAY_NEXT -> playNext(tracks)
        ListAction.ADD_TO_QUEUE -> addToQueue(tracks)
    }

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

    /** Open a playlist found in search: show it on the Library tab. */
    fun openPlaylistFromSearch(pl: Playlist) {
        _state.value = _state.value.copy(tab = 1, detailStack = emptyList())
        openPlaylist(pl)
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

    private fun friendly(t: Throwable, fallback: String) = friendlyError(t, fallback)

    // -- devices ------------------------------------------------------------

    /** Human summary of a credential adopt: what synced, what was kept, and what
     *  was rolled back because it didn't work on this instance. */
    private fun adoptSummary(r: AdoptResult, host: String): String {
        val parts = mutableListOf<String>()
        if (r.synced.isNotEmpty()) parts += "Synced " + r.synced.joinToString(", ") { serviceLabel(it) }
        if (r.kept.isNotEmpty()) parts += "kept " + r.kept.joinToString(", ") { serviceLabel(it) }
        r.rolledBack.forEach { parts += "${serviceLabel(it)} didn't work here — kept your existing login" }
        if (parts.isEmpty()) return "Nothing to sync — $host has no working logins to share."
        return parts.joinToString(" · ").replaceFirstChar { it.uppercase() }
    }

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
                _state.value = _state.value.copy(accountSyncBusy = false,
                    accountSyncMsg = adoptSummary(it, host))
            }.onFailure {
                _state.value = _state.value.copy(accountSyncBusy = false,
                    accountSyncMsg = friendly(it, "Couldn't sync accounts. Try again."))
            }
        }
    }

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
        player.startMonitor(client.monitorUrl())
        _state.value = _state.value.copy(
            routeStatus = "Playing ${_state.value.instanceName ?: "this hub"}'s audio. Your queue is kept — press Play to go back to it.")
    }

    fun stopPlayHere() {
        player.stopMonitor()
        _state.value = _state.value.copy(routeStatus = "Stopped.")
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

    override fun onCleared() {
        rtp.stop()
        relay.stop()
        instanceServer.stop()
        discovery.stop()
        // The player is NOT released: it belongs to the process/PlaybackService
        // so Back on the root screen doesn't kill playback.
        controllerFuture?.let { MediaController.releaseFuture(it) }
        player.persist()
        super.onCleared()
    }
}
