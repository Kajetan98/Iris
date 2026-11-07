package pl.iris.core

import java.math.BigDecimal
import java.math.RoundingMode
import java.time.Duration
import java.time.Instant
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * Pamięć IRIS jako graf pojęć (docs/spec.md, sekcja 9, "Pamięć: graf pojęć"). Port host/core/memory.py:
 * czyste reguły bez bazy i bez modelu językowego. Zgodność z Pythonem pilnuje VectorsTest na wspólnym pliku
 * memory_vectors.json.
 */

const val DAY_S = 86400.0
const val MAX_LABEL_CHARS = 40
const val MIN_COMMAND_CHARS = 3
const val MAX_COMMAND_CHARS = 300

/** Pojęcie w grafie. [strength] to siła w chwili [lastUsed]. */
data class Concept(
    val key: String,
    val label: String,
    val strength: Double,
    val lastUsed: Instant,
    val hits: Int,
    val createdAt: Instant,
    val domains: Map<String, Int> = emptyMap(),
)

/** Połączenie pojęć z jednej wypowiedzi; [a] < [b]. */
data class Link(val a: String, val b: String, val strength: Double, val lastUsed: Instant)

data class Extraction(
    val concepts: List<Pair<String, String>> = emptyList(), // (klucz, etykieta)
    val domain: String = DEFAULT_MEMORY_DOMAIN,
    val source: String = "heuristic",
)

data class LearnPlan(
    val concepts: List<Concept> = emptyList(),
    val links: List<Link> = emptyList(),
    val fresh: List<String> = emptyList(),
    val reinforced: List<String> = emptyList(),
    val domain: String = DEFAULT_MEMORY_DOMAIN,
    val stored: Boolean = true,
)

data class RecallHit(val key: String, val label: String, val score: Double, val via: String)

// ---- siła ------------------------------------------------------------------------------------------

private fun ageSeconds(lastUsed: Instant, now: Instant): Double =
    maxOf(0.0, Duration.between(lastUsed, now).toNanos() / 1e9)

/** Siła bieżąca: s · 0,5^(wiek / okres połowicznego zaniku). Wiek ujemny (zegar cofnięty) to zero. */
fun effective(strength: Double, lastUsed: Instant, now: Instant, halfLifeDays: Double): Double =
    strength * Math.pow(0.5, ageSeconds(lastUsed, now) / (halfLifeDays * DAY_S))

fun towardOne(e: Double, gain: Double): Double = e + (1.0 - e) * gain

fun conceptStrength(c: Concept, now: Instant, cfg: MemoryConfig): Double = effective(c.strength, c.lastUsed, now, cfg.halfLifeDays)

fun linkStrength(l: Link, now: Instant, cfg: MemoryConfig): Double = effective(l.strength, l.lastUsed, now, cfg.halfLifeDays)

// ---- wyciąganie pojęć ------------------------------------------------------------------------------

private val SUFFIXES = listOf(
    "owie", "ami", "ach", "ego", "emu", "ymi", "imi", "ów", "om", "iu", "ia", "ie",
    "ią", "ię", "ą", "ę", "y", "i", "a", "e", "u", "o",
)

private fun isDigitCp(cp: Int): Boolean = Character.getType(cp).toByte() == Character.DECIMAL_DIGIT_NUMBER

private fun hasDigit(s: String): Boolean = s.codePoints().anyMatch(::isDigitCp)

/** Przybliżony rdzeń polskiego słowa: łączy "lampa", "lampę" i "lampie" w "lamp". */
fun stem(word: String): String {
    val s = pyLower(word)
    val n = cpLength(s)
    if (n <= 4) return s
    for (suffix in SUFFIXES) {
        if (s.endsWith(suffix) && n - cpLength(suffix) >= 4) return s.substring(0, s.length - suffix.length)
    }
    return s
}

/** Słowa z liter i cyfr (`[^\W_]+` Pythona). */
private fun tokens(text: String): List<String> {
    val out = mutableListOf<String>()
    val cur = StringBuilder()
    text.codePoints().forEach { cp ->
        if (cp != '_'.code && isWordCp(cp)) {
            cur.appendCodePoint(cp)
        } else if (cur.isNotEmpty()) {
            out += cur.toString()
            cur.setLength(0)
        }
    }
    if (cur.isNotEmpty()) out += cur.toString()
    return out
}

