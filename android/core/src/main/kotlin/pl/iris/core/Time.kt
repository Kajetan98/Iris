package pl.iris.core

import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit
import java.util.Locale

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

/** Czas w bazie: UTC, ISO 8601 ze stałą szerokością (`2026-03-01T12:00:00.000+00:00`), jak w sekcji 9 specyfikacji. */
object IsoTime {
    private val FORMAT = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSSxxx", Locale.ROOT).withZone(ZoneOffset.UTC)

    fun format(instant: Instant): String = FORMAT.format(instant)

    fun parse(text: String): Instant = OffsetDateTime.parse(text).toInstant()
}

internal fun Instant.toMillisPrecision(): Instant = truncatedTo(ChronoUnit.MILLIS)
