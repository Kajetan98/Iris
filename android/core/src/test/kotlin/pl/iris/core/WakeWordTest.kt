package pl.iris.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class WakeWordTest {
    @Test
    fun `the wake phrases and their recognizer variants match`() {
        for (t in listOf("hej iris", "Hej Iris!", "irysku", "Irysku, słuchaj", "hej irys", "iris", "ej irysek", "  IRIS  ", "powiedz irysku coś")) {
            assertTrue(matchesWakeWord(t), t)
        }
    }

    @Test
    fun `other speech does not match`() {
        for (t in listOf("", "hej", "hej ty", "iryd", "wiris", "dobry wieczór", "ir ys")) {
            assertFalse(matchesWakeWord(t), t)
        }
    }

    @Test
    fun `the wake word is listened for only when everything allows it and the app is idle`() {
        assertTrue(wakeListening(true, true, true, true, Phase.IDLE))
        assertFalse(wakeListening(false, true, true, true, Phase.IDLE))
        assertFalse(wakeListening(true, false, true, true, Phase.IDLE))
        assertFalse(wakeListening(true, true, false, true, Phase.IDLE))
        assertFalse(wakeListening(true, true, true, false, Phase.IDLE))
        for (p in Phase.entries.filter { it != Phase.IDLE }) assertFalse(wakeListening(true, true, true, true, p), p.name)
    }
}