/** Słowa z liter; słowo z cyfrą jest pomijane w całości ("SpO2", "12"). */
private fun letterWords(text: String): List<String> = tokens(text).filter { !hasDigit(it) }

/** Klucz pojęcia: rdzenie jego słów połączone spacją; pusty, gdy etykieta nie ma słów z liter. */
fun conceptKey(label: String): String = letterWords(label).joinToString(" ") { stem(it) }

private fun isAcronym(word: String): Boolean =
    cpLength(word) >= 2 && word == word.uppercase(java.util.Locale.ROOT) && word != pyLower(word)

/** Domena z największą liczbą trafionych rdzeni (przy remisie wcześniejsza na liście), inaczej "rozmowa". */
fun domainOf(text: String, cfg: MemoryConfig): String {
    val words = letterWords(text).map(::pyLower)
    var best = DEFAULT_MEMORY_DOMAIN
    var bestHits = 0
    for (domain in MEMORY_DOMAIN_IDS) {
        val stems = cfg.domainWords[domain] ?: continue
        val hits = stems.count { s -> words.any { it.startsWith(s) } }
        if (hits > bestHits) {
            best = domain
            bestHits = hits
        }
    }
    return best
}

/** Pojęcia bez LLM: słowa z liter, bez krótkich i pomocniczych, skróty w oryginalnej pisowni. */
fun heuristic(text: String, cfg: MemoryConfig): Extraction {
    val stop = cfg.stopWords.map(::pyLower).toSet()
    val out = mutableListOf<Pair<String, String>>()
    val seen = HashSet<String>()
    for (w in letterWords(text)) {
        val acronym = isAcronym(w)
        if (!acronym && (cpLength(w) < 3 || pyLower(w) in stop)) continue
        val key = stem(w)
        if (!seen.add(key)) continue
        out += key to (if (acronym) w else pyLower(w))
        if (out.size >= cfg.maxConcepts) break
    }
    return Extraction(out, domainOf(text, cfg), "heuristic")
}

/** Odpowiedź LLM `{"domain": ..., "concepts": [...]}` albo null, gdy jest niepoprawna. */
fun parseLlm(raw: String, cfg: MemoryConfig): Extraction? {
    val start = raw.indexOf('{')
    val end = raw.lastIndexOf('}')
    if (start < 0 || end < start) return null
    val data = try {
        Json.parseToJsonElement(raw.substring(start, end + 1))
    } catch (_: IllegalArgumentException) {
        return null
    } as? JsonObject ?: return null
    val concepts = data["concepts"]
    if (concepts != null && concepts !is JsonArray) return null
    val domainRaw = (data["domain"] as? JsonPrimitive)?.takeIf { it.isString }?.content
    val domain = if (domainRaw in MEMORY_DOMAIN_IDS) domainRaw!! else DEFAULT_MEMORY_DOMAIN
    val out = mutableListOf<Pair<String, String>>()
    val seen = HashSet<String>()
    for (item in concepts ?: JsonArray(emptyList())) {
        val text = (item as? JsonPrimitive)?.takeIf { it.isString }?.content ?: continue
        val label = pyStrip(cpTake(pyStrip(collapseSpaces(text)), MAX_LABEL_CHARS))
        val key = conceptKey(label)
        if (key.isEmpty() || !seen.add(key)) continue
        out += key to label
        if (out.size >= cfg.maxConcepts) break
    }
    return Extraction(out, domain, "llm")
}

/** Czy tekst ma słowo zaczynające się od rdzenia danych wrażliwych. */
fun isSensitiveText(text: String, stems: List<String>): Boolean {
    val words = tokens(pyLower(text))
    return words.any { w -> stems.any { w.startsWith(it) } }
}

/** Filtr pamięci: bez liczb zawsze, bez danych wrażliwych poza poleceniem "zapamiętaj". */
fun keep(extraction: Extraction, cfg: MemoryConfig, allowSensitive: Boolean): Extraction =
    extraction.copy(
        concepts = extraction.concepts.filter { (_, label) ->
            !hasDigit(label) && (allowSensitive || !isSensitiveText(label, cfg.sensitiveWords))
        },
    )

// ---- zmiany w grafie -------------------------------------------------------------------------------

