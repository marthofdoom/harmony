package io.github.marthofdoom.harmony

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Cloud
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.LibraryMusic
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Speaker
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.filled.Sync
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Slider
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarResult
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
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
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
    // Removing a track from a playlist offers an undo.
    LaunchedEffect(state.undoableRemove) {
        val t = state.undoableRemove ?: return@LaunchedEffect
        val res = snackbar.showSnackbar(
            message = "Removed “${t.title}”",
            actionLabel = "Undo",
            withDismissAction = true,
        )
        if (res == SnackbarResult.ActionPerformed) vm.undoRemove() else vm.clearUndo()
    }

    // The outer Scaffold only hosts the Snackbar; it consumes no window insets so
    // each destination's own Scaffold/TopAppBar/NavigationBar applies them exactly once.
    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
    ) { padding ->
        Box(Modifier.padding(padding)) {
            if (state.conn == ConnState.CONNECTED) ConnectedScreen(vm, state)
            else ConnectScreen(vm, state)
        }
    }
}

// ── Shared pieces ─────────────────────────────────────────────────────────

/** A centered empty/placeholder state: icon, title, supporting line, optional action. */
@Composable
private fun EmptyState(
    icon: ImageVector,
    title: String,
    body: String,
    modifier: Modifier = Modifier,
    action: (@Composable () -> Unit)? = null,
) {
    Column(
        modifier.padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Icon(icon, null, Modifier.size(48.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(12.dp))
        Text(title, style = MaterialTheme.typography.titleMedium, textAlign = TextAlign.Center)
        Spacer(Modifier.height(4.dp))
        Text(body, style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant, textAlign = TextAlign.Center)
        if (action != null) {
            Spacer(Modifier.height(20.dp))
            action()
        }
    }
}

/** A human label for a service id: "qobuz" → "Qobuz", "ytmusic" → "YouTube Music". */
private fun serviceLabel(service: String): String = when (service.lowercase()) {
    "qobuz" -> "Qobuz"
    "ytmusic", "youtube", "youtubemusic", "yt" -> "YouTube Music"
    "spotify" -> "Spotify"
    "tidal" -> "Tidal"
    else -> service.replaceFirstChar { if (it.isLowerCase()) it.titlecase() else it.toString() }
}

/** Plural-aware track count, or null when unknown (so callers can hide it). */
private fun trackCountLabel(count: Int?): String? = when {
    count == null -> null
    count == 1 -> "1 track"
    else -> "$count tracks"
}

// ── Connect ──────────────────────────────────────────────────────────────

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ConnectScreen(vm: HarmonyViewModel, state: UiState) {
    var host by rememberSaveable { mutableStateOf("") }
    var key by rememberSaveable { mutableStateOf("") }
    val focus = LocalFocusManager.current

    Scaffold(topBar = { TopAppBar(title = { Text("Connect to Harmony") }) }) { pad ->
        Column(
            Modifier
                .padding(pad)
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .imePadding()
                .padding(16.dp)
        ) {
            Text("On your network", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            if (state.discovered.isEmpty()) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(12.dp))
                    Text("Searching for instances…", style = MaterialTheme.typography.bodyMedium)
                }
            } else {
                state.discovered.forEach { inst ->
                    Card(
                        Modifier.fillMaxWidth().padding(vertical = 4.dp)
                            .clickable { vm.connect(inst.baseUrl, key.ifBlank { null }) }
                    ) {
                        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Filled.Cloud, null)
                            Spacer(Modifier.width(12.dp))
                            Column(Modifier.weight(1f)) {
                                Text(inst.name, style = MaterialTheme.typography.bodyLarge)
                                Text("${inst.host}:${inst.port}", style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                            if (state.conn == ConnState.CONNECTING) {
                                CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                            }
                        }
                    }
                }
                Text("Tap an instance to connect.", style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 4.dp))
            }

            Spacer(Modifier.height(16.dp))
            Text("Or enter it manually", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = host, onValueChange = { host = it },
                label = { Text("Server address") },
                supportingText = { Text("host:port  —  e.g. 192.168.1.10:8080") },
                singleLine = true, modifier = Modifier.fillMaxWidth(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Next),
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = key, onValueChange = { key = it },
                label = { Text("Personal key (if the instance requires one)") },
                singleLine = true, modifier = Modifier.fillMaxWidth(),
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = {
                    focus.clearFocus()
                    if (host.isNotBlank()) vm.connect(normalizeBaseUrl(host), key.ifBlank { null })
                }),
            )
            Spacer(Modifier.height(16.dp))
            Button(
                onClick = { focus.clearFocus(); vm.connect(normalizeBaseUrl(host), key.ifBlank { null }) },
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
        // The NavigationBar owns the bottom inset; each screen's TopAppBar owns the
        // top inset. This Scaffold adds none of its own, so insets apply once.
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
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
    // System back closes an open playlist instead of exiting the app.
    BackHandler(enabled = open != null) { vm.closePlaylist() }

    var showDelete by remember { mutableStateOf(false) }
    if (showDelete && open != null) {
        AlertDialog(
            onDismissRequest = { showDelete = false },
            title = { Text("Delete playlist?") },
            text = { Text("Delete “${open.title}”? This can't be undone.") },
            confirmButton = {
                TextButton(
                    onClick = { showDelete = false; vm.deletePlaylist(open) },
                    colors = ButtonDefaults.textButtonColors(
                        contentColor = MaterialTheme.colorScheme.error),
                ) { Text("Delete") }
            },
            dismissButton = { TextButton(onClick = { showDelete = false }) { Text("Cancel") } },
        )
    }

    Column(Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text(open?.title ?: "Library", maxLines = 1, overflow = TextOverflow.Ellipsis) },
            navigationIcon = {
                if (open != null) IconButton(onClick = { vm.closePlaylist() }) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back")
                }
            },
            actions = {
                if (open != null) {
                    TextButton(
                        onClick = { showDelete = true },
                        colors = ButtonDefaults.textButtonColors(
                            contentColor = MaterialTheme.colorScheme.error),
                    ) { Text("Delete") }
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

            if (state.playlists.isEmpty() && !state.libraryLoading) {
                EmptyState(
                    icon = Icons.Filled.LibraryMusic,
                    title = "No playlists yet",
                    body = "Create one to start collecting songs across your services.",
                    modifier = Modifier.fillMaxSize(),
                    action = {
                        Button(onClick = { showNew = true }) {
                            Icon(Icons.Filled.Add, null); Spacer(Modifier.width(8.dp)); Text("New playlist")
                        }
                    },
                )
            } else {
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
                                Text(
                                    listOfNotNull(serviceLabel(pl.service), trackCountLabel(pl.trackCount))
                                        .joinToString(" · "),
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    }
                }
            }
        } else {
            LazyColumn(Modifier.fillMaxSize()) {
                items(state.playlistTracks) { t ->
                    TrackRow(t, onPlay = { vm.play(t) },
                        isPlaying = t.id == state.playback.track?.id,
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

@OptIn(ExperimentalMaterial3Api::class)
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
                        label = { Text("YouTube Music") })
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
            Spacer(Modifier.height(12.dp))
            Text("Direction", style = MaterialTheme.typography.labelMedium)
            Spacer(Modifier.height(4.dp))
            val dirs = listOf(
                "a_to_b" to "Source to target",
                "b_to_a" to "Target to source",
                "two_way" to "Both ways",
            )
            SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                dirs.forEachIndexed { i, (value, lab) ->
                    SegmentedButton(
                        selected = dir == value,
                        onClick = { dir = value },
                        shape = SegmentedButtonDefaults.itemShape(index = i, count = dirs.size),
                    ) { Text(lab, maxLines = 1, overflow = TextOverflow.Ellipsis) }
                }
            }
            Spacer(Modifier.height(16.dp))
            // Preview is a lower-emphasis (tonal) action; Apply is the primary,
            // and lives inside the result card once a plan exists.
            FilledTonalButton(
                enabled = src != null && tgt != null && !state.syncBusy,
                onClick = { vm.syncPreview(src!!.service to src!!.id, tgt!!.service to tgt!!.id, dir) },
            ) {
                if (state.syncBusy && state.syncPlan == null) {
                    CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(8.dp)); Text("Previewing…")
                } else Text("Preview")
            }

            state.syncPlan?.let { plan ->
                Spacer(Modifier.height(16.dp))
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text(
                            listOf(
                                "${plan.adds} to add",
                                "${plan.removes} to remove",
                                "${plan.unmatched} unmatched",
                            ).joinToString(" · "),
                            style = MaterialTheme.typography.titleSmall)
                        if (plan.notes.isNotEmpty()) {
                            Spacer(Modifier.height(4.dp))
                            Text(plan.notes.joinToString(" "), style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        Spacer(Modifier.height(12.dp))
                        Button(enabled = plan.token != null && !state.syncBusy, onClick = { vm.syncApply() }) {
                            if (state.syncBusy) {
                                CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                                Spacer(Modifier.width(8.dp)); Text("Applying…")
                            } else Text("Apply")
                        }
                    }
                }
            }
            // Transient/failure status only (the plan itself renders in the card above).
            state.syncMsg?.takeIf { state.syncPlan == null }?.let {
                Spacer(Modifier.height(12.dp))
                Text(it, style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
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
            value = selected?.let { "${it.title} — ${serviceLabel(it.service)}" } ?: "",
            onValueChange = {}, readOnly = true, label = { Text(label) },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            modifier = Modifier.menuAnchor().fillMaxWidth(),
        )
        ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            playlists.forEach { pl ->
                DropdownMenuItem(text = { Text("${pl.title} — ${serviceLabel(pl.service)}") },
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
                        OutlinedButton(onClick = { vm.stopPlayHere() }, modifier = Modifier.fillMaxWidth()) {
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
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                            Spacer(Modifier.width(12.dp))
                            Text("Looking for devices…", style = MaterialTheme.typography.bodySmall)
                        }
                    }
                    state.renderers.forEach { r ->
                        Row(Modifier.fillMaxWidth().heightIn(min = 48.dp)
                            .clickable { vm.bridgeToRenderer(r) }
                            .padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Filled.Speaker, null)
                            Spacer(Modifier.width(12.dp))
                            Text(r.name, modifier = Modifier.weight(1f))
                            if (state.bridgingTo == r.name) {
                                AssistChip(
                                    onClick = {},
                                    label = { Text("Playing") },
                                    leadingIcon = {
                                        Icon(Icons.Filled.PlayArrow, null,
                                            Modifier.size(AssistChipDefaults.IconSize))
                                    },
                                )
                            }
                        }
                    }
                    if (state.renderers.isEmpty() && !state.discoveringRenderers) {
                        Text("No devices found yet — tap Find.", style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 8.dp))
                    }
                    if (state.bridgingTo != null) {
                        Spacer(Modifier.height(8.dp))
                        OutlinedButton(onClick = { vm.stopBridge() }, modifier = Modifier.fillMaxWidth()) {
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
                        Column(Modifier.fillMaxWidth().heightIn(min = 48.dp).padding(vertical = 8.dp)) {
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
                Spacer(Modifier.height(16.dp))
                Card(Modifier.fillMaxWidth()) {
                    Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Filled.Info, null, tint = MaterialTheme.colorScheme.primary)
                        Spacer(Modifier.width(12.dp))
                        Text(it, style = MaterialTheme.typography.bodyMedium)
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SearchScreen(vm: HarmonyViewModel, state: UiState) {
    val focus = LocalFocusManager.current
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
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                keyboardActions = KeyboardActions(onSearch = { vm.search(); focus.clearFocus() }),
                trailingIcon = {
                    if (state.query.isNotEmpty()) {
                        IconButton(onClick = { vm.setQuery("") }) { Icon(Icons.Filled.Close, "Clear") }
                    }
                },
            )
            Spacer(Modifier.width(8.dp))
            IconButton(onClick = { vm.search(); focus.clearFocus() }) { Icon(Icons.Filled.Search, "Search") }
        }
        if (state.searching) {
            Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
        }
        if (state.results.isEmpty() && !state.searching) {
            if (state.query.isBlank()) {
                EmptyState(
                    icon = Icons.Filled.Search,
                    title = "Search for a song",
                    body = "Find songs across your connected services.",
                    modifier = Modifier.fillMaxSize(),
                )
            } else {
                EmptyState(
                    icon = Icons.Filled.Search,
                    title = "No results",
                    body = "Nothing matched “${state.query}”. Try a different search.",
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
        LazyColumn(Modifier.fillMaxSize()) {
            items(state.results) { t ->
                TrackRow(t, onPlay = { vm.play(t) },
                    isPlaying = t.id == state.playback.track?.id,
                    trailing = { AddToPlaylistButton(vm, state, t) })
            }
        }
    }
}

/** A track list row: art, title, artist·album, and an optional trailing action. */
@Composable
private fun TrackRow(
    t: Track,
    onPlay: () -> Unit,
    isPlaying: Boolean = false,
    trailing: @Composable (() -> Unit)? = null,
) {
    Row(Modifier.fillMaxWidth().clickable { onPlay() }.padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        NetworkImage(t.artworkUrl, Modifier.size(48.dp).clip(RoundedCornerShape(6.dp)))
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(t.title, maxLines = 1, overflow = TextOverflow.Ellipsis,
                style = MaterialTheme.typography.bodyLarge,
                color = if (isPlaying) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface)
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
                DropdownMenuItem(text = { Text("No playlists yet") }, onClick = {}, enabled = false)
            }
            state.playlists.forEach { pl ->
                DropdownMenuItem(text = { Text("${pl.title} · ${serviceLabel(pl.service)}") },
                    onClick = { vm.addToPlaylist(track, pl); expanded = false })
            }
        }
    }
}

@Composable
private fun NowPlayingScreen(vm: HarmonyViewModel, state: UiState) {
    val pb = state.playback
    val onDevice = state.target != "phone"

    // Nothing to show and not streaming hub audio: an empty state, not a dead transport.
    if (pb.track == null && !state.playingHere) {
        Column(
            Modifier.fillMaxSize().padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            EmptyState(
                icon = Icons.Filled.MusicNote,
                title = "Nothing playing",
                body = "Pick a song from Search or Library to start.",
            )
            Spacer(Modifier.height(8.dp))
            OutputSelector(vm, state)
        }
        return
    }

    // When playingHere with no track, we're streaming the hub's live audio.
    val title = pb.track?.title ?: "Hub audio"
    val subtitle = pb.track?.artist?.ifBlank { null } ?: state.instanceName ?: ""

    Column(
        Modifier.fillMaxSize().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Surface(
            shape = RoundedCornerShape(14.dp),
            shadowElevation = 8.dp,
            modifier = Modifier.padding(bottom = 24.dp),
        ) {
            NetworkImage(pb.track?.artworkUrl, Modifier.size(240.dp).clip(RoundedCornerShape(14.dp)))
        }
        Text(title, style = MaterialTheme.typography.titleLarge,
            maxLines = 1, overflow = TextOverflow.Ellipsis)
        Text(subtitle, style = MaterialTheme.typography.bodyMedium,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(24.dp))

        val dur = pb.durationMs.coerceAtLeast(0)
        val pos = pb.positionMs.coerceIn(0, if (dur > 0) dur else pb.positionMs)
        // While dragging, show the drag position; only commit on release so the
        // slider doesn't fight the 500ms progress ticker.
        var dragPos by remember { mutableStateOf<Float?>(null) }
        if (onDevice) {
            // The local slider only reflects this phone's player; when casting, the
            // device owns transport.
            Text("Playback controls are on the device.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else if (pb.track != null && dur > 0) {
            val livePos = pos.toFloat() / dur
            Slider(
                value = dragPos ?: livePos,
                onValueChange = { dragPos = it },
                onValueChangeFinished = {
                    dragPos?.let { vm.seekTo((it * dur).toLong()) }
                    dragPos = null
                },
            )
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(formatMs((dragPos?.let { (it * dur).toLong() }) ?: pos),
                    style = MaterialTheme.typography.labelSmall)
                Text(formatMs(dur), style = MaterialTheme.typography.labelSmall)
            }
        }
        Spacer(Modifier.height(16.dp))
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
    val h = totalSec / 3600
    val m = (totalSec % 3600) / 60
    val s = totalSec % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
}
