package io.github.marthofdoom.harmony

import android.graphics.BitmapFactory
import android.util.LruCache
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/** Tiny async image loader for album art — a shared OkHttp client + an in-memory
 *  bitmap cache, so we don't pull in a whole image library. Artwork URLs are
 *  public (googleusercontent / qobuz CDNs), so no personal key is sent. */
private val imageClient by lazy {
    OkHttpClient.Builder().connectTimeout(6, TimeUnit.SECONDS).readTimeout(12, TimeUnit.SECONDS).build()
}
private val imageCache = object : LruCache<String, ImageBitmap>(64) {}

private suspend fun loadBitmap(url: String): ImageBitmap? {
    imageCache.get(url)?.let { return it }
    return withContext(Dispatchers.IO) {
        runCatching {
            imageClient.newCall(Request.Builder().url(url).build()).execute().use { resp ->
                if (!resp.isSuccessful) return@use null
                resp.body?.byteStream()?.let { BitmapFactory.decodeStream(it) }?.asImageBitmap()
            }
        }.getOrNull()?.also { imageCache.put(url, it) }
    }
}

/** Shows album art from [url], falling back to a music-note placeholder while
 *  loading or when there's no art. */
@Composable
fun NetworkImage(url: String?, modifier: Modifier = Modifier) {
    var bitmap by remember(url) { mutableStateOf(imageCache.get(url ?: "")) }
    LaunchedEffect(url) {
        if (bitmap == null && !url.isNullOrEmpty()) bitmap = loadBitmap(url)
    }
    Box(modifier.background(MaterialTheme.colorScheme.surfaceVariant), contentAlignment = Alignment.Center) {
        val bmp = bitmap
        if (bmp != null) {
            Image(bmp, contentDescription = null, modifier = Modifier.fillMaxSize(), contentScale = ContentScale.Crop)
        } else {
            Icon(Icons.Filled.MusicNote, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}
