package io.github.marthofdoom.harmony

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Cloud
import androidx.compose.material.icons.filled.LibraryMusic
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Speaker
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.filled.Sync
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            HarmonyTheme {
                Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    App()
                }
            }
        }
    }
}

@Composable
private fun App(vm: HarmonyViewModel = viewModel()) {
    val state by vm.state.collectAsStateWithLifecycle()
    val snackbar = remember { SnackbarHostState() }

    LaunchedEffect(state.message) {
        state.message?.let { snackbar.showSnackbar(it); vm.clearMessage() }
    }

    Scaffold(snackbarHost = { SnackbarHost(snackbar) }) { padding ->
        Box(Modifier.padding(padding)) {
            if (state.conn == ConnState.CONNECTED) ConnectedScreen(vm, state)
            else ConnectScreen(vm, state)
        }
    }
}

// ── Connect ──────────────────────────────────────────────────────────────

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ConnectScreen(vm: HarmonyViewModel, state: UiState) {
    var host by rememberSaveable { mutableStateOf("") }
    var key by rememberSaveable { mutableStateOf("") }

    Scaffold(topBar = { TopAppBar(title = { Text("Connect to Harmony") }) }) { pad ->
        Column(Modifier.padding(pad).padding(16.dp).fillMaxSize()) {
            Text("On your network", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            if (state.discovered.isEmpty()) {
                Text("Searching for instances…", style = MaterialTheme.typography.bodyMedium)
            } else {
                LazyColumn(Modifier.fillMaxWidth().weight(1f, fill = false)) {
                    items(state.discovered) { inst ->
                        Card(
                            Modifier.fillMaxWidth().padding(vertical = 4.dp)
                                .clickable { host = "${inst.host}:${inst.port}" }
                        ) {
                            Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Filled.Cloud, null)
                                Spacer(Modifier.width(12.dp))
                                Column {
                                    Text(inst.name, style = MaterialTheme.typography.bodyLarge)
                                    Text("${inst.host}:${inst.port}", style = MaterialTheme.typography.bodySmall)
                                }
                            }
                        }
                    }
                }
            }

            Spacer(Modifier.height(16.dp))
            Text("Or enter it manually", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = host, onValueChange = { host = it },
                label = { Text("host:port  (e.g. 192.168.1.10:8080)") },
                singleLine = true, modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = key, onValueChange = { key = it },
                label = { Text("Personal key (if the instance requires one)") },
                singleLine = true, modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(16.dp))
            Button(
                onClick = { vm.connect(normalizeBaseUrl(host), key.ifBlank { null }) },
                enabled = host.isNotBlank() && state.conn != ConnState.CONNECTING,
                modifier = Modifier.fillMaxWidth(),
            ) {
                if (state.conn == ConnState.CONNECTING) {
                    CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(8.dp)); Text("Connecting…")
                } else Text("Connect")
            }
        }
    }
}

private fun normalizeBaseUrl(hostPort: String): String {
    val h = hostPort.trim()
    return if (h.startsWith("http://") || h.startsWith("https://")) h else "http://$h"
}

// ── Connected (Search / Now Playing) ─────────────────────────────────────

@Composable
private fun ConnectedScreen(vm: HarmonyViewModel, state: UiState) {
    var tab by rememberSaveable { mutableIntStateOf(0) }
    Scaffold(
        bottomBar = {
            NavigationBar {
                NavigationBarItem(selected = tab == 0, onClick = { tab = 0 },
                    icon = { Icon(Icons.Filled.Search, null) }, label = { Text("Search") })
                NavigationBarItem(selected = tab == 1, onClick = { tab = 1 },
                    icon = { Icon(Icons.Filled.LibraryMusic, null) }, label = { Text("Library") })
                NavigationBarItem(selected = tab == 2, onClick = { tab = 2 },
                    icon = { Icon(Icons.Filled.Sync, null) }, label = { Text("Sync") })
                NavigationBarItem(selected = tab == 3, onClick = { tab = 3 },
                    icon = { Icon(Icons.Filled.Speaker, null) }, label = { Text("Route") })
                NavigationBarItem(selected = tab == 4, onClick = { tab = 4 },
                    icon = { Icon(Icons.Filled.MusicNote, null) }, label = { Text("Playing") })
            }
        }
    ) { pad ->
        Box(Modifier.padding(pad)) {
            when (tab) {
                0 -> SearchScreen(vm, state)
                1 -> LibraryScreen(vm, state)
                2 -> SyncScreen(vm, state)
                3 -> RouteScreen(vm, state)
                else -> NowPlayingScreen(vm, state)
            }
        }
    }
}

