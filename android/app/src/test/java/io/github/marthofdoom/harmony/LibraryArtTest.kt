package io.github.marthofdoom.harmony

import org.junit.Assert.assertEquals
import org.junit.Test

/** Library artwork is served by the instance as a signed path ("/art/<album>.<sig>").
 *  The app makes it absolute to show it, and hands it back relative when it loads a
 *  device queue, so the instance next to the speaker re-addresses it for that device. */
class LibraryArtTest {
    @Test fun libraryArtGoesBackAsAServerPath() {
        assertEquals("/art/abc123.def456", HarmonyApi.serverRelative("http://10.0.0.2:8080/art/abc123.def456"))
        assertEquals("/art/abc.def", HarmonyApi.serverRelative("http://[fd7a::1]:8080/art/abc.def"))
    }

    @Test fun remoteArtIsUntouched() {
        val yt = "https://lh3.googleusercontent.com/abc=w544-h544"
        assertEquals(yt, HarmonyApi.serverRelative(yt))
        assertEquals("https://static.qobuz.com/images/covers/x_600.jpg",
            HarmonyApi.serverRelative("https://static.qobuz.com/images/covers/x_600.jpg"))
    }

    @Test fun queueJsonCarriesTheRelativeArt() {
        val t = Track("local", "t1", "Nemo", "Nightwish", "Once", 271, "http://192.168.1.5:8080/art/al1.sig")
        assertEquals("/art/al1.sig", HarmonyApi.trackJson(t).getString("art_url"))
    }
}
