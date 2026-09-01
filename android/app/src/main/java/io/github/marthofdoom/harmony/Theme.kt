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

// A complete Material 3 scheme seeded from the Harmony brand blue, so every
// role (secondary/tertiary/error/outline/surface) is defined and nothing falls
// back to the baseline purple palette on pre-Android-12 devices.
private val LightColors = lightColorScheme(
    primary = Brand,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFDCE6FF),
    onPrimaryContainer = Color(0xFF0A1F44),
    secondary = Color(0xFF565E71),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFDAE2F9),
    onSecondaryContainer = Color(0xFF131C2B),
    tertiary = Color(0xFF3D6373),
    onTertiary = Color.White,
    tertiaryContainer = Color(0xFFC1E8FB),
    onTertiaryContainer = Color(0xFF001F29),
    error = Color(0xFFBA1A1A),
    onError = Color.White,
    errorContainer = Color(0xFFFFDAD6),
    onErrorContainer = Color(0xFF410002),
    background = Color(0xFFFAFBFD),
    onBackground = Color(0xFF1A1C1F),
    surface = Color(0xFFFFFFFF),
    onSurface = Color(0xFF1A1C1F),
    surfaceVariant = Color(0xFFE1E2EC),
    onSurfaceVariant = Color(0xFF44474F),
    outline = Color(0xFF74777F),
    outlineVariant = Color(0xFFC4C6D0),
)

private val DarkColors = darkColorScheme(
    primary = BrandLight,
    onPrimary = Color(0xFF0C1E3A),
    primaryContainer = Color(0xFF23406E),
    onPrimaryContainer = Color(0xFFDCE6FF),
    secondary = Color(0xFFBEC6DC),
    onSecondary = Color(0xFF283141),
    secondaryContainer = Color(0xFF3E4759),
    onSecondaryContainer = Color(0xFFDAE2F9),
    tertiary = Color(0xFFA5CDDF),
    onTertiary = Color(0xFF063544),
    tertiaryContainer = Color(0xFF234C5B),
    onTertiaryContainer = Color(0xFFC1E8FB),
    error = Color(0xFFFFB4AB),
    onError = Color(0xFF690005),
    errorContainer = Color(0xFF93000A),
    onErrorContainer = Color(0xFFFFDAD6),
    background = Color(0xFF141619),
    onBackground = Color(0xFFE4E6EA),
    surface = Color(0xFF1D2024),
    onSurface = Color(0xFFE4E6EA),
    surfaceVariant = Color(0xFF44474F),
    onSurfaceVariant = Color(0xFFC4C6D0),
    outline = Color(0xFF8E9099),
    outlineVariant = Color(0xFF44474F),
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
