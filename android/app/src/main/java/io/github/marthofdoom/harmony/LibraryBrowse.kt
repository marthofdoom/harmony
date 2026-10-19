package io.github.marthofdoom.harmony

import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Sort
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.LibraryMusic
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

// ── Small shared pieces ───────────────────────────────────────────────────

/** The "Hi-Res" chip; renders nothing for anything below hi-res. */
@Composable
fun HiResChip(q: Quality?, modifier: Modifier = Modifier) {
    if (q == null || !q.isHiRes) return
    Surface(shape = RoundedCornerShape(4.dp), color = MaterialTheme.colorScheme.primaryContainer,
        contentColor = MaterialTheme.colorScheme.onPrimaryContainer, modifier = modifier) {
        Text("Hi-Res", style = MaterialTheme.typography.labelSmall,
            modifier = Modifier.padding(horizontal = 5.dp, vertical = 1.dp), maxLines = 1)
    }
}

/** Segment row: Artists | Albums | Songs | Genres | Playlists. */
@Composable
private fun LibrarySegments(current: LibSection, onSelect: (LibSection) -> Unit) {
    Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())
            .padding(horizontal = 16.dp, vertical = 4.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        LibSection.entries.forEach { sec ->
            FilterChip(selected = sec == current, onClick = { onSelect(sec) }, label = { Text(sec.label) })
        }
    }
}

/** "Sort: Title" button with a menu of orders + an ascending/descending toggle. */
@Composable
private fun SortMenu(options: List<Pair<String, String>>, order: String, desc: Boolean,
                     onChange: (String, Boolean) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    val label = options.firstOrNull { it.first == order }?.second ?: order
    Box {
        TextButton(onClick = { expanded = true }) {
            Icon(Icons.AutoMirrored.Filled.Sort, null); Spacer(Modifier.width(6.dp))
            Text("Sort: $label ${if (desc) "↓" else "↑"}")
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            options.forEach { (value, name) ->
                DropdownMenuItem(
                    text = { Text(name) },
                    leadingIcon = { if (value == order) Icon(Icons.Filled.Check, null) else Spacer(Modifier.size(24.dp)) },
                    onClick = { onChange(value, desc); expanded = false })
            }
            HorizontalDivider()
            DropdownMenuItem(
                text = { Text(if (desc) "Ascending" else "Descending") },
                onClick = { onChange(order, !desc); expanded = false })
        }
    }
}

private val ALBUM_SORTS = listOf("title" to "Title", "artist" to "Artist", "year" to "Year",
    "recent" to "Recently added")
private val SONG_SORTS = listOf("title" to "Title", "artist" to "Artist", "album" to "Album",
    "year" to "Year", "genre" to "Genre", "duration" to "Duration", "quality" to "Quality")

