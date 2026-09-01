package io.github.marthofdoom.harmony

import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext

// Harmony brand blue (the app icon's gradient), used when the device can't
// supply a dynamic Material You palette.
private val Brand = Color(0xFF3A6FD8)
private val BrandLight = Color(0xFF5B8DEF)

private val LightColors = lightColorScheme(
    primary = Brand,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFDCE6FF),
    onPrimaryContainer = Color(0xFF0A1F44),
    secondary = Color(0xFF4C6089),
    background = Color(0xFFFAFBFD),
    onBackground = Color(0xFF1A1C1F),
    surface = Color(0xFFFFFFFF),
    surfaceVariant = Color(0xFFE4E7EE),
    onSurfaceVariant = Color(0xFF5A5F6A),
)

private val DarkColors = darkColorScheme(
    primary = BrandLight,
    onPrimary = Color(0xFF0C1E3A),
    primaryContainer = Color(0xFF23406E),
    onPrimaryContainer = Color(0xFFDCE6FF),
    secondary = Color(0xFFB3C5E8),
    background = Color(0xFF141619),
    onBackground = Color(0xFFE4E6EA),
    surface = Color(0xFF1D2024),
    surfaceVariant = Color(0xFF2A2E35),
    onSurfaceVariant = Color(0xFFA9AEB8),
)

/** Themes the app: Material You dynamic color on Android 12+, else the Harmony
 *  brand palette; follows the system light/dark setting. */
@Composable
fun HarmonyTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val scheme = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S -> {
            val ctx = LocalContext.current
            if (dark) dynamicDarkColorScheme(ctx) else dynamicLightColorScheme(ctx)
        }
        dark -> DarkColors
        else -> LightColors
    }
    MaterialTheme(colorScheme = scheme, content = content)
}
