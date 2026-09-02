package io.github.marthofdoom.harmony

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Album
import androidx.compose.material.icons.filled.ErrorOutline
import androidx.compose.material.icons.automirrored.filled.OpenInNew
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.AssistChip
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Constraints
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlin.math.max

// ── Entity navigation host ────────────────────────────────────────────────

/** Renders the top of the entity back stack. System Back pops one level. */
@Composable
fun DetailHost(vm: HarmonyViewModel, state: UiState) {
    val entry = state.detailStack.lastOrNull() ?: return
    androidx.activity.compose.BackHandler(enabled = true) { vm.popDetail() }
    when (entry.kind) {
        DetailKind.ARTIST -> ArtistScreen(vm, state, entry)
        DetailKind.ALBUM -> AlbumScreen(vm, state, entry)
        DetailKind.TRACK -> TrackScreen(vm, entry)
    }
}

// ── Shared pieces ─────────────────────────────────────────────────────────

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DetailBar(title: String, onBack: () -> Unit) {
    TopAppBar(
        title = { Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis) },
        navigationIcon = {
            IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
        },
    )
}

@Composable
private fun Loading() {
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        CircularProgressIndicator()
    }
}

@Composable
private fun SectionHeader(text: String) {
    Text(text, style = MaterialTheme.typography.titleSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(start = 16.dp, end = 16.dp, top = 16.dp, bottom = 4.dp))
}

/** A human label for an artist kind. */
private fun kindLabel(kind: String): String = when (kind) {
    "group" -> "Group"
    "person" -> "Person"
    else -> "Artist"
}

/** Year label from an Album's year/date (year wins; date is a fallback). */
private fun albumYear(album: Album): String? =
    album.year?.toString() ?: album.date?.take(4)?.ifBlank { null }