// ── Library (playlists + editing) ────────────────────────────────────────

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun LibraryScreen(vm: HarmonyViewModel, state: UiState) {
    val open = state.openPlaylist
    Column(Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text(open?.title ?: "Library", maxLines = 1, overflow = TextOverflow.Ellipsis) },
            navigationIcon = {
                if (open != null) IconButton(onClick = { vm.closePlaylist() }) {
                    Icon(Icons.Filled.ArrowBack, "Back")
                }
            },
            actions = {
                if (open != null) {
                    TextButton(onClick = { vm.deletePlaylist(open) }) { Text("Delete") }
                } else {
                    TextButton(onClick = { vm.loadLibrary() }) { Text("Refresh") }
                }
            },
        )
        if (state.libraryLoading) {
            Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
        }
        if (open == null) {
            var showNew by remember { mutableStateOf(false) }
            if (showNew) NewPlaylistDialog(onDismiss = { showNew = false },
                onCreate = { svc, title -> vm.createPlaylist(svc, title); showNew = false })
            Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) {
                Button(onClick = { showNew = true }) {
                    Icon(Icons.Filled.Add, null); Spacer(Modifier.width(8.dp)); Text("New playlist")
                }
            }
            LazyColumn(Modifier.fillMaxSize()) {
                items(state.playlists) { pl ->
                    Row(Modifier.fillMaxWidth().clickable { vm.openPlaylist(pl) }
                        .padding(horizontal = 16.dp, vertical = 8.dp),
                        verticalAlignment = Alignment.CenterVertically) {
                        NetworkImage(pl.artworkUrl, Modifier.size(48.dp).clip(RoundedCornerShape(6.dp)))
                        Spacer(Modifier.width(12.dp))
                        Column(Modifier.weight(1f)) {
                            Text(pl.title, maxLines = 1, overflow = TextOverflow.Ellipsis,
                                style = MaterialTheme.typography.bodyLarge)
                            Text("${pl.service.uppercase().take(3)} · ${pl.trackCount ?: "?"} tracks",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
            }
        } else {
            LazyColumn(Modifier.fillMaxSize()) {
                items(state.playlistTracks) { t ->
                    TrackRow(t, onPlay = { vm.play(t) },
                        trailing = {
                            IconButton(onClick = { vm.removeFromPlaylist(t) }) {
                                Icon(Icons.Filled.Close, "Remove")
                            }
                        })
                }
            }
        }
    }
}

@Composable
private fun NewPlaylistDialog(onDismiss: () -> Unit, onCreate: (String, String) -> Unit) {
    var title by remember { mutableStateOf("") }
    var service by remember { mutableStateOf("qobuz") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("New playlist") },
        text = {
            Column {
                OutlinedTextField(value = title, onValueChange = { title = it },
                    label = { Text("Title") }, singleLine = true)
                Spacer(Modifier.height(8.dp))
                Row {
                    FilterChip(selected = service == "qobuz", onClick = { service = "qobuz" },
                        label = { Text("Qobuz") })
                    Spacer(Modifier.width(8.dp))
                    FilterChip(selected = service == "ytmusic", onClick = { service = "ytmusic" },
                        label = { Text("YT Music") })
                }
            }
        },
        confirmButton = {
            TextButton(onClick = { if (title.isNotBlank()) onCreate(service, title.trim()) }) { Text("Create") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

// ── Sync ─────────────────────────────────────────────────────────────────

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SyncScreen(vm: HarmonyViewModel, state: UiState) {
    val playlists = state.playlists
    var src by remember { mutableStateOf<Playlist?>(null) }
    var tgt by remember { mutableStateOf<Playlist?>(null) }
    var dir by remember { mutableStateOf("a_to_b") }
    Column(Modifier.fillMaxSize()) {
        TopAppBar(title = { Text("Sync playlists") })
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
            Text("Mirror one playlist onto another across services. Preview first — nothing is written until you apply.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(12.dp))
            PlaylistDropdown("Source", playlists, src) { src = it }
            Spacer(Modifier.height(8.dp))
            PlaylistDropdown("Target", playlists, tgt) { tgt = it }
            Spacer(Modifier.height(8.dp))
            Text("Direction", style = MaterialTheme.typography.labelMedium)
            Row {
                listOf("a_to_b" to "Source→Target", "b_to_a" to "Target→Source", "two_way" to "Two-way").forEach {
                    FilterChip(selected = dir == it.first, onClick = { dir = it.first },
                        label = { Text(it.second) }, modifier = Modifier.padding(end = 6.dp))
                }
            }
            Spacer(Modifier.height(12.dp))
            Row {
                Button(enabled = src != null && tgt != null && !state.syncBusy,
                    onClick = { vm.syncPreview(src!!.service to src!!.id, tgt!!.service to tgt!!.id, dir) }) {
                    Text("Preview")
                }
                Spacer(Modifier.width(8.dp))
                Button(enabled = state.syncPlan?.token != null && !state.syncBusy, onClick = { vm.syncApply() }) {
                    Text("Apply")
                }
            }
            state.syncMsg?.let {
                Spacer(Modifier.height(12.dp))
                Text(it, style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun PlaylistDropdown(label: String, playlists: List<Playlist>, selected: Playlist?,
                             onSelect: (Playlist) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
        OutlinedTextField(
            value = selected?.let { "${it.title} — ${it.service.uppercase().take(3)}" } ?: "",
            onValueChange = {}, readOnly = true, label = { Text(label) },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            modifier = Modifier.menuAnchor().fillMaxWidth(),
        )
        ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            playlists.forEach { pl ->
                DropdownMenuItem(text = { Text("${pl.title} — ${pl.service.uppercase().take(3)}") },
                    onClick = { onSelect(pl); expanded = false })
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun RouteScreen(vm: HarmonyViewModel, state: UiState) {
    Column(Modifier.fillMaxSize()) {
        TopAppBar(title = { Text("Route audio") })
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {

            // 1) Play the connected hub's audio on this phone (RTP → the speaker).
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Text("Play on this phone", style = MaterialTheme.typography.titleMedium)
                    Text("Stream ${state.instanceName ?: "this hub"}'s audio to your phone.",
                        style = MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(12.dp))
                    if (state.playingHere) {
                        Button(onClick = { vm.stopPlayHere() }, modifier = Modifier.fillMaxWidth()) {
                            Icon(Icons.Filled.Stop, null); Spacer(Modifier.width(8.dp)); Text("Stop")
                        }
                    } else {
                        Button(onClick = { vm.playHere() }, modifier = Modifier.fillMaxWidth()) {
                            Icon(Icons.Filled.Speaker, null); Spacer(Modifier.width(8.dp)); Text("Play here")
                        }
                    }
                }
            }

            // 2) Bridge: play the current track on a device on the phone's LAN,
            //    relaying through the phone (works even when the hub is remote).
            Spacer(Modifier.height(16.dp))
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("Play on a local device", style = MaterialTheme.typography.titleMedium,
                            modifier = Modifier.weight(1f))
                        TextButton(onClick = { vm.discoverRenderers() },
                            enabled = !state.discoveringRenderers) { Text("Find") }
                    }
                    Text("Cast the current track to a speaker/TV on this phone's network.",
                        style = MaterialTheme.typography.bodySmall)
                    if (state.discoveringRenderers) {
                        Spacer(Modifier.height(8.dp))
                        CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                    }
                    state.renderers.forEach { r ->
                        Row(Modifier.fillMaxWidth().clickable { vm.bridgeToRenderer(r) }
                            .padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Filled.Speaker, null)
                            Spacer(Modifier.width(12.dp))
                            Text(r.name, modifier = Modifier.weight(1f))
                            if (state.bridgingTo == r.name) Text("playing",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.primary)
                        }
                    }
                    if (state.renderers.isEmpty() && !state.discoveringRenderers) {
                        Text("No devices found yet — tap Find.", style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 8.dp))
                    }
                    if (state.bridgingTo != null) {
                        Spacer(Modifier.height(8.dp))
                        Button(onClick = { vm.stopBridge() }, modifier = Modifier.fillMaxWidth()) {
                            Icon(Icons.Filled.Stop, null); Spacer(Modifier.width(8.dp)); Text("Stop")
                        }
                    }
                }
            }

            // 3) Route between two Harmony hubs.
            Spacer(Modifier.height(16.dp))
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("Route between hubs", style = MaterialTheme.typography.titleMedium,
                            modifier = Modifier.weight(1f))
                        TextButton(onClick = { vm.refreshPeers() }) { Text("Refresh") }
                    }
                    if (state.peers.isEmpty()) {
                        Text("No other instances found.", style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 8.dp))
                    }
                    state.peers.forEach { peer ->
                        Column(Modifier.padding(vertical = 8.dp)) {
                            Text(peer.name, style = MaterialTheme.typography.bodyLarge)
                            Text("${peer.host}:${peer.port}", style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                            Row {
                                TextButton(onClick = { vm.route("send", peer) }) { Text("Send to") }
                                Spacer(Modifier.width(8.dp))
                                TextButton(onClick = { vm.route("receive", peer) }) { Text("Play theirs") }
                            }
                        }
                    }
                }
            }

            state.routeStatus?.let {
                Spacer(Modifier.height(12.dp))
                Text(it, style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SearchScreen(vm: HarmonyViewModel, state: UiState) {
    Column(Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text(state.instanceName ?: "Harmony", maxLines = 1, overflow = TextOverflow.Ellipsis) },
            actions = { TextButton(onClick = { vm.disconnect() }) { Text("Disconnect") } },
        )
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                value = state.query, onValueChange = { vm.setQuery(it) },
                label = { Text("Search songs") }, singleLine = true, modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(8.dp))
            IconButton(onClick = { vm.search() }) { Icon(Icons.Filled.Search, "Search") }
        }
        if (state.searching) {
            Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
        }
        if (state.results.isEmpty() && !state.searching) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(if (state.query.isBlank()) "Search for a song" else "No results",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        LazyColumn(Modifier.fillMaxSize()) {
            items(state.results) { t ->
                TrackRow(t, onPlay = { vm.play(t) },
                    trailing = { AddToPlaylistButton(vm, state, t) })
            }
        }
    }
}

/** A track list row: art, title, artist·album, and an optional trailing action. */
@Composable
private fun TrackRow(t: Track, onPlay: () -> Unit, trailing: @Composable (() -> Unit)? = null) {
    Row(Modifier.fillMaxWidth().clickable { onPlay() }.padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        NetworkImage(t.artworkUrl, Modifier.size(48.dp).clip(RoundedCornerShape(6.dp)))
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(t.title, maxLines = 1, overflow = TextOverflow.Ellipsis,
                style = MaterialTheme.typography.bodyLarge)
            Text(listOfNotNull(t.artist.ifBlank { null }, t.album).joinToString(" · "),
                maxLines = 1, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        trailing?.invoke()
    }
}

@Composable
private fun AddToPlaylistButton(vm: HarmonyViewModel, state: UiState, track: Track) {
    var expanded by remember { mutableStateOf(false) }
    Box {
        IconButton(onClick = { expanded = true }) { Icon(Icons.Filled.Add, "Add to playlist") }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            if (state.playlists.isEmpty()) {
                DropdownMenuItem(text = { Text("No playlists") }, onClick = { expanded = false })
            }
            state.playlists.forEach { pl ->
                DropdownMenuItem(text = { Text("${pl.title} · ${pl.service.uppercase().take(3)}") },
                    onClick = { vm.addToPlaylist(track, pl); expanded = false })
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun NowPlayingScreen(vm: HarmonyViewModel, state: UiState) {
    val pb = state.playback
    Column(
        Modifier.fillMaxSize().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        NetworkImage(
            pb.track?.artworkUrl,
            Modifier.size(240.dp).padding(bottom = 24.dp).clip(RoundedCornerShape(14.dp)),
        )
        Text(pb.track?.title ?: "Nothing playing", style = MaterialTheme.typography.titleLarge,
            maxLines = 1, overflow = TextOverflow.Ellipsis)
        Text(pb.track?.artist ?: "", style = MaterialTheme.typography.bodyMedium,
            maxLines = 1, overflow = TextOverflow.Ellipsis)
        Spacer(Modifier.height(24.dp))

        val dur = pb.durationMs.coerceAtLeast(0)
        val pos = pb.positionMs.coerceIn(0, if (dur > 0) dur else pb.positionMs)
        androidx.compose.material3.Slider(
            value = if (dur > 0) pos.toFloat() / dur else 0f,
            onValueChange = { if (dur > 0) vm.seekTo((it * dur).toLong()) },
            enabled = pb.track != null && dur > 0,
        )
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(formatMs(pos), style = MaterialTheme.typography.labelSmall)
            Text(formatMs(dur), style = MaterialTheme.typography.labelSmall)
        }
        Spacer(Modifier.height(16.dp))
        val onDevice = state.target != "phone"
        val playing = if (onDevice) !state.devicePaused else pb.isPlaying
        FilledIconButton(onClick = { vm.togglePlayPause() }, enabled = pb.track != null,
            modifier = Modifier.size(72.dp)) {
            Icon(
                if (playing) Icons.Filled.Pause else Icons.Filled.PlayArrow,
                contentDescription = if (playing) "Pause" else "Play",
                modifier = Modifier.size(40.dp),
            )
        }
        if (onDevice) {
            Spacer(Modifier.height(8.dp))
            Text("Casting to ${state.devices.firstOrNull { it.host == state.target }?.name ?: "a device"}",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.primary)
        }
        Spacer(Modifier.height(16.dp))
        OutputSelector(vm, state)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun OutputSelector(vm: HarmonyViewModel, state: UiState) {
    var expanded by remember { mutableStateOf(false) }
    val label = if (state.target == "phone") "This phone"
    else state.devices.firstOrNull { it.host == state.target }?.name ?: state.target
    ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
        OutlinedTextField(
            value = label, onValueChange = {}, readOnly = true, label = { Text("Output") },
            leadingIcon = { Icon(Icons.Filled.Speaker, null) },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            modifier = Modifier.menuAnchor().fillMaxWidth(),
        )
        ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            DropdownMenuItem(text = { Text("This phone") },
                onClick = { vm.setTarget("phone"); expanded = false })
            state.devices.forEach { d ->
                DropdownMenuItem(text = { Text("${d.name} · ${d.kind}") },
                    onClick = { vm.setTarget(d.host); expanded = false })
            }
        }
    }
}

private fun formatMs(ms: Long): String {
    val totalSec = ms / 1000
    return "%d:%02d".format(totalSec / 60, totalSec % 60)
}
