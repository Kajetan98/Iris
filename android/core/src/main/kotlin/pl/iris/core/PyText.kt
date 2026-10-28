package pl.iris.core

/**
 * Operacje na tekście zgodne z semantyką Pythona używaną w host/core/learning.py: długości i wycinki
 * w punktach kodowych, białe znaki jak `str.isspace`, słowa jak `\w+`. Regexy z `\s` i `\w` zależą od
 * platformy (Android ma ICU, JVM nie), więc reguły nie polegają na ich znaczeniu.
 */

/** Znaki `str.isspace()` Pythona (a więc i `\s` w jego wyrażeniach regularnych). */
internal fun isPySpace(cp: Int): Boolean = when (cp) {
    in 0x09..0x0D, in 0x1C..0x1F, 0x20, 0x85, 0xA0, 0x1680, in 0x2000..0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000 -> true
    else -> false
}

/** Fragment wyrażenia regularnego: klasa znaków równa `\s` Pythona. */
internal const val PY_WS_CLASS =
    "[\\u0009-\\u000D\\u001C-\\u001F\\u0020\\u0085\\u00A0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000]"

/** Znak słowa jak w `\w` Pythona: litera, cyfra lub inny znak liczbowy Unicode albo `_`. */
internal fun isWordCp(cp: Int): Boolean = cp == '_'.code || when (Character.getType(cp).toByte()) {
    Character.UPPERCASE_LETTER, Character.LOWERCASE_LETTER, Character.TITLECASE_LETTER,
    Character.MODIFIER_LETTER, Character.OTHER_LETTER,
    Character.DECIMAL_DIGIT_NUMBER, Character.LETTER_NUMBER, Character.OTHER_NUMBER,
    -> true
    else -> false
}

internal fun codePoints(s: String): IntArray = s.codePoints().toArray()

internal fun fromCodePoints(cps: IntArray, from: Int = 0, to: Int = cps.size): String = String(cps, from, to - from)

internal fun cpLength(s: String): Int = s.codePointCount(0, s.length)

/** `s[:n]` w punktach kodowych. */
internal fun cpTake(s: String, n: Int): String =
    if (cpLength(s) <= n) s else s.substring(0, s.offsetByCodePoints(0, n))

/** `s.strip()` Pythona (znaki z [isPySpace]). */
internal fun pyStrip(s: String): String = stripWhere(s, ::isPySpace)

/** `s.strip(chars)` Pythona. */
internal fun stripChars(s: String, chars: String): String {
    val set = chars.codePoints().toArray().toSet()
    return stripWhere(s) { it in set }
}

/** `s.rstrip(chars)` Pythona. */
internal fun rstripChars(s: String, chars: String): String {
    val set = chars.codePoints().toArray().toSet()
    val cps = codePoints(s)
    var end = cps.size
    while (end > 0 && cps[end - 1] in set) end--
    return fromCodePoints(cps, 0, end)
}

private fun stripWhere(s: String, drop: (Int) -> Boolean): String {
    val cps = codePoints(s)
    var start = 0
    var end = cps.size
    while (start < end && drop(cps[start])) start++
    while (end > start && drop(cps[end - 1])) end--
    return fromCodePoints(cps, start, end)
}

/** `re.sub(r"\s+", " ", s)` Pythona. */
internal fun collapseSpaces(s: String): String {
    val out = StringBuilder(s.length)
    var inSpace = false
    s.codePoints().forEach { cp ->
        if (isPySpace(cp)) {
            if (!inSpace) out.append(' ')
            inSpace = true
        } else {
            inSpace = false
            out.appendCodePoint(cp)
        }
    }
    return out.toString()
}

/** `re.findall(r"\w+", s)` Pythona. */
internal fun wordsOf(s: String): List<String> {
    val words = mutableListOf<String>()
    val cur = StringBuilder()
    s.codePoints().forEach { cp ->
        if (isWordCp(cp)) {
            cur.appendCodePoint(cp)
        } else if (cur.isNotEmpty()) {
            words += cur.toString()
            cur.setLength(0)
        }
    }
    if (cur.isNotEmpty()) words += cur.toString()
    return words
}

/** `s.lower()` Pythona (reguły Unicode, bez wpływu ustawień regionalnych). */
internal fun pyLower(s: String): String = s.lowercase(java.util.Locale.ROOT)

/** `str.isdigit()` Pythona dla niepustego słowa składającego się z cyfr dziesiętnych Unicode. */
internal fun isAllDigits(s: String): Boolean =
    s.isNotEmpty() && s.codePoints().allMatch { Character.getType(it).toByte() == Character.DECIMAL_DIGIT_NUMBER }