@Composable
private fun AlbumRow(album: Album, onClick: (() -> Unit)?) {
    val base = Modifier.fillMaxWidth()
    val clickable = if (onClick != null) base.clickable { onClick() } else base
    Row(clickable.padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        NetworkImage(album.artworkUrl, Modifier.size(48.dp).clip(RoundedCornerShape(6.dp)))
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(album.title, maxLines = 1, overflow = TextOverflow.Ellipsis,
                style = MaterialTheme.typography.bodyLarge)
            Text(
                listOfNotNull(albumYear(album), album.artist.ifBlank { null }).joinToString(" · "),
                maxLines = 1, overflow = TextOverflow.Ellipsis,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun ArtistRefRow(ref: ArtistRef, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().clickable { onClick() }
        .padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Icon(Icons.Filled.Person, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.width(12.dp))
        Text(ref.name, style = MaterialTheme.typography.bodyLarge, modifier = Modifier.weight(1f),
            maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

/** Formats [start, end?] spans as "1968–1971, 1975–present". */
private fun spansLabel(spans: List<Pair<Int, Int?>>): String? {
    if (spans.isEmpty()) return null
    return spans.joinToString(", ") { (s, e) -> "$s–${e?.toString() ?: "present"}" }
}

// ── Artist ────────────────────────────────────────────────────────────────

@Composable
private fun ArtistScreen(vm: HarmonyViewModel, state: UiState, entry: DetailEntry) {
    val d = entry.artist
    Column(Modifier.fillMaxSize()) {
        DetailBar(d?.artist?.name ?: "Artist", onBack = { vm.popDetail() })
        when {
            entry.loading && d == null -> Loading()
            entry.error != null && d == null -> EmptyState(
                icon = Icons.Filled.ErrorOutline, title = "Couldn't load artist",
                body = entry.error, modifier = Modifier.fillMaxSize())
            d == null -> Loading()
            else -> LazyColumn(Modifier.fillMaxSize()) {
                item { ArtistHeader(d.artist, d.kind) }
                d.artist.bio?.let { bio -> item { BioBlock(bio) } }

                // A dated group gets the Wikipedia-style member timeline.
                val chrono = d.chronology
                if (d.kind == "group" && chrono != null && chrono.members.isNotEmpty()) {
                    item { SectionHeader("Timeline") }
                    item { ChronologyChart(chrono) }
                }

                if (d.albums.isNotEmpty()) {
                    item { SectionHeader(if (d.kind == "person") "Performed on" else "Albums") }
                    items(d.albums) { al ->
                        // Person rows with a null id are informational (not navigable).
                        AlbumRow(al, onClick = al.id?.let { id -> { vm.openAlbum(al.service, id) } })
                    }
                }
                if (d.singles.isNotEmpty()) {
                    item { SectionHeader("Singles") }
                    items(d.singles) { al ->
                        AlbumRow(al, onClick = al.id?.let { id -> { vm.openAlbum(al.service, id) } })
                    }
                }
                if (d.topTracks.isNotEmpty()) {
                    item { SectionHeader("Top tracks") }
                    items(d.topTracks) { t ->
                        TrackRow(t, onPlay = { vm.play(t) },
                            isPlaying = t.id == state.playback.track?.id,
                            trailing = { AddToPlaylistButton(vm, state, t) })
                    }
                }
                if (d.members.isNotEmpty()) {
                    item { SectionHeader("Members") }
                    items(d.members) { m -> MemberRow(m) { vm.searchName(m.name) } }
                }
                if (d.memberOf.isNotEmpty()) {
                    item { SectionHeader("Member of") }
                    items(d.memberOf) { b ->
                        BandRow(b, onClick = {
                            val ref = b.ref
                            if (ref != null) vm.openArtist(ref) else vm.searchName(b.name)
                        })
                    }
                }
                item { Spacer(Modifier.height(24.dp)) }
            }
        }
    }
}

@Composable
private fun ArtistHeader(artist: ArtistInfo, kind: String) {
    Row(Modifier.fillMaxWidth().padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
        NetworkImage(artist.imageUrl, Modifier.size(96.dp).clip(RoundedCornerShape(48.dp)))
        Spacer(Modifier.width(16.dp))
        Column(Modifier.weight(1f)) {
            Text(artist.name, style = MaterialTheme.typography.headlineSmall,
                maxLines = 2, overflow = TextOverflow.Ellipsis)
            Spacer(Modifier.height(4.dp))
            Text(kindLabel(kind), style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.primary)
        }
    }
}

@Composable
private fun BioBlock(bio: Bio) {
    Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp)) {
        Text(bio.text, style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (bio.source != null) {
            Spacer(Modifier.height(2.dp))
            Text("Source: ${bio.source.replaceFirstChar { it.uppercase() }}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun MemberRow(member: Member, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().clickable { onClick() }.heightIn(min = 56.dp)
        .padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Icon(Icons.Filled.Person, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(member.name, style = MaterialTheme.typography.bodyLarge,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
            val sub = listOfNotNull(
                member.instruments.joinToString(", ").ifBlank { null },
                spansLabel(member.spans),
            ).joinToString(" · ")
            if (sub.isNotBlank()) {
                Text(sub, style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
        if (member.isCurrent) {
            AssistChip(onClick = onClick, label = { Text("Current") })
        }
    }
}

@Composable
private fun BandRow(band: Band, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().clickable { onClick() }.heightIn(min = 56.dp)
        .padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Icon(Icons.Filled.Person, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(band.name, style = MaterialTheme.typography.bodyLarge,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
            spansLabel(band.spans)?.let {
                Text(it, style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

// ── Album ─────────────────────────────────────────────────────────────────

@Composable
private fun AlbumScreen(vm: HarmonyViewModel, state: UiState, entry: DetailEntry) {
    val d = entry.album
    Column(Modifier.fillMaxSize()) {
        DetailBar(d?.album?.title ?: "Album", onBack = { vm.popDetail() })
        when {
            entry.loading && d == null -> Loading()
            entry.error != null && d == null -> EmptyState(
                icon = Icons.Filled.ErrorOutline, title = "Couldn't load album",
                body = entry.error, modifier = Modifier.fillMaxSize())
            d == null -> Loading()
            else -> LazyColumn(Modifier.fillMaxSize()) {
                item { AlbumHeader(d, onArtist = { d.artistRef?.let { vm.openArtist(it) } }) }
                d.bio?.let { bio -> item { BioBlock(bio) } }
                item { SectionHeader("Tracks") }
                items(d.tracks) { t ->
                    NumberedTrackRow(
                        t,
                        isPlaying = t.id == state.playback.track?.id,
                        onOpen = { vm.openTrack(t.service, t.id) },
                        onPlay = { vm.play(t) },
                    )
                }
                item { Spacer(Modifier.height(24.dp)) }
            }
        }
    }
}

@Composable
private fun AlbumHeader(d: AlbumDetail, onArtist: () -> Unit) {
    val album = d.album
    Column(Modifier.fillMaxWidth().padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Surface(shape = RoundedCornerShape(12.dp), shadowElevation = 6.dp) {
            NetworkImage(album.artworkUrl, Modifier.size(200.dp).clip(RoundedCornerShape(12.dp)))
        }
        Spacer(Modifier.height(16.dp))
        Text(album.title, style = MaterialTheme.typography.titleLarge, textAlign = TextAlign.Center,
            maxLines = 3, overflow = TextOverflow.Ellipsis)
        Spacer(Modifier.height(4.dp))
        val artistName = d.artistRef?.name ?: album.artist
        if (artistName.isNotBlank()) {
            val artistMod = if (d.artistRef != null) Modifier.clickable { onArtist() } else Modifier
            Text(artistName, style = MaterialTheme.typography.bodyLarge, modifier = artistMod,
                color = if (d.artistRef != null) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface)
        }
        val meta = listOfNotNull(
            album.date ?: album.year?.toString(),
            trackCountLabel(album.trackCount ?: d.tracks.size.takeIf { it > 0 }),
        ).joinToString(" · ")
        if (meta.isNotBlank()) {
            Spacer(Modifier.height(2.dp))
            Text(meta, style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun NumberedTrackRow(t: Track, isPlaying: Boolean, onOpen: () -> Unit, onPlay: () -> Unit) {
    Row(Modifier.fillMaxWidth().clickable { onOpen() }
        .padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(32.dp), contentAlignment = Alignment.Center) {
            Text(t.trackNumber?.toString() ?: "–", style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.width(8.dp))
        Column(Modifier.weight(1f)) {
            Text(t.title, maxLines = 1, overflow = TextOverflow.Ellipsis,
                style = MaterialTheme.typography.bodyLarge,
                color = if (isPlaying) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface)
            if (t.artist.isNotBlank()) {
                Text(t.artist, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        IconButton(onClick = onPlay) { Icon(Icons.Filled.PlayArrow, "Play") }
    }
}

// ── Track ─────────────────────────────────────────────────────────────────

@Composable
private fun TrackScreen(vm: HarmonyViewModel, entry: DetailEntry) {
    val d = entry.track
    Column(Modifier.fillMaxSize()) {
        DetailBar(d?.track?.title ?: "Track", onBack = { vm.popDetail() })
        when {
            entry.loading && d == null -> Loading()
            entry.error != null && d == null -> EmptyState(
                icon = Icons.Filled.ErrorOutline, title = "Couldn't load track",
                body = entry.error, modifier = Modifier.fillMaxSize())
            d == null -> Loading()
            else -> LazyColumn(Modifier.fillMaxSize()) {
                item { TrackHeader(d.track, onPlay = { vm.play(d.track) }) }
                d.albumRef?.let { ref ->
                    item { SectionHeader("Album") }
                    item {
                        Row(Modifier.fillMaxWidth().clickable { vm.openAlbum(ref) }
                            .padding(horizontal = 16.dp, vertical = 10.dp),
                            verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Filled.Album, null,
                                tint = MaterialTheme.colorScheme.onSurfaceVariant)
                            Spacer(Modifier.width(12.dp))
                            Text(ref.title, style = MaterialTheme.typography.bodyLarge,
                                maxLines = 1, overflow = TextOverflow.Ellipsis)
                        }
                    }
                }
                if (d.artistRefs.isNotEmpty()) {
                    item { SectionHeader("Artists") }
                    items(d.artistRefs) { ref -> ArtistRefRow(ref) { vm.openArtist(ref) } }
                }
                item { SectionHeader("Performers") }
                if (d.performers.isNotEmpty()) {
                    items(d.performers) { p -> PerformerRow(p) }
                } else {
                    // Credited artists as a fallback, plus an honest note — never blank.
                    if (d.artistRefs.isEmpty() && d.track.artist.isNotBlank()) {
                        item {
                            Text(d.track.artist, style = MaterialTheme.typography.bodyLarge,
                                modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp))
                        }
                    }
                    item {
                        Text(
                            "Detailed performer credits aren't in MusicBrainz for this recording.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp))
                    }
                }
                item { Spacer(Modifier.height(24.dp)) }
            }
        }
    }
}

@Composable
private fun TrackHeader(track: Track, onPlay: () -> Unit) {
    Column(Modifier.fillMaxWidth().padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Surface(shape = RoundedCornerShape(12.dp), shadowElevation = 6.dp) {
            NetworkImage(track.artworkUrl, Modifier.size(200.dp).clip(RoundedCornerShape(12.dp)))
        }
        Spacer(Modifier.height(16.dp))
        Text(track.title, style = MaterialTheme.typography.titleLarge, textAlign = TextAlign.Center,
            maxLines = 3, overflow = TextOverflow.Ellipsis)
        val sub = listOfNotNull(
            track.artist.ifBlank { null }, track.album,
            track.year?.toString(),
        ).joinToString(" · ")
        if (sub.isNotBlank()) {
            Spacer(Modifier.height(4.dp))
            Text(sub, style = MaterialTheme.typography.bodyMedium, textAlign = TextAlign.Center,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        track.isrc?.let {
            Spacer(Modifier.height(2.dp))
            Text("ISRC $it", style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(16.dp))
        androidx.compose.material3.Button(onClick = onPlay) {
            Icon(Icons.Filled.PlayArrow, null); Spacer(Modifier.width(8.dp)); Text("Play")
        }
    }
}

@Composable
private fun PerformerRow(p: Performer) {
    Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Icon(Icons.Filled.Person, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(p.name, style = MaterialTheme.typography.bodyLarge,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
            if (p.roles.isNotEmpty()) {
                Text(p.roles.joinToString(", "), style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

// ── Smart-search results ──────────────────────────────────────────────────

/** Renders smart search top-to-bottom per the spec: artist discography (if any)
 *  → album matches → incidental (tracks/artists/playlists). */
@Composable
fun SmartResults(vm: HarmonyViewModel, state: UiState, smart: SmartSearch) {
    val artist = smart.artist
    val inc = smart.incidental
    val empty = artist == null && smart.albums.isEmpty() &&
        inc.tracks.isEmpty() && inc.artists.isEmpty() && inc.playlists.isEmpty()
    if (empty) {
        EmptyState(
            icon = Icons.Filled.Person, title = "No results",
            body = "Nothing matched “${smart.query}”. Try a different search.",
            modifier = Modifier.fillMaxSize())
        return
    }
    LazyColumn(Modifier.fillMaxSize()) {
        if (artist != null) {
            item {
                Row(Modifier.fillMaxWidth().clickable { vm.openArtist(artist.ref) }
                    .padding(horizontal = 16.dp, vertical = 12.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Filled.Person, null, tint = MaterialTheme.colorScheme.primary)
                    Spacer(Modifier.width(12.dp))
                    Column(Modifier.weight(1f)) {
                        Text(artist.ref.name, style = MaterialTheme.typography.titleMedium,
                            maxLines = 1, overflow = TextOverflow.Ellipsis)
                        Text(kindLabel(artist.kind), style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Icon(Icons.AutoMirrored.Filled.OpenInNew, "Open artist",
                        tint = MaterialTheme.colorScheme.primary)
                }
            }
            if (artist.albums.isNotEmpty()) {
                item { SectionHeader("Discography") }
                items(artist.albums) { al ->
                    AlbumRow(al, onClick = al.id?.let { id -> { vm.openAlbum(al.service, id) } })
                }
            }
        }
        if (smart.albums.isNotEmpty()) {
            item { SectionHeader("Albums") }
            items(smart.albums) { al ->
                AlbumRow(al, onClick = al.id?.let { id -> { vm.openAlbum(al.service, id) } })
            }
        }
        if (inc.tracks.isNotEmpty()) {
            item { SectionHeader("Songs") }
            items(inc.tracks) { t ->
                TrackRow(t, onPlay = { vm.play(t) },
                    isPlaying = t.id == state.playback.track?.id,
                    trailing = { AddToPlaylistButton(vm, state, t) })
            }
        }
        if (inc.artists.isNotEmpty()) {
            item { SectionHeader("Artists") }
            items(inc.artists) { ref -> ArtistRefRow(ref) { vm.openArtist(ref) } }
        }
        if (inc.playlists.isNotEmpty()) {
            item { SectionHeader("Playlists") }
            items(inc.playlists) { pl ->
                Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    NetworkImage(pl.artworkUrl, Modifier.size(48.dp).clip(RoundedCornerShape(6.dp)))
                    Spacer(Modifier.width(12.dp))
                    Column(Modifier.weight(1f)) {
                        Text(pl.title, maxLines = 1, overflow = TextOverflow.Ellipsis,
                            style = MaterialTheme.typography.bodyLarge)
                        Text(listOfNotNull(serviceLabel(pl.service), trackCountLabel(pl.trackCount))
                            .joinToString(" · "),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
        item { Spacer(Modifier.height(24.dp)) }
    }
}

// ── Member-chronology chart (Compose Canvas) ──────────────────────────────

private fun niceStep(raw: Int): Int {
    val steps = intArrayOf(1, 2, 5, 10, 20, 25, 50, 100)
    for (s in steps) if (s >= raw) return s
    return raw
}

/**
 * A Wikipedia-style band timeline: a horizontal year axis, one lane per member
 * spanning their active years, and vertical album-year marker lines. Member
 * names sit in a fixed left gutter; the dated timeline scrolls horizontally.
 * All colors come from the theme so it reads in light and dark.
 */
@Composable
fun ChronologyChart(chrono: Chronology) {
    val members = chrono.members
    if (members.isEmpty()) return
    val start = chrono.startYear
    val end = max(chrono.endYear, start + 1)
    val span = end - start
    val cs = MaterialTheme.colorScheme
    val measurer = rememberTextMeasurer()

    // Lane colors cycle through theme roles so adjacent members stay distinct.
    val laneColors = listOf(cs.primary, cs.tertiary, cs.secondary)

    val laneHDp = 34.dp
    val topPadDp = 96.dp       // header band for rotated album titles
    val bottomPadDp = 26.dp    // year-axis labels
    val pxPerYearDp = 26.dp
    val leftPadDp = 10.dp
    val gutterW = 132.dp

    val chartWDp = leftPadDp * 2 + pxPerYearDp * span.toFloat()
    val totalHDp = topPadDp + laneHDp * members.size.toFloat() + bottomPadDp

    val titleStyle = TextStyle(color = cs.onSurface, fontSize = 11.sp)
    val yearStyle = TextStyle(color = cs.onSurfaceVariant, fontSize = 10.sp)

    Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) {
        // Left gutter: member labels, vertically aligned with the lanes.
        Column(Modifier.width(gutterW)) {
            Spacer(Modifier.height(topPadDp))
            members.forEach { m ->
                Column(Modifier.height(laneHDp), verticalArrangement = Arrangement.Center) {
                    Text(m.name, style = MaterialTheme.typography.labelMedium,
                        maxLines = 1, overflow = TextOverflow.Ellipsis)
                    m.instruments.firstOrNull()?.let {
                        Text(it, style = MaterialTheme.typography.labelSmall,
                            color = cs.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                }
            }
        }
        Spacer(Modifier.width(8.dp))
        // Timeline: scrolls horizontally when the year range is wide.
        Box(Modifier.horizontalScroll(rememberScrollState())) {
            Canvas(Modifier.width(chartWDp).height(totalHDp)) {
                val laneH = laneHDp.toPx()
                val topPad = topPadDp.toPx()
                val leftPad = leftPadDp.toPx()
                val pxPerYear = pxPerYearDp.toPx()
                val axisY = topPad
                val lanesBottom = axisY + members.size * laneH
                fun xOf(year: Int): Float = leftPad + (year - start).coerceIn(0, span) * pxPerYear

                // Album marker lines (behind the bars) + rotated titles above the axis.
                for (album in chrono.albums) {
                    if (album.year < start || album.year > end) continue
                    val x = xOf(album.year)
                    drawLine(
                        color = cs.outline.copy(alpha = 0.5f),
                        start = Offset(x, axisY), end = Offset(x, lanesBottom),
                        strokeWidth = 1f)
                    drawCircle(cs.outline, radius = 2.5f, center = Offset(x, axisY))
                    if (album.title.isNotBlank()) {
                        val maxW = (topPad - 14.dp.toPx()).toInt().coerceAtLeast(20)
                        val layout = measurer.measure(
                            AnnotatedString(album.title), style = titleStyle,
                            overflow = TextOverflow.Ellipsis, maxLines = 1,
                            constraints = Constraints(maxWidth = maxW))
                        val h = layout.size.height.toFloat()
                        val anchor = Offset(x - h / 2f, axisY - 3.dp.toPx())
                        rotate(degrees = -90f, pivot = anchor) {
                            drawText(layout, topLeft = anchor)
                        }
                    }
                }

                // Member lanes: one rounded bar per active span.
                members.forEachIndexed { i, m ->
                    val color = laneColors[i % laneColors.size]
                    val top = axisY + i * laneH + 6.dp.toPx()
                    val barH = laneH - 12.dp.toPx()
                    for ((s, e) in m.spans) {
                        val x1 = xOf(s.coerceIn(start, end))
                        val x2 = xOf((e ?: end).coerceIn(start, end))
                        val w = max(x2 - x1, 3.dp.toPx())
                        drawRoundRect(
                            color = color,
                            topLeft = Offset(x1, top),
                            size = Size(w, barH),
                            cornerRadius = CornerRadius(barH / 2f, barH / 2f))
                    }
                }

                // Year axis: baseline + tick labels along the bottom.
                drawLine(cs.outlineVariant, Offset(leftPad, lanesBottom),
                    Offset(xOf(end), lanesBottom), strokeWidth = 1.5f)
                val step = niceStep(max(1, (span / 8.0).let { if (it < 1) 1 else Math.ceil(it).toInt() }))
                var year = start
                while (year <= end) {
                    val x = xOf(year)
                    drawLine(cs.outlineVariant, Offset(x, lanesBottom),
                        Offset(x, lanesBottom + 4.dp.toPx()), strokeWidth = 1.5f)
                    val label = measurer.measure(AnnotatedString(year.toString()), style = yearStyle)
                    drawText(label, topLeft = Offset(x - label.size.width / 2f, lanesBottom + 6.dp.toPx()))
                    year += step
                }
            }
        }
    }
}
