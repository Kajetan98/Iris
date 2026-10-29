package pl.iris.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class PyTextTest {
    @Test
    fun `whitespace follows str isspace`() {
        for (c in listOf(' ', '\t', '\n', '\u000B', '\u000C', '\r', '\u001C', '\u0085', ' ', ' ', ' ', '　')) {
            assertTrue(isPySpace(c.code), "U+%04X".format(c.code))
        }
        for (c in listOf('a', 'ł', '0', '_', '-', '​', '⁠')) assertFalse(isPySpace(c.code), "U+%04X".format(c.code))
    }

    @Test
    fun `strip and collapse use code points and python whitespace`() {
        assertEquals("a b", pyStrip("  a b "))
        assertEquals("a b c", collapseSpaces("a  \n b\t\tc"))
        assertEquals("ab", stripChars("„„ab”\"", "\"'„”«» "))
        assertEquals("ab", rstripChars("ab.!?…", ".!?…"))
        assertEquals("a😀", cpTake("a😀b", 2))
        assertEquals(3, cpLength("a😀b"))
    }

    @Test
    fun `words are letters digits and underscores`() {
        assertEquals(listOf("zażółć", "gęślą", "jaźń_2", "½"), wordsOf("zażółć, gęślą; jaźń_2 ½!"))
        assertEquals(listOf("abc"), wordsOf("😀 abc 😀"))
        assertTrue(isAllDigits("2024") && !isAllDigits("20a4") && !isAllDigits(""))
    }

    @Test
    fun `lowercasing is locale independent`() {
        val previous = java.util.Locale.getDefault()
        try {
            java.util.Locale.setDefault(java.util.Locale.forLanguageTag("tr-TR"))
            assertEquals("iris", pyLower("IRIS"))
        } finally {
            java.util.Locale.setDefault(previous)
        }
    }

    @Test
    fun `database time has a fixed width like the host`() {
        assertEquals("2026-03-01T12:00:00.000+00:00", IsoTime.format(java.time.Instant.parse("2026-03-01T12:00:00Z")))
        assertEquals("2026-03-01T12:00:00.250+00:00", IsoTime.format(java.time.Instant.parse("2026-03-01T12:00:00.25Z")))
        assertEquals("2026-03-01T12:00:00.123+00:00", IsoTime.format(java.time.Instant.parse("2026-03-01T12:00:00.123456Z")))
        assertEquals(java.time.Instant.parse("2026-03-01T13:00:00Z"), IsoTime.parse("2026-03-01T14:00:00.000+01:00"))
        val s = "2026-10-28T19:34:47.512+00:00"
        assertEquals(s, IsoTime.format(IsoTime.parse(s)))
    }

    @Test
    fun `fact meta shows the source and the confidence in percent`() {
        val t = java.time.Instant.EPOCH
        fun fact(source: Source, c: Double) = Fact(1, "x", source, c, t, t)
        assertEquals("od ciebie", Texts.factMeta(fact(Source.STATED, 1.0)))
        assertEquals("przypuszczenie, pewność 65%", Texts.factMeta(fact(Source.INFERRED, 0.65)))
        assertEquals("przypuszczenie, pewność 45%", Texts.factMeta(fact(Source.INFERRED, 0.45)))
    }
}
