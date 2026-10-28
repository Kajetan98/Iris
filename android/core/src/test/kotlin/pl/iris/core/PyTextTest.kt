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
}