fun linkPair(a: String, b: String): Pair<String, String> = if (a < b) a to b else b to a

/** Nauka: nowe pojęcia dostają `initial`, znane rosną o `gainConcept`, każda para dostaje połączenie. */
fun planLearn(
    extraction: Extraction,
    known: Map<String, Concept>,
    links: Map<Pair<String, String>, Link>,
    now: Instant,
    cfg: MemoryConfig,
    domain: String? = null,
): LearnPlan {
    val d = when {
        domain != null && domain in MEMORY_DOMAIN_IDS -> domain
        extraction.domain in MEMORY_DOMAIN_IDS -> extraction.domain
        else -> DEFAULT_MEMORY_DOMAIN
    }
    val concepts = mutableListOf<Concept>()
    val fresh = mutableListOf<String>()
    val reinforced = mutableListOf<String>()
    val keys = mutableListOf<String>()
    for ((key, label) in extraction.concepts) {
        if (key.isEmpty() || key in keys) continue
        keys += key
        val old = known[key]
        if (old == null) {
            concepts += Concept(key, cpTake(label, MAX_LABEL_CHARS), cfg.initial, now, 1, now, mapOf(d to 1))
            fresh += label
        } else {
            val domains = old.domains.toMutableMap().also { it[d] = (it[d] ?: 0) + 1 }
            val e = conceptStrength(old, now, cfg)
            concepts += old.copy(strength = towardOne(e, cfg.gainConcept), lastUsed = now, hits = old.hits + 1, domains = domains)
            reinforced += label
        }
    }
    val newLinks = mutableListOf<Link>()
    for (i in keys.indices) for (j in i + 1 until keys.size) {
        val (a, b) = linkPair(keys[i], keys[j])
        val old = links[a to b]
        newLinks += if (old == null) Link(a, b, cfg.gainLink, now) else Link(a, b, towardOne(linkStrength(old, now, cfg), cfg.gainLink), now)
    }
    return LearnPlan(concepts, newLinks, fresh, reinforced, d)
}

/** `round(x, 4)` z Pythona: zaokrąglenie dokładnej wartości binarnej, remisy do parzystej. */
internal fun roundTo4(x: Double): Double = BigDecimal(x).setScale(4, RoundingMode.HALF_EVEN).toDouble()

/**
 * Pojęcia o podanych kluczach i ich najmocniej połączeni sąsiedzi; pasujące są wzmacniane.
 * Zwraca (wyniki malejąco po wyniku, przy remisie po kluczu; wzmocnione pojęcia do zapisania).
 */
fun planRecall(
    keys: List<String>,
    known: Map<String, Concept>,
    links: Iterable<Link>,
    now: Instant,
    cfg: MemoryConfig,
    k: Int = cfg.recallK,
): Pair<List<RecallHit>, List<Concept>> {
    val wanted = keys.filter { it.isNotEmpty() }.distinct()
    val matches = wanted.mapNotNull { known[it] }
    val matchKeys = matches.map { it.key }.toSet()
    val scores = LinkedHashMap<String, Triple<Double, String, String>>()
    for (c in matches) scores[c.key] = Triple(conceptStrength(c, now, cfg), c.label, "match")
    for (link in links) {
        for ((self, other) in listOf(link.a to link.b, link.b to link.a)) {
            if (self !in matchKeys || other in matchKeys) continue
            val neighbour = known[other] ?: continue
            val score = linkStrength(link, now, cfg) * conceptStrength(neighbour, now, cfg)
            val prev = scores[other]
            if (prev == null || prev.first < score) scores[other] = Triple(score, neighbour.label, "link")
        }
    }
    val ranked = scores.entries.sortedWith(compareBy<Map.Entry<String, Triple<Double, String, String>>> { -it.value.first }.thenBy { it.key }).take(k)
    val hits = ranked.map { RecallHit(it.key, it.value.second, roundTo4(it.value.first), it.value.third) }
    val reinforced = matches.map { it.copy(strength = towardOne(conceptStrength(it, now, cfg), cfg.gainRecall), lastUsed = now) }
    return hits to reinforced
}