@Composable
private fun AlbumCard(vm: HarmonyViewModel, al: Album) {
    Column(Modifier.fillMaxWidth().clickable(enabled = al.id != null) { al.id?.let { vm.openAlbum(al.service, it) } }
        .padding(4.dp)) {
        Box {
            NetworkImage(al.artworkUrl, Modifier.fillMaxWidth().aspectRatio(1f).clip(RoundedCornerShape(8.dp)))
            HiResChip(al.quality, Modifier.align(Alignment.TopEnd).padding(6.dp))
        }
        Text(al.title, maxLines = 1, overflow = TextOverflow.Ellipsis,
            style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(top = 6.dp))
        Text(listOfNotNull(al.artist.ifBlank { null }, (al.year ?: al.date?.take(4)?.toIntOrNull())?.toString())
            .joinToString(" · "),
            maxLines = 1, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

private fun countsLabel(albums: Int, tracks: Int): String =
    listOfNotNull(if (albums > 0) (if (albums == 1) "1 album" else "$albums albums") else null,
        if (tracks > 0) (if (tracks == 1) "1 song" else "$tracks songs") else null).joinToString(" · ")

// ── The browser ───────────────────────────────────────────────────────────

/** Segment row + the content of the chosen non-playlist segment (or the artist /
 *  genre screen when one is open). Playlists stay in LibraryScreen. */
@Composable
fun LibrarySegmentsHeader(vm: HarmonyViewModel, state: UiState) {
    LibrarySegments(state.lib.section) { vm.setLibSection(it) }
}

@Composable
fun LibraryBrowseContent(vm: HarmonyViewModel, state: UiState) {
    val lib = state.lib
    val focus = lib.focus
    if (focus != null) { FocusContent(vm, state, focus); return }
    if (lib.enabled == false) {
        EmptyState(
            icon = Icons.Filled.LibraryMusic,
            title = "No library yet",
            body = "Point a Harmony server (or a peer on your network) at a music folder " +
                "and it will show up here.",
            modifier = Modifier.fillMaxSize())
        return
    }
    if (lib.enabled == null || (lib.loading && lib.songs.isEmpty() && lib.artists.isEmpty())) {
        Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.TopCenter) {
            CircularProgressIndicator()
        }
        return
    }
    Column(Modifier.fillMaxSize()) {
    if (lib.outdatedPeers.isNotEmpty()) Text(
        "Some libraries need updating: ${lib.outdatedPeers.joinToString(", ")}",
        Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
        style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f))
    when (lib.section) {
        LibSection.ARTISTS -> ArtistsList(vm, lib)
        LibSection.ALBUMS -> AlbumsGrid(vm, lib)
        LibSection.SONGS -> SongsList(vm, state)
        LibSection.GENRES -> GenresGrid(vm, lib)
        LibSection.PLAYLISTS -> {}
    }
    }
}

@Composable
private fun ArtistsList(vm: HarmonyViewModel, lib: LibraryBrowse) {
    if (lib.artists.isEmpty()) {
        EmptyState(Icons.Filled.LibraryMusic, "No artists", "Nothing has been scanned yet.",
            Modifier.fillMaxSize()); return
    }
    LazyColumn(Modifier.fillMaxSize()) {
        itemsIndexed(lib.artists, key = { i, a -> "$i-${a.id}" }) { _, a ->
            Row(Modifier.fillMaxWidth().clickable { vm.openLibArtist(a) }
                .padding(horizontal = 16.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                NetworkImage(a.artworkUrl, Modifier.size(48.dp).clip(CircleShape))
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    Text(a.name, maxLines = 1, overflow = TextOverflow.Ellipsis,
                        style = MaterialTheme.typography.bodyLarge)
                    Text(countsLabel(a.albumCount, a.trackCount), style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}

@Composable
private fun AlbumsGrid(vm: HarmonyViewModel, lib: LibraryBrowse) {
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            SortMenu(ALBUM_SORTS, lib.albumOrder, lib.albumDesc) { o, d -> vm.setAlbumSort(o, d) }
            Text("${lib.albums.size} albums", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (lib.albums.isEmpty()) {
            EmptyState(Icons.Filled.LibraryMusic, "No albums", "Nothing has been scanned yet.",
                Modifier.fillMaxSize()); return
        }
        LazyVerticalGrid(GridCells.Fixed(2), Modifier.fillMaxSize(),
            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)) {
            items(lib.albums, key = { "${it.service}-${it.id}-${it.title}" }) { AlbumCard(vm, it) }
        }
    }
}

@Composable
private fun GenresGrid(vm: HarmonyViewModel, lib: LibraryBrowse) {
    if (lib.genres.isEmpty()) {
        EmptyState(Icons.Filled.LibraryMusic, "No genres", "Tag your files with a genre to see them here.",
            Modifier.fillMaxSize()); return
    }
    LazyVerticalGrid(GridCells.Fixed(2), Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)) {
        items(lib.genres, key = { it.name }) { g ->
            Column(Modifier.fillMaxWidth().clickable { vm.openLibGenre(g) }.padding(4.dp)) {
                NetworkImage(g.artworkUrl, Modifier.fillMaxWidth().aspectRatio(1f).clip(RoundedCornerShape(8.dp)))
                Text(g.name, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(top = 6.dp))
                Text(countsLabel(g.albumCount, g.trackCount), style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun SongsList(vm: HarmonyViewModel, state: UiState) {
    val lib = state.lib
    val songsCtx = PlayContext("songs", "Songs")
    val listState = rememberLazyListState()
    // Page in the next 200 as the end of the loaded list comes into view.
    val lastVisible by remember { derivedStateOf { listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0 } }
    LaunchedEffect(lastVisible, lib.songs.size, lib.songsTotal) {
        if (lib.songs.isNotEmpty() && lastVisible >= lib.songs.size - 20) vm.loadMoreSongs()
    }
    // A new sort starts at the top.
    LaunchedEffect(lib.songOrder, lib.songDesc) { listState.scrollToItem(0) }
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            SortMenu(SONG_SORTS, lib.songOrder, lib.songDesc) { o, d -> vm.setSongSort(o, d) }
            Text(if (lib.songsTotal == 1) "1 song" else "${lib.songsTotal} songs",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (lib.songs.isEmpty()) {
            EmptyState(Icons.Filled.LibraryMusic, "No songs", "Nothing has been scanned yet.",
                Modifier.fillMaxSize()); return
        }
        LazyColumn(Modifier.fillMaxSize(), state = listState) {
            itemsIndexed(lib.songs, key = { i, t -> "$i-${t.service}-${t.id}" }) { i, t ->
                TrackRow(t, onPlay = { vm.playFrom(lib.songs, i, songsCtx) },
                    playState = state.rowPlay(t),
                    trailing = { TrackRowMenu(vm, state, t) })
            }
            if (lib.songsLoadingMore) item {
                Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(Modifier.size(24.dp), strokeWidth = 2.dp)
                }
            }
        }
    }
}

/** An artist (albums grid + songs) or a genre (albums grid) opened in the Library. */
@Composable
private fun FocusContent(vm: HarmonyViewModel, state: UiState, f: LibFocus) {
    if (f.loading && f.albums.isEmpty() && f.songs.isEmpty()) {
        Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.TopCenter) {
            CircularProgressIndicator()
        }
        return
    }
    val ctx = if (f.kind == "artist") PlayContext("artist", f.title, "local", f.id)
              else PlayContext("genre", f.title)
    LazyVerticalGrid(GridCells.Fixed(2), Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)) {
        if (f.songs.isNotEmpty()) item(span = { GridItemSpan(2) }) {
            ListActionsRow(
                onPlay = { vm.playAll(f.songs, shuffle = false, context = ctx) },
                onShuffle = { vm.playAll(f.songs, shuffle = true, context = ctx) },
                onPlayNext = { vm.playNext(f.songs) },
                onAddToQueue = { vm.addToQueue(f.songs) })
        }
        if (f.albums.isNotEmpty()) {
            item(span = { GridItemSpan(2) }) {
                Text("Albums", style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(start = 4.dp, top = 12.dp, bottom = 4.dp))
            }
            items(f.albums, key = { "a-${it.service}-${it.id}-${it.title}" }) { AlbumCard(vm, it) }
        }
        if (f.songs.isNotEmpty()) {
            item(span = { GridItemSpan(2) }) {
                Text("Songs", style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(start = 4.dp, top = 12.dp, bottom = 4.dp))
            }
            itemsIndexedSpan(f.songs) { i, t ->
                Box(Modifier.padding(horizontal = 0.dp)) {
                    TrackRow(t, onPlay = { vm.playFrom(f.songs, i, ctx) },
                        playState = state.rowPlay(t),
                        trailing = { TrackRowMenu(vm, state, t) })
                }
            }
        }
        if (f.albums.isEmpty() && f.songs.isEmpty()) item(span = { GridItemSpan(2) }) {
            EmptyState(Icons.Filled.LibraryMusic, "Nothing here", "No albums found.", Modifier.fillMaxWidth())
        }
    }
}

/** Full-width indexed rows inside a 2-column grid. */
private fun <T> androidx.compose.foundation.lazy.grid.LazyGridScope.itemsIndexedSpan(
    list: List<T>, content: @Composable (Int, T) -> Unit,
) {
    items(list.size, span = { GridItemSpan(2) }, key = { "s-$it" }) { i -> content(i, list[i]) }
}
