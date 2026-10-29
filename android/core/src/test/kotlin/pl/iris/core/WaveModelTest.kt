package pl.iris.core

import kotlin.math.abs
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class WaveModelTest {
    private fun settle(model: WaveModel, seconds: Double = 5.0, level: Double = 0.0) {
        var t = 0.0
        while (t < seconds) {
            model.step(1 / 60.0, level)
            t += 1 / 60.0
        }
    }

    private fun maxAbs(model: WaveModel, strand: Int) = model.offsets(strand).maxOf { abs(it) }

    @Test
    fun `the wave tapers to zero at both ends for every strand and state`() {
        for (state in WaveState.entries) {
            val m = WaveModel().apply { this.state = state }
            settle(m, 2.0, 0.6)
            for (s in WaveModel.STRANDS.indices) {
                val o = m.offsets(s)
                assertEquals(WaveModel.SAMPLES, o.size)
                assertEquals(0.0, o.first(), 1e-9, "$state/$s")
                assertEquals(0.0, o.last(), 1e-9, "$state/$s")
            }
        }
    }

    @Test
    fun `offline is flat and speaking is taller than idle`() {
        val offline = WaveModel().apply { state = WaveState.OFFLINE }
        settle(offline)
        assertEquals(0.0, maxAbs(offline, 1), 1e-9)
        val idle = WaveModel().apply { state = WaveState.IDLE }
        val speaking = WaveModel().apply { state = WaveState.SPEAKING }
        settle(idle)
        settle(speaking, level = 0.7)
        assertTrue(maxAbs(speaking, 1) > maxAbs(idle, 1) * 2)
        assertTrue(maxAbs(speaking, 1) < 0.5) // mieści się w płótnie
    }

    @Test
    fun `the level lifts the wave only while listening or speaking`() {
        val quiet = WaveModel().apply { state = WaveState.LISTENING }
        val loud = WaveModel().apply { state = WaveState.LISTENING }
        settle(quiet, level = 0.0)
        settle(loud, level = 1.0)
        assertTrue(maxAbs(loud, 1) > maxAbs(quiet, 1))
        val thinking = WaveModel().apply { state = WaveState.THINKING }
        settle(thinking, level = 1.0)
        assertEquals(0.0, thinking.smoothLevel, 1e-9) // myślenie nie śledzi dźwięku
    }

    @Test
    fun `transitions converge and stop animating`() {
        val m = WaveModel().apply { state = WaveState.IDLE }
        assertTrue(m.animating())
        settle(m, 10.0)
        assertTrue(m.animating()) // idle się porusza (speed > 0)
        val still = WaveModel(reducedMotion = true).apply { state = WaveState.LISTENING }
        settle(still, 1.0, 1.0)
        assertFalse(still.animating())
        assertEquals(0.45, still.amp, 1e-9) // bez ruchu: stała wysokość stanu
        val before = still.offsets(1).copyOf()
        settle(still, 1.0, 1.0)
        assertTrue(before.contentEquals(still.offsets(1)))
    }

    @Test
    fun `colors blend from cool to warm and stay valid`() {
        val m = WaveModel().apply { state = WaveState.LISTENING }
        settle(m)
        val cool = WaveModel.STRANDS[1].cool[0]
        assertEquals(cool, m.color(1, 0))
        m.state = WaveState.SPEAKING
        settle(m)
        assertEquals(WaveModel.STRANDS[1].warm[0], m.color(1, 0))
        for (s in WaveModel.STRANDS.indices) for (stop in WaveModel.STOPS.indices) assertTrue(m.color(s, stop) in 0..0xFFFFFF)
        assertTrue(m.alpha(1) in 0.0..1.0)
    }

    @Test
    fun `sparks reproduce the browser generator`() {
        assertEquals(46, WaveModel.SPARKS.size)
        // Pierwsza iskra z generatora liniowego wave.js: seed 7, 16807 mod 2147483647.
        val r1 = 7L * 16807 % 2147483647 / 2147483647.0
        assertEquals(0.06 + 0.42 * Math.pow(r1, 1.6), WaveModel.SPARKS[0].u, 1e-12)
        assertTrue(WaveModel.SPARKS.all { it.u in 0.06..0.48 && it.dy in -1.0..1.0 })
        val m = WaveModel().apply { state = WaveState.SPEAKING }
        settle(m)
        assertTrue((0 until 46).all { m.sparkAlpha(it) in 0.0..0.6 })
        assertEquals(WaveState.SPEAKING, wavePhaseState(Phase.SPEAKING))
        assertEquals(WaveState.IDLE, wavePhaseState(Phase.IDLE))
    }
}
