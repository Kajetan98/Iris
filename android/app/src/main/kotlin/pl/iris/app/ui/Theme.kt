package pl.iris.app.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color

/** Kolory jak w kliencie przeglądarkowym (host/api/static/style.css): tylko ciemny motyw, fala jest jedynym barwnym elementem. */
object IrisColors {
    val Bg = Color(0xFF07060B)
    val Haze = Color(0xFF1A1224)
    val Surface = Color(0xFF13111A)
    val Field = Color(0xFF0C0B11)
    val Ink = Color(0xFFEEEDF3)
    val Ink2 = Color(0xFFA8A4B6)
    val Line = Color(0xFF2C2938)
    val You = Color(0xFF8FB3FF)
    val Warn = Color(0xFFFFB08A)
    val Voice = Brush.horizontalGradient(listOf(Color(0xFF2F62E6), Color(0xFF6A45DC), Color(0xFFA8408A)))
}

@Composable
fun IrisTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(
            primary = IrisColors.You,
            onPrimary = IrisColors.Bg,
            background = IrisColors.Bg,
            onBackground = IrisColors.Ink,
            surface = IrisColors.Surface,
            onSurface = IrisColors.Ink,
            surfaceVariant = IrisColors.Field,
            onSurfaceVariant = IrisColors.Ink2,
            outline = IrisColors.Line,
            error = IrisColors.Warn,
        ),
        content = content,
    )
}
