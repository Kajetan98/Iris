package pl.iris.core

import java.time.LocalDateTime
import java.time.ZoneId
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** Rozmowa widziana z interfejsu: tura, stan pamięci i usuwanie pojęcia. */
interface Conversation {
    suspend fun say(text: String): ChatReply

    suspend fun memory(): MemoryView

    suspend fun forget(key: String): Boolean
}

/** Odpowiedź tury rozmowy. `model` jest null dla odpowiedzi programu bez LLM (polecenia pamięci). */
data class ChatReply(val text: String, val truncated: Boolean, val model: String?, val usage: Usage)

/** Rozmowa wymaga silnika LLM, a go nie ma (brak klucza albo modelu w ustawieniach). */
class ChatDisabled : Exception("model językowy jest wyłączony")

enum class ChatErrorKind { REFUSED, UNAVAILABLE, CONFIG, FAILED }

/** Błąd tury rozmowy. Historia i pamięć zostają bez zmian. */
class ChatError(val kind: ChatErrorKind, message: String) : Exception(message)

/**
 * Jedna tura rozmowy (kolejność jak w HostService.chat, spec sekcje 9 i 14): odpowiedź na pytanie
 * z "zapomnij wszystko", polecenia pamięci i reakcje, LLM z narzędziami pamięci, nauka z wypowiedzi w tle
 * (tura na nią nie czeka). Tury są obsługiwane po kolei.
 */
class Chat(
    private val config: CoreConfig,
    private val clock: Clock,
    store: MemoryStore,
    private val assistant: Assistant?,
    private val scope: CoroutineScope,
    private val zone: ZoneId = ZoneId.systemDefault(),
) : Conversation {
    private val memory = MemoryKeeper(config.memory, config.llm, clock, store)
    private val lock = Mutex()
    private val background = mutableListOf<Job>()

    override suspend fun say(text: String): ChatReply {
        val utterance = pyStrip(text)
        require(utterance.isNotEmpty()) { "pusta wypowiedź" }
        lock.withLock {
            memory.maybePrune() // przy pierwszej turze i co memory.pruneEveryS
            (memory.answer(utterance) ?: memory.command(utterance))?.let { return ChatReply(it, false, null, Usage(0, 0)) }
        }
        val engine = assistant ?: throw ChatDisabled()
        val reply = lock.withLock {
            val tools = if (memory.enabled) memory.tools() else null
            val answer = try {
                engine.ask(utterance, LocalDateTime.ofInstant(clock.now(), zone), tools)
            } catch (e: LlmRefusal) {
                throw ChatError(ChatErrorKind.REFUSED, e.message.orEmpty())
            } catch (e: LlmUnavailable) {
                throw ChatError(ChatErrorKind.UNAVAILABLE, e.message.orEmpty())
            } catch (e: LlmConfigError) {
                throw ChatError(ChatErrorKind.CONFIG, e.message.orEmpty())
            } catch (e: LlmException) {
                throw ChatError(ChatErrorKind.FAILED, e.message.orEmpty())
            }
            // Pochwała lub krytyka dotyczy pojęć przypomnianych do tej odpowiedzi (spec, sekcja 9).
            memory.noteRecalled(tools?.recalled.orEmpty())
            ChatReply(answer.text, answer.truncated, answer.model, answer.usage)
        }
        if (memory.learnable(utterance)) {
            val job = scope.launch { learnFrom(engine.engine, utterance) }
            synchronized(background) { background += job }
        }
        return reply
    }

    /** Usunięcie pojęcia z ekranu pamięci. */
    override suspend fun forget(key: String): Boolean = lock.withLock { memory.forget(listOf(key)).isNotEmpty() }

    /** Stan pamięci dla ekranu "Pamięć IRIS" i eksportu. */
    override suspend fun memory(): MemoryView = lock.withLock { memory.view() }

    /** Czeka na naukę z rozmowy liczoną w tle (testy, zamykanie aplikacji). */
    suspend fun idle() {
        val jobs = synchronized(background) { background.toList().also { background.clear() } }
        jobs.joinAll()
    }

    private suspend fun learnFrom(engine: LlmEngine, utterance: String) {
        val ext = memory.extract(engine, utterance) // zapytanie poza blokadą: tura się nie wstrzymuje
        lock.withLock { memory.learnConversation(ext) }
    }
}
