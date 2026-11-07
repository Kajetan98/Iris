package pl.iris.core

import kotlinx.coroutines.delay
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/** Interfejs silnika LLM (docs/spec.md, sekcja 12). Silniki są wymienne. */

enum class Role(val wire: String) { USER("user"), ASSISTANT("assistant") }

/** Narzędzie dla modelu (spec, sekcja 12): nazwa, opis i schemat JSON argumentów. */
data class ToolSpec(val name: String, val description: String, val parameters: JsonObject)

data class ToolCall(val id: String, val name: String, val arguments: JsonObject)

data class ToolResult(val callId: String, val name: String, val content: String, val isError: Boolean = false)

/**
 * Wiadomość rozmowy. W rundzie narzędzi asystent ma [toolCalls] i [raw] (treść odpowiedzi silnika do odesłania
 * bez zmian, np. z blokami myślenia Claude), a następna wiadomość użytkownika [toolResults].
 */
data class Message(
    val role: Role,
    val content: String,
    val toolCalls: List<ToolCall> = emptyList(),
    val toolResults: List<ToolResult> = emptyList(),
    val raw: JsonElement? = null,
)

data class Usage(val inputTokens: Int? = null, val outputTokens: Int? = null)

enum class Finish { STOP, LENGTH, REFUSAL, TOOL_USE, OTHER }

data class LlmReply(
    val text: String,
    val finish: Finish,
    val model: String,
    val usage: Usage = Usage(),
    val toolCalls: List<ToolCall> = emptyList(),
    val raw: JsonElement? = null,
)

/** Błąd silnika. Wywołujący nie dostaje odpowiedzi, rdzeń działa dalej. */
open class LlmException(message: String) : Exception(message)

/** Brak połączenia, limit czasu, 429 lub 5xx. Można ponowić. */
class LlmUnavailable(message: String) : LlmException(message)

/** Zła konfiguracja: brak klucza, nieznany model lub brak uprawnień. Ponowienie nie pomoże. */
class LlmConfigError(message: String) : LlmException(message)

/** Silnik odmówił odpowiedzi. */
class LlmRefusal(message: String) : LlmException(message)

interface LlmEngine {
    /** Prawda, gdy dane opuszczają urządzenie (decyduje o polityce pamięci). */
    val remote: Boolean

    /** Silnik przyjmuje narzędzia (Claude); LiteRT-LM ich nie ma, więc pamięć dostaje w bloku kontekstu. */
    val supportsTools: Boolean get() = false

    suspend fun complete(system: String, messages: List<Message>, maxTokens: Int): LlmReply

    /** Zapytanie z narzędziami; [allowCalls] false zabrania wywołań (ostatnia runda, spec sekcja 12). */
    suspend fun complete(system: String, messages: List<Message>, maxTokens: Int, tools: List<ToolSpec>, allowCalls: Boolean): LlmReply =
        complete(system, messages, maxTokens)
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
