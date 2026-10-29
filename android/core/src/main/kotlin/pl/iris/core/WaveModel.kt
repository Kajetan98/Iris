package pl.iris.core

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.exp
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.sin

/**
 * Model fali głosu IRIS: port host/api/static/wave.js bez rysowania (rysuje Compose w module app).
 * Amplituda zależy od poziomu dźwięku: mikrofon przy słuchaniu, stały wzór przy mówieniu.
 * Kilka pasm o różnych fazach, zwężonych na końcach; kolory się sumują.
 */
enum class WaveState { OFFLINE, CONNECTING, INACTIVE, IDLE, LISTENING, THINKING, SPEAKING }

/** Cel dla stanu: wysokość bez dźwięku, wpływ poziomu dźwięku, tempo, rozejście pasm, ciepło kolorów, jasność. */
data class WaveTarget(val amp: Double, val gain: Double, val speed: Double, val spread: Double, val warm: Double, val light: Double)

/** Pasmo: grubość w px, jasność, dwie częstotliwości, fazy, prędkości, udział w wysokości, kolory (RGB) ciepłe i chłodne. */
class Strand(
    val w: Double,
    val a: Double,
    val k: DoubleArray,
    val p: DoubleArray,
    val s: DoubleArray,
    val amp: Double,
    val warm: List<Int>,
    val cool: List<Int>,
    val dots: Boolean = false,
)

class Spark(val u: Double, val dy: Double, val f: Double, val ph: Double)

class WaveModel(private val reducedMotion: Boolean = false) {
    var state: WaveState = WaveState.OFFLINE
    var amp = STATES.getValue(WaveState.OFFLINE).amp
        private set
    var spread = STATES.getValue(WaveState.OFFLINE).spread
        private set
    var warm = STATES.getValue(WaveState.OFFLINE).warm
        private set
    var light = STATES.getValue(WaveState.OFFLINE).light
        private set
    var speed = STATES.getValue(WaveState.OFFLINE).speed
        private set
    var smoothLevel = 0.0
        private set
    var time = 0.0
        private set
    private var phase = 1.3 // czas fali (rośnie z tempem stanu)

    fun target(): WaveTarget {
        val t = STATES.getValue(state)
        return if (reducedMotion) t.copy(amp = STILL_AMP[state] ?: t.amp, gain = 0.0, speed = 0.0) else t
    }

    /** Czy trzeba dalej rysować klatki (fala w ruchu albo trwa przejście między stanami). */
    fun animating(): Boolean {
        val t = target()
        val moving = !reducedMotion && (t.speed > 0 || smoothLevel > 0.002)
        return moving || !settled(t)
    }

    private fun settled(t: WaveTarget): Boolean =
        abs(t.amp - amp) <= 0.002 && abs(t.spread - spread) <= 0.002 && abs(t.warm - warm) <= 0.002 &&
            abs(t.light - light) <= 0.002 && abs(t.speed - speed) <= 0.002

    /** Krok animacji o [dtSeconds] przy surowym poziomie dźwięku [rawLevel] (0-1). */
    fun step(dtSeconds: Double, rawLevel: Double) {
        val t = target()
        val dt = min(0.1, max(0.0, dtSeconds))
        time += dt
        val raw = if (t.gain > 0) rawLevel.coerceIn(0.0, 1.0) else 0.0
        // Szybkie narastanie, wolniejsze opadanie: fala nadąża za sylabami, ale nie migocze.
        smoothLevel += (raw - smoothLevel) * (1 - exp(-dt * (if (raw > smoothLevel) 26 else 7)))
        val k = if (reducedMotion) 1.0 else 1 - exp(-dt * 5)
        amp += (t.amp - amp) * k
        spread += (t.spread - spread) * k
        warm += (t.warm - warm) * k
        light += (t.light - light) * k
        speed += (t.speed - speed) * k
        phase += dt * speed
    }

