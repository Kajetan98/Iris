package pl.iris.core

import kotlinx.coroutines.delay

/** Interfejs silnika LLM (docs/spec.md, sekcja 12). Silniki są wymienne. */

enum class Role(val wire: String) { USER("user"), ASSISTANT("assistant") }

data class Message(val role: Role, val content: String)

data class Usage(val inputTokens: Int? = null, val outputTokens: Int? = null)

enum class Finish { STOP, LENGTH, REFUSAL, OTHER }

data class LlmReply(val text: String, val finish: Finish, val model: String, val usage: Usage = Usage())

/** Błąd silnika. Wywołujący nie dostaje odpowiedzi, rdzeń działa dalej. */
open class LlmException(message: String) : Exception(message)

/** Brak połączenia, limit czasu, 429 lub 5xx. Można ponowić. */
class LlmUnavailable(message: String) : LlmException(message)

/** Zła konfiguracja: brak klucza, nieznany model lub brak uprawnień. Ponowienie nie pomoże. */
class LlmConfigError(message: String) : LlmException(message)

/** Silnik odmówił odpowiedzi. */
class LlmRefusal(message: String) : LlmException(message)

interface LlmEngine {
    /** Prawda, gdy dane opuszczają urządzenie (decyduje o polityce faktów). */
    val remote: Boolean

    suspend fun complete(system: String, messages: List<Message>, maxTokens: Int): LlmReply
}

/** Ponawia tylko [LlmUnavailable], z opóźnieniem rosnącym dwukrotnie. */
suspend fun <T> withRetries(retries: Int, baseDelayMs: Long = 500, block: suspend () -> T): T {
    var attempt = 0
    while (true) {
        try {
            return block()
        } catch (e: LlmUnavailable) {
            if (attempt >= retries) throw e
            delay(baseDelayMs shl attempt)
            attempt++
        }
    }
}