/** Klucze pojęć i pary połączeń o sile bieżącej poniżej `forgetBelow`. */
fun planPrune(concepts: Iterable<Concept>, links: Iterable<Link>, now: Instant, cfg: MemoryConfig): Pair<List<String>, List<Pair<String, String>>> {
    val dead = concepts.filter { conceptStrength(it, now, cfg) < cfg.forgetBelow }.map { it.key }.sorted()
    val weak = links.filter { linkStrength(it, now, cfg) < cfg.forgetBelow }.map { it.a to it.b }
        .sortedWith(compareBy<Pair<String, String>> { it.first }.thenBy { it.second })
    return dead to weak
}

fun planPraise(concepts: Iterable<Concept>, now: Instant, cfg: MemoryConfig): List<Concept> =
    concepts.map { it.copy(strength = towardOne(conceptStrength(it, now, cfg), cfg.gainRecall), lastUsed = now) }

/** Krytyka: siła bieżąca razy (1 − feedbackDown); poniżej progu pojęcie jest usuwane. */
fun planCriticism(concepts: Iterable<Concept>, now: Instant, cfg: MemoryConfig): Pair<List<Concept>, List<String>> {
    val kept = mutableListOf<Concept>()
    val dead = mutableListOf<String>()
    for (c in concepts) {
        val s = conceptStrength(c, now, cfg) * (1.0 - cfg.feedbackDown)
        if (s < cfg.forgetBelow) dead += c.key else kept += c.copy(strength = s, lastUsed = now)
    }
    return kept to dead
}

/** [n] pojęć o największej sile bieżącej (przy remisie po kluczu). */
fun strongest(concepts: Iterable<Concept>, now: Instant, cfg: MemoryConfig, n: Int): List<Concept> =
    concepts.sortedWith(compareBy<Concept> { -conceptStrength(it, now, cfg) }.thenBy { it.key }).take(n)

/** Domena, w której pojęcie padło najczęściej (przy remisie wcześniejsza na liście). */
fun mainDomain(c: Concept): String {
    var best = DEFAULT_MEMORY_DOMAIN
    var most = 0
    for (d in MEMORY_DOMAIN_IDS) {
        val n = c.domains[d] ?: 0
        if (n > most) {
            best = d
            most = n
        }
    }
    return best
}

// ---- polecenia i reakcje ---------------------------------------------------------------------------

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
    PREFIX + "(?:co (?:o mnie wiesz|wiesz o mnie|pamiętasz)|czego się o mnie nauczył(?:aś|eś))$",
    RegexOption.IGNORE_CASE,
)
private val IRIS = Regex("^iris[ ,]+")

// Reakcje to cała wypowiedź; dłuższe zdanie z takim słowem na początku ("źle się czuję") to zwykła rozmowa.
private val PRAISE = setOf("dobrze", "świetnie", "dobra odpowiedź", "trafnie", "dokładnie tak", "dzięki", "dziękuję")
private val CRITICISM = setOf("źle", "to nie tak", "nie tak", "nie o to pytałem", "nie o to chodziło", "to nieprawda", "mylisz się")

private val YES = Regex("^(?:tak|zgadza się|dokładnie|owszem)$NOT_WORD", RegexOption.IGNORE_CASE)
private val NO = Regex("^(?:nie zgadza się|nieprawda|nie)$NOT_WORD", RegexOption.IGNORE_CASE)

enum class MemoryKind { REMEMBER, FORGET, FORGET_ALL, RECALL }

data class MemoryCommand(val kind: MemoryKind, val text: String = "")

enum class Feedback { PRAISE, CRITICISM }

private fun clean(text: String): String = pyStrip(rstripChars(pyStrip(collapseSpaces(pyStrip(text))), ".!?…"))

/** Polecenie pamięci albo null. Tekst polecenia zachowuje wielkość liter wypowiedzi. */
fun parseCommand(text: String): MemoryCommand? {
    val t = pyStrip(rstripChars(clean(text), "?"))
    if (RECALL.containsMatchIn(t)) return MemoryCommand(MemoryKind.RECALL)
    if (FORGET_ALL.containsMatchIn(t)) return MemoryCommand(MemoryKind.FORGET_ALL)
    for ((kind, pattern) in listOf(MemoryKind.REMEMBER to REMEMBER, MemoryKind.FORGET to FORGET)) {
        val m = pattern.find(t) ?: continue
        val x = pyStrip(stripChars(clean(m.groups["x"]!!.value), "\"'„”«» "))
        return if (cpLength(x) >= MIN_COMMAND_CHARS) MemoryCommand(kind, cpTake(x, MAX_COMMAND_CHARS)) else null
    }
    return null
}

