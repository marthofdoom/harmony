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
import androidx.compose.material.icons.filled.Cloud
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Speaker
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
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
            MaterialTheme {
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
                NavigationBarItem(
                    selected = tab == 0, onClick = { tab = 0 },
                    icon = { Icon(Icons.Filled.Search, null) }, label = { Text("Search") },
                )
                NavigationBarItem(
                    selected = tab == 1, onClick = { tab = 1 },
                    icon = { Icon(Icons.Filled.Speaker, null) }, label = { Text("Route") },
                )
                NavigationBarItem(
                    selected = tab == 2, onClick = { tab = 2 },
                    icon = { Icon(Icons.Filled.MusicNote, null) }, label = { Text("Now Playing") },
                )
            }
        }
    ) { pad ->
        Box(Modifier.padding(pad)) {
            when (tab) {
                0 -> SearchScreen(vm, state)
                1 -> RouteScreen(vm, state)
                else -> NowPlayingScreen(vm, state)
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
                Row(
                    Modifier.fillMaxWidth().clickable { vm.play(t) }.padding(horizontal = 16.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    NetworkImage(t.artworkUrl, Modifier.size(48.dp).clip(RoundedCornerShape(6.dp)))
                    Spacer(Modifier.width(12.dp))
                    Column(Modifier.weight(1f)) {
                        Text(t.title, maxLines = 1, overflow = TextOverflow.Ellipsis,
                            style = MaterialTheme.typography.bodyLarge)
                        Text(listOfNotNull(t.artist.ifBlank { null }, t.album).joinToString(" · "),
                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Text(t.service.uppercase().take(3), style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
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
        IconButton(onClick = { vm.togglePlayPause() }, enabled = pb.track != null,
            modifier = Modifier.size(72.dp)) {
            Icon(
                if (pb.isPlaying) Icons.Filled.Pause else Icons.Filled.PlayArrow,
                contentDescription = if (pb.isPlaying) "Pause" else "Play",
                modifier = Modifier.size(48.dp),
            )
        }
    }
}

private fun formatMs(ms: Long): String {
    val totalSec = ms / 1000
    return "%d:%02d".format(totalSec / 60, totalSec % 60)
}
