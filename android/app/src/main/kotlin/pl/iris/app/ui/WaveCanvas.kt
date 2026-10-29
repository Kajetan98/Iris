package pl.iris.app.ui

import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Fill
import androidx.compose.ui.graphics.drawscope.Stroke
import kotlin.math.abs
import kotlin.math.min
import kotlin.math.sin
import pl.iris.core.Phase
import pl.iris.core.WaveModel
import pl.iris.core.wavePhaseState

/**
 * Fala głosu IRIS (rysunek według host/api/static/wave.js; geometrię liczy WaveModel z modułu core).
 * Poziom przy słuchaniu to mikrofon; przy mówieniu TextToSpeech nie udostępnia dźwięku, więc fala porusza się
 * według stałego wzoru (przybliżenie, nie odczyt dźwięku).
 */
@Composable
fun WaveCanvas(phase: Phase, level: Float, modifier: Modifier = Modifier) {
    val model = remember { WaveModel() }
    val offsets = remember { DoubleArray(WaveModel.SAMPLES) }
    var frame by remember { mutableLongStateOf(0L) }
    val currentPhase by rememberUpdatedState(phase)
    val currentLevel by rememberUpdatedState(level)

    LaunchedEffect(Unit) {
        var last = 0L
        var speakingTime = 0.0
        while (true) {
            androidx.compose.runtime.withFrameNanos { now ->
                val dt = if (last == 0L) 0.0 else (now - last) / 1e9
                last = now
                model.state = wavePhaseState(currentPhase)
                val raw = when (currentPhase) {
                    Phase.LISTENING -> currentLevel.toDouble()
                    Phase.SPEAKING -> {
                        speakingTime += dt
                        min(1.0, 0.22 + 0.2 * abs(sin(speakingTime * 9.5)) + 0.18 * abs(sin(speakingTime * 3.1)))
                    }
                    else -> {
                        speakingTime = 0.0
                        0.0
                    }
                }
                model.step(dt, raw)
                frame = now
            }
        }
    }

    Canvas(modifier) {
        frame // odczyt stanu: każda klatka rysuje od nowa
        drawWave(model, offsets)
    }
}

private fun DrawScope.drawWave(model: WaveModel, offsets: DoubleArray) {
    val w = size.width
    val h = size.height
    val x0 = w * (1 - WaveModel.SPAN.toFloat()) / 2
    val span = w * WaveModel.SPAN.toFloat()
    val cy = h / 2
    val density = this.density

    fun x(i: Int) = x0 + WaveModel.U[i].toFloat() * span

    for (n in WaveModel.STRANDS.indices) {
        val strand = WaveModel.STRANDS[n]
        model.offsets(n, offsets)
        val stops = buildList {
            add(0f to Color.Transparent)
            WaveModel.STOPS.forEachIndexed { i, at ->
                add(at.toFloat() to Color(0xFF000000.toInt() or model.color(n, i)))
            }
            add(1f to Color.Transparent)
        }
        val brush = Brush.horizontalGradient(*stops.toTypedArray(), startX = x0, endX = x0 + span)
        val alpha = model.alpha(n).toFloat().coerceIn(0f, 1f)

        fun line(): Path = Path().apply {
            for (i in 0 until WaveModel.SAMPLES) {
                val y = cy + h * offsets[i].toFloat()
                if (i == 0) moveTo(x(i), y) else lineTo(x(i), y)
            }
        }

        if (strand.dots) {
            drawPath(
                line(), brush, alpha = alpha, blendMode = BlendMode.Plus,
                style = Stroke(
                    width = strand.w.toFloat() * density, cap = StrokeCap.Round, join = StrokeJoin.Round,
                    pathEffect = PathEffect.dashPathEffect(floatArrayOf(0.001f, 4.2f * density)),
                ),
            )
            continue
        }
        // poświata: jedno szerokie, słabe pociągnięcie pod rdzeniem
        drawPath(
            line(), brush, alpha = alpha * 0.16f, blendMode = BlendMode.Plus,
            style = Stroke(width = min(strand.w * 3.4, 14.0).toFloat() * density, cap = StrokeCap.Round, join = StrokeJoin.Round),
        )
        // rdzeń: wstęga zwężona na końcach
        val ribbon = Path().apply {
            for (i in 0 until WaveModel.SAMPLES) {
                val half = strand.w.toFloat() * density * WaveModel.THIN[i].toFloat() / 2
                val y = cy + h * offsets[i].toFloat() - half
                if (i == 0) moveTo(x(i), y) else lineTo(x(i), y)
            }
            for (i in WaveModel.SAMPLES - 1 downTo 0) {
                val half = strand.w.toFloat() * density * WaveModel.THIN[i].toFloat() / 2
                lineTo(x(i), cy + h * offsets[i].toFloat() + half)
            }
            close()
        }
        drawPath(ribbon, brush, alpha = alpha, blendMode = BlendMode.Plus, style = Fill)
    }

    // iskry przy lewym końcu, migoczą tylko w ruchu
    for (i in WaveModel.SPARKS.indices) {
        val a = model.sparkAlpha(i).toFloat()
        if (a < 0.02f) continue
        val p = WaveModel.SPARKS[i]
        val idx = Math.round(p.u * (WaveModel.SAMPLES - 1)).toInt()
        val px = x0 + p.u.toFloat() * span
        val py = cy + h * offsets[idx].toFloat() + p.dy.toFloat() * (2 + 6 * WaveModel.THIN[idx].toFloat()) * density * (0.4f + model.spread.toFloat())
        drawRect(Color(1f, 0.85f, 0.93f), topLeft = Offset(px, py), size = Size(1.4f * density, 1.4f * density), alpha = a.coerceIn(0f, 1f), blendMode = BlendMode.Plus)
    }
}