    /**
     * Przesunięcie pionowe pasma [strand] względem środka jako ułamek wysokości płótna (dodatnie: w dół),
     * dla [SAMPLES] punktów od lewego do prawego końca fali.
     */
    fun offsets(strand: Int, out: DoubleArray = DoubleArray(SAMPLES)): DoubleArray {
        require(out.size == SAMPLES)
        val st = STRANDS[strand]
        val height = (amp + target().gain * smoothLevel).coerceIn(0.0, 1.0)
        val a = 0.46 * height
        val scale = 1 + (st.amp - 1) * spread
        for (i in 0 until SAMPLES) {
            val u = U[i]
            val own = wobble(st, u)
            val common = wobble(BASE, u)
            out[i] = a * ENV[i] * scale * (common + (own - common) * spread)
        }
        return out
    }

    private fun wobble(st: Strand, u: Double): Double =
        0.62 * sin(TAU * st.k[0] * u + st.p[0] + st.s[0] * phase) +
            0.38 * sin(TAU * st.k[1] * u + st.p[1] - st.s[1] * phase)

    /** Kolor [stop] (0 do [STOPS].size - 1) pasma przy bieżącym cieple: mieszanie chłodnego z ciepłym, RGB. */
    fun color(strand: Int, stop: Int): Int {
        val st = STRANDS[strand]
        val w = (Math.round(warm * 100) / 100.0)
        fun mix(shift: Int): Int {
            val c = (st.cool[stop] shr shift) and 255
            val h = (st.warm[stop] shr shift) and 255
            return Math.round(c + (h - c) * w).toInt().coerceIn(0, 255)
        }
        return (mix(16) shl 16) or (mix(8) shl 8) or mix(0)
    }

    /** Jasność pasma (przez `globalAlpha` w wersji przeglądarkowej). */
    fun alpha(strand: Int): Double = STRANDS[strand].a * light

    /** Jasność iskry [index] w bieżącym stanie (migotanie tylko w ruchu). */
    fun sparkAlpha(index: Int): Double {
        val p = SPARKS[index]
        val flicker = if (reducedMotion) 0.6 else 0.5 + 0.5 * sin(time * TAU * p.f * 0.5 + p.ph)
        return 0.55 * light * flicker * (0.3 + 0.7 * spread)
    }

