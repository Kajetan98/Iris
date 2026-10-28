package pl.iris.core

import java.time.Instant
import java.time.temporal.ChronoUnit

enum class Source(val wire: String) {
    STATED("stated"),
    INFERRED("inferred"),
}

/** Fakt o użytkowniku (spec, sekcja 9). Czas z dokładnością do milisekundy, jak w bazie hosta. */
data class Fact(
    val id: Long,
    val text: String,
    val source: Source,
    val confidence: Double,
    val createdAt: Instant,
    val updatedAt: Instant,
    val reinforced: Int = 0,
    val askedAt: Instant? = null,
)

interface Clock {
    /** Sekundy z zegara monotonicznego. */
    fun monotonic(): Double

    fun now(): Instant
}

object SystemClock : Clock {
    override fun monotonic(): Double = System.nanoTime() / 1e9

    override fun now(): Instant = Instant.now()
}

/** Zegar sterowany ręcznie, oba odczyty przesuwają się razem (testy). */
class ManualClock(private var wall: Instant = Instant.parse("2026-03-01T12:00:00Z"), private var mono: Double = 0.0) : Clock {
    override fun monotonic(): Double = mono

    override fun now(): Instant = wall

    fun advance(seconds: Double) {
        mono += seconds
        wall = wall.plusMillis(Math.round(seconds * 1000))
    }
}

internal fun Instant.toMillisPrecision(): Instant = truncatedTo(ChronoUnit.MILLIS)

/**
 * Magazyn faktów. Semantyka jak `Store` hosta (host/store/store.py). Błąd wejścia-wyjścia
 * jest rzucany do wywołującego; reguły w [Learner] nie wymagają innego zachowania.
 */
interface FactStore {
    fun facts(): List<Fact>

    fun getFact(id: Long): Fact?

    /** Fakt `inferred` wymaga [confidence]; `stated` bez niej dostaje 1. */
    fun addFact(text: String, source: Source, confidence: Double? = null): Fact

    /**
     * Zmiana tekstu bez jawnego [source] i [confidence] oznacza wypowiedź użytkownika:
     * `stated` i pewność 1. Zwraca null, gdy faktu nie ma.
     */
    fun updateFact(id: Long, text: String? = null, source: Source? = null, confidence: Double? = null): Fact?

    /** Powtórzony wniosek: pewność rośnie o [step] (najwyżej [cap]), licznik o 1. */
    fun reinforceFact(id: Long, step: Double, cap: Double): Fact?

    /** Zapisuje pytanie o fakt. Nie odświeża `updatedAt`, więc pytanie nie wstrzymuje zanikania. */
    fun markFactAsked(id: Long): Fact?

    /** Zanikanie przypuszczeń niezmienionych od [olderThan]. Zwraca (osłabione, usunięte). */
    fun decayFacts(olderThan: Instant, step: Double, forgetBelow: Double): Pair<List<Long>, List<Long>>

    fun deleteAllFacts(): Int

    fun deleteFact(id: Long): Boolean

    fun countFacts(): Int
}

internal fun checkConfidence(value: Double): Double {
    require(value in 0.0..1.0) { "confidence musi być w zakresie 0-1" }
    return value
}

internal fun checkText(text: String): String {
    require(text.isNotBlank()) { "text nie może być pusty" }
    return text
}

/** Magazyn w pamięci: wzorzec zachowania dla testów i dla wersji z bazą (identyfikatory nie wracają). */
class InMemoryFactStore(private val clock: Clock) : FactStore {
    private val rows = LinkedHashMap<Long, Fact>()
    private var nextId = 1L

    override fun facts(): List<Fact> = rows.values.sortedBy { it.id }

    override fun getFact(id: Long): Fact? = rows[id]

    override fun addFact(text: String, source: Source, confidence: Double?): Fact {
        val conf = when {
            confidence != null -> checkConfidence(confidence)
            source == Source.INFERRED -> throw IllegalArgumentException("fakt 'inferred' wymaga confidence")
            else -> 1.0
        }
        checkText(text)
        val now = clock.now().toMillisPrecision()
        val fact = Fact(nextId++, text, source, conf, now, now)
        rows[fact.id] = fact
        return fact
    }

    override fun updateFact(id: Long, text: String?, source: Source?, confidence: Double?): Fact? {
        if (confidence != null) checkConfidence(confidence)
        if (text != null) checkText(text)
        val current = rows[id] ?: return null
        var newSource = current.source
        var newConf = current.confidence
        if (text != null && text != current.text) {
            newSource = Source.STATED
            newConf = 1.0
        }
        if (source != null) newSource = source
        if (confidence != null) newConf = confidence
        val updated = current.copy(
            text = text ?: current.text, source = newSource, confidence = newConf,
            updatedAt = clock.now().toMillisPrecision(),
        )
        rows[id] = updated
        return updated
    }

    override fun reinforceFact(id: Long, step: Double, cap: Double): Fact? {
        val current = rows[id] ?: return null
        val conf = maxOf(current.confidence, minOf(cap, current.confidence + step))
        val updated = current.copy(
            confidence = checkConfidence(conf), reinforced = current.reinforced + 1,
            updatedAt = clock.now().toMillisPrecision(),
        )
        rows[id] = updated
        return updated
    }

    override fun markFactAsked(id: Long): Fact? {
        val current = rows[id] ?: return null
        val updated = current.copy(askedAt = clock.now().toMillisPrecision())
        rows[id] = updated
        return updated
    }

    override fun decayFacts(olderThan: Instant, step: Double, forgetBelow: Double): Pair<List<Long>, List<Long>> {
        val now = clock.now().toMillisPrecision()
        val cutoff = olderThan.toMillisPrecision()
        val weakened = mutableListOf<Long>()
        val forgotten = mutableListOf<Long>()
        for (fact in facts()) {
            if (fact.source != Source.INFERRED || !fact.updatedAt.isBefore(cutoff)) continue
            val conf = roundTo6(maxOf(0.0, fact.confidence - step))
            if (conf < forgetBelow) {
                rows.remove(fact.id)
                forgotten += fact.id
            } else {
                rows[fact.id] = fact.copy(confidence = conf, updatedAt = now)
                weakened += fact.id
            }
        }
        return weakened to forgotten
    }

    override fun deleteAllFacts(): Int = rows.size.also { rows.clear() }

    override fun deleteFact(id: Long): Boolean = rows.remove(id) != null

    override fun countFacts(): Int = rows.size
}

/** `round(x, 6)` z Pythona: zaokrąglenie dokładnej wartości binarnej, remisy do parzystej. */
internal fun roundTo6(x: Double): Double =
    java.math.BigDecimal(x).setScale(6, java.math.RoundingMode.HALF_EVEN).toDouble()