/** Odpowiedź na pytanie z "zapomnij wszystko": true ("tak"), false ("nie"), null (inna wypowiedź). */
fun parseYesNo(text: String): Boolean? {
    val t = clean(text)
    return when {
        YES.containsMatchIn(t) -> true
        NO.containsMatchIn(t) -> false
        else -> null
    }
}

/** Pochwała albo krytyka ostatniej odpowiedzi, inaczej null. */
fun parseFeedback(text: String): Feedback? {
    val t = stripChars(IRIS.replace(pyLower(clean(text)), ""), " ,;:")
    return when (t) {
        in PRAISE -> Feedback.PRAISE
        in CRITICISM -> Feedback.CRITICISM
        else -> null
    }
}

// ---- teksty odpowiedzi -----------------------------------------------------------------------------

internal fun plural(n: Int, one: String, few: String, many: String): String = when {
    n == 1 -> one
    n % 10 in 2..4 && n % 100 !in 12..14 -> few
    else -> many
}

fun conceptsCount(n: Int): String = "$n ${plural(n, "pojęcie", "pojęcia", "pojęć")}"

fun rememberText(labels: List<String>): String =
    if (labels.isEmpty()) "Nie znalazłem w tym zdaniu niczego do zapamiętania." else "Zapamiętane: ${labels.joinToString(", ")}."

fun forgetText(labels: List<String>): String =
    if (labels.isEmpty()) "Nie pamiętam takiego pojęcia." else "Zapomniane: ${labels.joinToString(", ")}."

fun forgetAllQuestion(n: Int): String =
    if (n == 0) "Nic nie pamiętam, nie ma czego usuwać." else "Na pewno usunąć całą pamięć (${conceptsCount(n)})? Powiedz tak albo nie."

const val FORGET_ALL_DONE = "Pamięć wyczyszczona."
const val FORGET_ALL_KEPT = "Dobrze, niczego nie usuwam."
const val STORE_FAILED_TEXT = "Nie udało się zapisać w pamięci. Spróbuj ponownie."
const val PRAISE_TEXT = "Dobrze, wzmacniam pojęcia użyte w tej odpowiedzi."
const val CRITICISM_TEXT = "Dobrze, osłabiam pojęcia użyte w tej odpowiedzi."

/** Odpowiedź na "co o mnie wiesz": najsilniejsze pojęcia i ich łączna liczba. */
fun recallText(topLabels: List<String>, total: Int): String =
    if (total == 0) {
        "Jeszcze nic nie pamiętam. Możesz powiedzieć: zapamiętaj, że ..."
    } else {
        "Najmocniej kojarzę: ${topLabels.joinToString(", ")}. Razem pamiętam ${conceptsCount(total)}."
    }

/** Liczba 0-1 z dwoma miejscami po przecinku, połówki w górę, liczona na liczbach całkowitych jak w Pythonie. */
fun decimalPl(x: Double): String {
    val n = BigDecimal(x * 10000).setScale(0, RoundingMode.HALF_EVEN).toLong() // round() Pythona
    val hundredths = Math.floorDiv(n + 50, 100)
    return "${Math.floorDiv(hundredths, 100)},${"%02d".format(java.util.Locale.ROOT, Math.floorMod(hundredths, 100))}"
}

/** Wynik narzędzia `iris_recall` dla modelu: jeden wiersz na pojęcie. */
fun toolRecallText(hits: List<RecallHit>): String =
    if (hits.isEmpty()) {
        "Nic na ten temat nie pamiętam."
    } else {
        hits.joinToString("\n") { "${it.label} (siła ${decimalPl(it.score)}, ${if (it.via == "match") "wprost" else "powiązane"})" }
    }

fun toolRememberText(fresh: List<String>, reinforced: List<String>): String {
    val parts = mutableListOf<String>()
    if (fresh.isNotEmpty()) parts += "nowe: " + fresh.joinToString(", ")
    if (reinforced.isNotEmpty()) parts += "wzmocnione: " + reinforced.joinToString(", ")
    return parts.joinToString("; ").ifEmpty { "Nic do zapamiętania." }
}
