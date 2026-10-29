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

/**
 * Wiersze tabeli `facts`: najprostsza warstwa trwałości. Reguły (zmiana tekstu, wzmacnianie, zanikanie)
 * są w [RowFactStore], więc baza (SQLite na telefonie) musi tylko przechowywać wiersze.
 * Identyfikatory rosną i nie są ponownie używane po usunięciu (jak `AUTOINCREMENT`).
 */
interface FactRows {
    /** Wszystkie wiersze rosnąco po id. */
    fun all(): List<Fact>

    fun get(id: Long): Fact?

    /** Nowy wiersz z kolejnym id. */
    fun insert(text: String, source: Source, confidence: Double, at: Instant): Fact

    /** Zastępuje istniejący wiersz o tym samym id. */
    fun save(fact: Fact)

    fun delete(id: Long): Boolean

    fun deleteAll(): Int

    fun count(): Int
}

/** Wiersze w pamięci (testy). */
class InMemoryFactRows : FactRows {
    private val rows = LinkedHashMap<Long, Fact>()
    private var nextId = 1L

    override fun all(): List<Fact> = rows.values.sortedBy { it.id }

    override fun get(id: Long): Fact? = rows[id]

    override fun insert(text: String, source: Source, confidence: Double, at: Instant): Fact =
        Fact(nextId++, text, source, confidence, at, at).also { rows[it.id] = it }

    override fun save(fact: Fact) {
        require(fact.id in rows) { "brak wiersza ${fact.id}" }
        rows[fact.id] = fact
    }

    override fun delete(id: Long): Boolean = rows.remove(id) != null

    override fun deleteAll(): Int = rows.size.also { rows.clear() }

    override fun count(): Int = rows.size
}

/** Semantyka `Store` hosta (host/store/store.py) na dowolnych wierszach. */
class RowFactStore(private val clock: Clock, private val rows: FactRows) : FactStore {
    override fun facts(): List<Fact> = rows.all()

    override fun getFact(id: Long): Fact? = rows.get(id)

    override fun addFact(text: String, source: Source, confidence: Double?): Fact {
        val conf = when {
            confidence != null -> checkConfidence(confidence)
            source == Source.INFERRED -> throw IllegalArgumentException("fakt 'inferred' wymaga confidence")
            else -> 1.0
        }
        checkText(text)
        return rows.insert(text, source, conf, clock.now().toMillisPrecision())
    }

    override fun updateFact(id: Long, text: String?, source: Source?, confidence: Double?): Fact? {
        if (confidence != null) checkConfidence(confidence)
        if (text != null) checkText(text)
        val current = rows.get(id) ?: return null
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
        rows.save(updated)
        return updated
    }

    override fun reinforceFact(id: Long, step: Double, cap: Double): Fact? {
        val current = rows.get(id) ?: return null
        val conf = maxOf(current.confidence, minOf(cap, current.confidence + step))
        val updated = current.copy(
            confidence = checkConfidence(conf), reinforced = current.reinforced + 1,
            updatedAt = clock.now().toMillisPrecision(),
        )
        rows.save(updated)
        return updated
    }

    override fun markFactAsked(id: Long): Fact? {
        val current = rows.get(id) ?: return null
        val updated = current.copy(askedAt = clock.now().toMillisPrecision())
        rows.save(updated)
        return updated
    }

    override fun decayFacts(olderThan: Instant, step: Double, forgetBelow: Double): Pair<List<Long>, List<Long>> {
        val now = clock.now().toMillisPrecision()
        val cutoff = olderThan.toMillisPrecision()
        val weakened = mutableListOf<Long>()
        val forgotten = mutableListOf<Long>()
        for (fact in rows.all()) {
            if (fact.source != Source.INFERRED || !fact.updatedAt.isBefore(cutoff)) continue
            val conf = roundTo6(maxOf(0.0, fact.confidence - step))
            if (conf < forgetBelow) {
                rows.delete(fact.id)
                forgotten += fact.id
            } else {
                rows.save(fact.copy(confidence = conf, updatedAt = now))
                weakened += fact.id
            }
        }
        return weakened to forgotten
    }

    override fun deleteAllFacts(): Int = rows.deleteAll()

    override fun deleteFact(id: Long): Boolean = rows.delete(id)

    override fun countFacts(): Int = rows.count()
}

/** Magazyn w pamięci: wzorzec zachowania dla testów. */
class InMemoryFactStore(clock: Clock) : FactStore by RowFactStore(clock, InMemoryFactRows())

/** `round(x, 6)` z Pythona: zaokrąglenie dokładnej wartości binarnej, remisy do parzystej. */
internal fun roundTo6(x: Double): Double =
    java.math.BigDecimal(x).setScale(6, java.math.RoundingMode.HALF_EVEN).toDouble()
