package pl.iris.core

import java.time.Duration
import java.time.Instant

/**
 * Deterministyczne reguły uczenia się (docs/spec.md, sekcja 9, "Uczenie się"). Port host/core/learning.py;
 * zgodność pilnuje VectorsTest na wspólnym pliku testowym.
 */

const val MIN_FACT_CHARS = 3
const val MAX_FACT_CHARS = 300

private const val PREFIX = "^(?:iris[ ,]+)?"
private const val NOT_WORD = "(?![\\p{L}\\p{N}_])" // koniec słowa jak \b Pythona przed znakiem słowa

private val REMEMBER = Regex(
    PREFIX + "(?:(?:zapamiętaj|zapisz)(?: sobie)?[ ,:]+(?:(?:o tym,? )?że )?" +
        "|pamiętaj(?: sobie)?[ ,:]+(?:o tym,? )?że )(?<x>.+)$",
    setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
)
private val FORGET_ALL = Regex(PREFIX + "zapomnij (?:o mnie )?wszystko(?: o mnie)?$", RegexOption.IGNORE_CASE)
private val FORGET = Regex(
    PREFIX + "zapomnij[ ,:]+(?:(?:o tym,? )?że |o )?(?<x>.+)$",
    setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
)
private val RECALL = Regex(
    PREFIX + "(?:co (?:o mnie wiesz|wiesz o mnie)|czego się o mnie nauczył(?:aś|eś))$",
    RegexOption.IGNORE_CASE,
)
private val IRIS = Regex("^iris[ ,]+")

// Reakcje to cała wypowiedź; dłuższe zdanie z takim słowem na początku ("źle się czuję") to zwykła rozmowa.
private val PRAISE = setOf("dobrze", "świetnie", "dobra odpowiedź", "trafnie", "dokładnie tak", "dzięki", "dziękuję")
private val CRITICISM = setOf("źle", "to nie tak", "nie tak", "nie o to pytałem", "nie o to chodziło", "to nieprawda", "mylisz się")

private val YES = Regex("^(?:tak|zgadza się|dokładnie|owszem)$NOT_WORD", RegexOption.IGNORE_CASE)
private val NO = Regex(
    "^(?:nie zgadza się|nieprawda|nie)$NOT_WORD[ ,.:;!-]*(?<rest>.*)$",
    setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
)

// Słowa pomocnicze pomijane przy porównaniu (zaimki, "mam/masz", "jest/jesteś").
private val STOPWORDS = setOf(
    "jest", "jestem", "jesteś", "mam", "masz", "się", "nie", "tak", "oraz", "który", "która", "które",
    "tego", "tym", "ten", "jak", "dla", "przez", "bardzo", "mój", "moja", "moje", "twój", "twoja",
    "twoje", "mnie", "ciebie", "też", "także", "lub", "albo",
)

enum class MemoryKind { REMEMBER, FORGET, FORGET_ALL, RECALL }

data class MemoryCommand(val kind: MemoryKind, val text: String = "")

private fun clean(text: String): String = pyStrip(rstripChars(collapseSpaces(pyStrip(text)), ".!?…"))

/** Tekst faktu w słowach użytkownika: bez cudzysłowów i końcowej interpunkcji. */
fun factText(text: String): String = pyStrip(stripChars(clean(text), "\"'„”«» "))

/** Polecenie pamięci albo null. Tekst faktu zachowuje wielkość liter wypowiedzi. */
fun parseMemory(text: String): MemoryCommand? {
    val t = pyStrip(rstripChars(clean(text), "?"))
    if (RECALL.containsMatchIn(t)) return MemoryCommand(MemoryKind.RECALL)
    if (FORGET_ALL.containsMatchIn(t)) return MemoryCommand(MemoryKind.FORGET_ALL)
    for ((kind, pattern) in listOf(MemoryKind.REMEMBER to REMEMBER, MemoryKind.FORGET to FORGET)) {
        val m = pattern.find(t) ?: continue
        val x = factText(m.groups["x"]!!.value)
        return if (cpLength(x) >= MIN_FACT_CHARS) MemoryCommand(kind, cpTake(x, MAX_FACT_CHARS)) else null
    }
    return null
}

/** Odpowiedź na pytanie: `yes` to true dla "tak", false dla "nie" (z poprawką), null w innym wypadku. */
data class ParsedAnswer(val yes: Boolean?, val correction: String = "")

fun parseAnswer(text: String): ParsedAnswer {
    val t = clean(text)
    if (YES.containsMatchIn(t)) return ParsedAnswer(true)
    val m = NO.find(t) ?: return ParsedAnswer(null)
    val rest = factText(m.groups["rest"]!!.value)
    return ParsedAnswer(false, if (cpLength(rest) >= MIN_FACT_CHARS) rest else "")
}

enum class Feedback { PRAISE, CRITICISM }

/** Pochwała albo krytyka ostatniej odpowiedzi (spec, sekcja 9, "Reakcje użytkownika"), inaczej null. */
fun parseFeedback(text: String): Feedback? {
    val t = stripChars(IRIS.replace(pyLower(clean(text)), ""), " ,;:")
    return when (t) {
        in PRAISE -> Feedback.PRAISE
        in CRITICISM -> Feedback.CRITICISM
        else -> null
    }
}

