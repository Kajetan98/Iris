package pl.iris.core

import java.time.LocalDateTime
import java.time.ZoneId
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** Rozmowa widziana z interfejsu: tura, lista faktów i usuwanie faktu. */
interface Conversation {
    suspend fun say(text: String): ChatReply

    suspend fun facts(): List<Fact>

    suspend fun forget(id: Long): Boolean
}

/** Odpowiedź tury rozmowy. `model` jest null dla odpowiedzi programu bez LLM (polecenia pamięci). */
data class ChatReply(val text: String, val truncated: Boolean, val model: String?, val usage: Usage)

/** Rozmowa wymaga silnika LLM, a go nie ma (brak klucza w ustawieniach). */
class ChatDisabled : Exception("model językowy jest wyłączony")

enum class ChatErrorKind { REFUSED, UNAVAILABLE, CONFIG, FAILED }

/** Błąd tury rozmowy. Historia i fakty zostają bez zmian. */
class ChatError(val kind: ChatErrorKind, message: String) : Exception(message)

/**
 * Jedna tura rozmowy (kolejność jak w HostService.chat, spec sekcje 9 i 14): odpowiedź na pytanie IRIS,
 * polecenia pamięci, LLM, pytanie o przypuszczenie, wnioski w tle. Tury są obsługiwane po kolei.
 */
class Chat(
    config: CoreConfig,
    private val clock: Clock,
    private val store: FactStore,
    private val assistant: Assistant?,
    private val scope: CoroutineScope,
    private val zone: ZoneId = ZoneId.systemDefault(),
) : Conversation {
    private val learner = Learner(config.learning, config.llm.minInferredConfidence, config.quiet, clock, store)
    private val lock = Mutex()
    private val background = mutableListOf<Job>()

    override suspend fun say(text: String): ChatReply {
        val utterance = pyStrip(text)
        require(utterance.isNotEmpty()) { "pusta wypowiedź" }
        lock.withLock {
            learner.maybeDecay() // raz na dobę, przy pierwszej turze po starcie też
            (learner.answer(utterance) ?: learner.command(utterance))?.let { return ChatReply(it, false, null, Usage(0, 0)) }
        }
        val engine = assistant ?: throw ChatDisabled()
        val reply = lock.withLock {
            val answer = try {
                engine.ask(utterance, store.facts(), LocalDateTime.ofInstant(clock.now(), zone))
            } catch (e: LlmRefusal) {
                throw ChatError(ChatErrorKind.REFUSED, e.message.orEmpty())
            } catch (e: LlmUnavailable) {
                throw ChatError(ChatErrorKind.UNAVAILABLE, e.message.orEmpty())
            } catch (e: LlmConfigError) {
                throw ChatError(ChatErrorKind.CONFIG, e.message.orEmpty())
            } catch (e: LlmException) {
                throw ChatError(ChatErrorKind.FAILED, e.message.orEmpty())
            }
            val question = learner.question() // pytanie o przypuszczenie (spec, sekcja 9)
            ChatReply(if (question == null) answer.text else "${answer.text} $question", answer.truncated, answer.model, answer.usage)
        }
        if (learner.infersWith(engine.engine)) {
            val job = scope.launch { learnFrom(engine.engine, utterance) }
            synchronized(background) { background += job }
        }
        return reply
    }

    /** Usunięcie faktu z listy w aplikacji. */
    override suspend fun forget(id: Long): Boolean = lock.withLock { learner.forget(id) }

    /** Lista faktów dla ekranu "Co IRIS o mnie wie". */
    override suspend fun facts(): List<Fact> = lock.withLock { store.facts() }

    /** Czeka na wnioski z rozmowy liczone w tle (testy, zamykanie aplikacji). */
    suspend fun idle() {
        val jobs = synchronized(background) { background.toList().also { background.clear() } }
        jobs.joinAll()
    }

    private suspend fun learnFrom(engine: LlmEngine, utterance: String) {
        val guesses = learner.infer(engine, utterance) ?: return // błąd silnika pomijamy bez treści
        lock.withLock { learner.merge(guesses) }
    }
}