    companion object {
        const val SAMPLES = 120
        const val SPAN = 0.88 // część szerokości zajęta przez falę
        private const val TAU = PI * 2

        val STATES: Map<WaveState, WaveTarget> = mapOf(
            WaveState.OFFLINE to WaveTarget(0.0, 0.0, 0.0, 0.05, 0.3, 0.3),
            WaveState.CONNECTING to WaveTarget(0.04, 0.0, 0.5, 0.3, 0.3, 0.45),
            WaveState.INACTIVE to WaveTarget(0.03, 0.0, 0.25, 0.25, 0.4, 0.4),
            WaveState.IDLE to WaveTarget(0.09, 0.0, 0.35, 0.55, 0.65, 0.8),
            WaveState.LISTENING to WaveTarget(0.1, 0.8, 0.8, 0.6, 0.0, 1.0),
            WaveState.THINKING to WaveTarget(0.16, 0.0, 1.7, 0.85, 0.6, 0.9),
            WaveState.SPEAKING to WaveTarget(0.26, 0.8, 1.15, 1.0, 1.0, 1.0),
        )

        /** Bez ruchu: stała wysokość dla stanu, bez śledzenia dźwięku. */
        private val STILL_AMP = mapOf(WaveState.LISTENING to 0.45, WaveState.THINKING to 0.3, WaveState.SPEAKING to 0.7)

        val STOPS = doubleArrayOf(0.08, 0.3, 0.52, 0.72, 0.92)

        private fun rgb(vararg hex: String): List<Int> = hex.map { it.removePrefix("#").toInt(16) }

        val STRANDS: List<Strand> = listOf(
            Strand(7.0, 0.28, doubleArrayOf(1.7, 3.2), doubleArrayOf(0.4, 2.0), doubleArrayOf(0.7, 0.5), 0.75,
                rgb("#ff8a5c", "#c45cff", "#5b6cff", "#8b5cf6", "#ff7a9a"), rgb("#4f7dff", "#3d6bff", "#5b8cff", "#4f7dff", "#7aa8ff")),
            Strand(3.4, 0.95, doubleArrayOf(1.9, 3.6), doubleArrayOf(0.2, 1.4), doubleArrayOf(1.0, 0.6), 1.0,
                rgb("#ff9466", "#7c5cff", "#3d7bff", "#5b7cff", "#ff7a8a"), rgb("#6aa8ff", "#4f7dff", "#3d7bff", "#5b8cff", "#8fbaff")),
            Strand(2.4, 0.85, doubleArrayOf(2.2, 3.1), doubleArrayOf(1.1, 0.3), doubleArrayOf(0.8, 0.9), 0.6,
                rgb("#ff7ab0", "#a855f7", "#6d5dfc", "#3b82f6", "#c084fc"), rgb("#7f9cff", "#6d8bff", "#4f7dff", "#3b82f6", "#9db8ff")),
            Strand(2.2, 0.85, doubleArrayOf(1.6, 4.1), doubleArrayOf(2.3, 0.9), doubleArrayOf(0.9, 0.7), 1.1,
                rgb("#c05cff", "#8b5cf6", "#ffd84a", "#ffb347", "#ff6fa0"), rgb("#9db8ff", "#6d8bff", "#bcd2ff", "#8fb3ff", "#7aa8ff")),
            Strand(1.5, 0.9, doubleArrayOf(2.0, 2.8), doubleArrayOf(0.8, 2.6), doubleArrayOf(1.1, 0.8), 0.5,
                rgb("#ffd6c2", "#ffffff", "#f3e8ff", "#ffffff", "#ffd1dc"), rgb("#dbe7ff", "#ffffff", "#eef3ff", "#ffffff", "#dbe7ff")),
            Strand(1.6, 0.95, doubleArrayOf(2.4, 3.4), doubleArrayOf(1.7, 1.2), doubleArrayOf(0.7, 1.0), 1.15,
                rgb("#ffe4d6", "#ffffff", "#ffffff", "#ffffff", "#ffd1dc"), rgb("#e3edff", "#ffffff", "#ffffff", "#ffffff", "#e3edff"), dots = true),
            Strand(2.8, 0.7, doubleArrayOf(1.8, 3.8), doubleArrayOf(2.9, 0.1), doubleArrayOf(0.6, 1.1), 0.85,
                rgb("#fb7185", "#ff8a5c", "#f472b6", "#ff8a5c", "#fb923c"), rgb("#5b8cff", "#7aa8ff", "#6d8bff", "#7aa8ff", "#5b8cff")),
        )

        private val BASE = STRANDS[1]

        /** Położenie punktów wzdłuż fali, 0 do 1. */
        val U = DoubleArray(SAMPLES) { it / (SAMPLES - 1.0) }

        /** Zwężenie grubości pasma na końcach. */
        val THIN = DoubleArray(SAMPLES) { sin(PI * U[it]).pow(0.9) }

        /** Obwiednia: zero na końcach, największa wysokość trochę na prawo od środka (jak w nagraniu mowy). */
        private val ENV = DoubleArray(SAMPLES) {
            val u = U[it]
            val taper = sin(PI * u).pow(2.4)
            val focus = exp(-(((u - 0.56) / 0.21).pow(2)))
            taper * (0.28 + 0.72 * focus)
        }

        /** Iskry wokół lewego końca, jak rozproszone światło w paśmie (ten sam generator co w wave.js). */
        val SPARKS: List<Spark> = run {
            var seed = 7L
            fun rand(): Double {
                seed = seed * 16807 % 2147483647
                return seed / 2147483647.0
            }
            List(46) {
                val u = 0.06 + 0.42 * rand().pow(1.6)
                val dy = rand() * 2 - 1
                val f = 0.6 + rand() * 2.2
                val ph = rand() * TAU
                Spark(u, dy, f, ph)
            }
        }
    }
}

/** Zgodność stanu ekranu z falą: słuchanie i mówienie mają własne stany, spoczynek wolniejszy. */
fun wavePhaseState(phase: Phase): WaveState = when (phase) {
    Phase.IDLE -> WaveState.IDLE
    Phase.LISTENING -> WaveState.LISTENING
    Phase.THINKING -> WaveState.THINKING
    Phase.SPEAKING -> WaveState.SPEAKING
}