// Rdzeń przybliżony: polska odmiana zmienia końcówki ("pracuję"/"pracujesz", "kawa"/"kawę").
private fun key(word: String): String = cpTake(word, if (cpLength(word) > 4) 4 else 3)

fun stems(text: String): Set<String> {
    val words = wordsOf(pyLower(text))
    val out = HashSet<String>()
    for (w in words) {
        if (cpLength(w) >= 3 && !isAllDigits(w) && w !in STOPWORDS) out += key(w)
    }
    for (w in words) if (isAllDigits(w)) out += w
    return out
}

/** Indeks Jaccarda zbiorów rdzeni słów (spec, sekcja 9). */
fun similarity(a: String, b: String): Double {
    val sa = stems(a)
    val sb = stems(b)
    if (sa.isEmpty() || sb.isEmpty()) return 0.0
    val common = sa.count { it in sb }
    return common.toDouble() / (sa.size + sb.size - common)
}

/** Najbardziej podobny fakt (przy remisie nowszy) albo null poniżej progu. */
fun bestMatch(text: String, facts: Iterable<Fact>, minScore: Double): Fact? {
    var best: Fact? = null
    var bestScore = 0.0
    for (f in facts) {
        val score = similarity(text, f.text)
        if (score < minScore) continue
        val b = best
        val better = b == null || score > bestScore ||
            (score == bestScore && f.updatedAt.toEpochMilli() > b.updatedAt.toEpochMilli())
        if (better) {
            best = f
            bestScore = score
        }
    }
    return best
}

/** Czy tekst zawiera rdzeń z listy danych wrażliwych (dopasowanie do początku słowa). */
fun isSensitive(text: String, stems: List<String>): Boolean {
    val words = wordsOf(pyLower(text))
    return words.any { w -> stems.any { w.startsWith(it) } }
}

/** Mała pierwsza litera zdania, ale nie skrótu ("USA"). */
private fun lowerFirst(text: String): String {
    if (text.isEmpty()) return text
    val first = text.codePointAt(0)
    val firstLen = Character.charCount(first)
    val secondUpper = text.length > firstLen && Character.isUpperCase(text.codePointAt(firstLen))
    return if (Character.isUpperCase(first) && !secondUpper) {
        String(Character.toChars(Character.toLowerCase(first))) + text.substring(firstLen)
    } else {
        text
    }
}

fun questionFor(fact: Fact): String = "Czy dobrze rozumiem: ${lowerFirst(factText(fact.text))}?"

/** Przypuszczenie do zapytania: najwyższa pewność, przy remisie najnowsze. */
fun pickQuestion(facts: Iterable<Fact>, now: Instant, cfg: LearningConfig): Fact? {
    val again = Duration.ofMillis(Math.round(cfg.askAgainDays * 86_400_000.0))
    return facts
        .filter {
            it.source == Source.INFERRED && it.confidence >= cfg.askMin &&
                (it.askedAt == null || Duration.between(it.askedAt, now) >= again)
        }
        .maxWithOrNull(
            compareBy<Fact> { it.confidence }.thenBy { it.updatedAt.toEpochMilli() }.thenBy { it.id },
        )
}

internal fun plural(n: Int, one: String, few: String, many: String): String = when {
    n == 1 -> one
    n % 10 in 2..4 && n % 100 !in 12..14 -> few
    else -> many
}

/** Odpowiedź na "co o mnie wiesz": 5 faktów podanych wprost i 3 przypuszczenia. */
fun recallText(facts: List<Fact>, minInferred: Double): String {
    val stated = facts.filter { it.source == Source.STATED }.sortedByDescending { it.updatedAt.toEpochMilli() }
    val guesses = facts.filter { it.source == Source.INFERRED && it.confidence >= minInferred }
        .sortedWith(compareByDescending<Fact> { it.confidence }.thenByDescending { it.updatedAt.toEpochMilli() })
    if (facts.isEmpty()) return "Jeszcze nic o tobie nie wiem. Możesz powiedzieć: zapamiętaj, że ..."
    val parts = mutableListOf<String>()
    val shown = stated.take(5)
    if (shown.isNotEmpty()) {
        parts += "Wiem od ciebie: " + shown.joinToString("; ") { "\"${lowerFirst(factText(it.text))}\"" } + "."
    }
    val shownGuesses = guesses.take(3)
    if (shownGuesses.isNotEmpty()) {
        val items = shownGuesses.map { "${lowerFirst(factText(it.text))} (${Math.rint(it.confidence * 100).toLong()}%)" }
        parts += "Przypuszczam, że " + items.joinToString("; ") + "."
    }
    val rest = facts.size - shown.size - shownGuesses.size
    if (parts.isEmpty()) {
        val word = plural(rest, "niepewne przypuszczenie", "niepewne przypuszczenia", "niepewnych przypuszczeń")
        return "Mam tylko $rest $word, pełna lista jest w aplikacji."
    }
    if (rest > 0) {
        val word = plural(rest, "informacja", "informacje", "informacji")
        parts += "Oprócz tego $rest $word, pełna lista jest w aplikacji."
    }
    return parts.joinToString(" ")
}
