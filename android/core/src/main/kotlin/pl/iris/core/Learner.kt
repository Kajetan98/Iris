package pl.iris.core

import java.time.Duration

/**
 * Uczenie się w turach rozmowy (docs/spec.md, sekcja 9, "Uczenie się"). Port host/api/learner.py bez
 * zdarzeń `fact.*`: wersja na telefon nie ma dziennika zdarzeń. Treść faktów jest tylko w magazynie.
 *
 * Klasa nie jest bezpieczna wątkowo; [Chat] serializuje wywołania.
 */
class Learner(
    private val cfg: LearningConfig,
    private val minInferredConfidence: Double,
    private val quiet: Boolean,
    private val clock: Clock,
    private val store: FactStore,
) {
    private enum class PendingKind { FACT, FORGET_ALL }

    private class Pending(val kind: PendingKind, val until: Double, val factId: Long? = null)

    private var pending: Pending? = null
    private var lastQuestion: Double? = null
    private var lastDecay: Double? = null

    val enabled: Boolean get() = cfg.enabled

    /** Odpowiedź na otwarte pytanie IRIS albo null. Każda wypowiedź zamyka pytanie. */
    fun answer(text: String): String? {
        val open = pending
        pending = null
        if (!enabled || open == null || clock.monotonic() > open.until) return null
        val parsed = parseAnswer(text)
        val yes = parsed.yes ?: return null
        if (open.kind == PendingKind.FORGET_ALL) {
            if (!yes) return "Dobrze, niczego nie usuwam."
            store.deleteAllFacts()
            return "Dobrze, usuwam wszystko, co o tobie wiem."
        }
        val fact = open.factId?.let { store.getFact(it) } ?: return "Dobrze."
        if (yes) {
            store.updateFact(fact.id, source = Source.STATED, confidence = 1.0)
            return "Dobrze, zapamiętane."
        }
        store.deleteFact(fact.id)
        if (parsed.correction.isNotEmpty()) {
            val added = remember(parsed.correction)
            return "Dobrze, poprawione: \"${added?.text ?: parsed.correction}\"."
        }
        return "Dobrze, usuwam to przypuszczenie."
    }

    /** Polecenie "zapamiętaj", "zapomnij", "zapomnij wszystko" lub "co o mnie wiesz". */
    fun command(text: String): String? {
        if (!enabled) return null
        val cmd = parseMemory(text) ?: return null
        return when (cmd.kind) {
            MemoryKind.REMEMBER -> "Zapamiętane: \"${remember(cmd.text)?.text ?: cmd.text}\"."
            MemoryKind.FORGET -> forgetByText(cmd.text)
            MemoryKind.FORGET_ALL -> askToForgetAll()
            MemoryKind.RECALL -> recallText(store.facts(), minInferredConfidence)
        }
    }

    private fun forgetByText(text: String): String {
        val match = bestMatch(text, store.facts(), cfg.forgetMatch) ?: return "Nie znam takiej informacji."
        store.deleteFact(match.id)
        return "Zapomniane: \"${match.text}\"."
    }

    private fun askToForgetAll(): String {
        val count = store.countFacts()
        if (count == 0) return "Nic o tobie nie wiem, nie ma czego usuwać."
        pending = Pending(PendingKind.FORGET_ALL, clock.monotonic() + cfg.answerWindowS)
        val word = plural(count, "informacja", "informacje", "informacji")
        return "Na pewno usunąć wszystko, co o tobie wiem ($count $word)? Powiedz tak albo nie."
    }

    /** Fakt podany wprost; podobny istniejący fakt zostaje zastąpiony nowym tekstem. */
    private fun remember(text: String): Fact? {
        val match = bestMatch(text, store.facts(), cfg.similar)
        return if (match != null) {
            store.updateFact(match.id, text = text, source = Source.STATED, confidence = 1.0)
        } else {
            store.addFact(text, Source.STATED)
        }
    }

    /** Usunięcie przez użytkownika (lista w aplikacji). */
    fun forget(id: Long): Boolean = store.deleteFact(id)

    /** Pytanie o przypuszczenie, dopisywane do odpowiedzi, albo null. */
    fun question(): String? {
        if (!(enabled && cfg.ask) || quiet || pending != null) return null
        val now = clock.monotonic()
        val last = lastQuestion
        if (last != null && now - last < cfg.askIntervalS) return null
        val fact = pickQuestion(store.facts(), clock.now(), cfg) ?: return null
        store.markFactAsked(fact.id)
        lastQuestion = now
        pending = Pending(PendingKind.FACT, now + cfg.answerWindowS, fact.id)
        return questionFor(fact)
    }

    fun infersWith(engine: LlmEngine): Boolean =
        enabled && cfg.maxPerTurn > 0 &&
            (cfg.infer == InferPolicy.ALWAYS || (cfg.infer == InferPolicy.LOCAL_ONLY && !engine.remote))

    /** Propozycje faktów z wypowiedzi. Błąd silnika zgłaszany jako null, bez treści (spec, sekcja 9). */
    suspend fun infer(engine: LlmEngine, utterance: String): List<Guess>? = try {
        inferFacts(engine, utterance, cfg.maxPerTurn)
    } catch (_: LlmException) {
        null
    } catch (_: LearnError) {
        null
    }

    /** Wnioski do pamięci: wrażliwe i niepewne odpada, podobne wzmacnia, nowe zaczynają słabo. */
    fun merge(guesses: List<Guess>) {
        for (g in guesses) {
            if (g.confidence < cfg.forgetBelow || isSensitive(g.text, cfg.sensitiveWords)) continue
            val match = bestMatch(g.text, store.facts(), cfg.similar)
            if (match != null && match.source == Source.STATED) continue
            if (match != null) {
                store.reinforceFact(match.id, cfg.reinforceStep, cfg.maxInferred)
            } else {
                store.addFact(g.text, Source.INFERRED, minOf(g.confidence, cfg.startMax))
            }
        }
    }

    /** Raz na dobę (i przy pierwszym wywołaniu): słabną przypuszczenia bez zmian od decayAfterDays. */
    fun maybeDecay() {
        val now = clock.monotonic()
        val last = lastDecay
        if (!enabled || (last != null && now - last < DAY_S)) return
        lastDecay = now
        val older = clock.now().minus(Duration.ofMillis(Math.round(cfg.decayAfterDays * 86_400_000.0)))
        store.decayFacts(older, cfg.decayStep, cfg.forgetBelow)
    }

    private companion object {
        const val DAY_S = 86_400.0
    }
}
