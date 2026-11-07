package pl.iris.core

import java.time.LocalDateTime
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

data class Answer(val text: String, val truncated: Boolean, val model: String, val usage: Usage, val toolRounds: Int = 0)

/**
 * Fasada rozmowy: historia, kontekst, polityka pamięci, narzędzia i wywołanie silnika
 * (port host/brain/assistant.py, spec sekcja 12).
 */
class Assistant(val engine: LlmEngine, private val cfg: LlmConfig) {
    private var history: List<Message> = emptyList()
    private val lock = Mutex()

    // Model, który odrzucił narzędzia, do końca sesji dostaje pamięć w bloku kontekstu.
    private var toolsOk = engine.supportsTools

    fun history(): List<Message> = history

    fun reset() {
        history = emptyList()
    }

    /**
     * Odpowiedź na wypowiedź. [tools] to narzędzia pamięci tej tury (bez nich model nie dostaje pamięci).
     * Rzuca [LlmException] (w tym [LlmUnavailable], [LlmRefusal]); historia zmienia się tylko po sukcesie i dostaje
     * tylko wypowiedź i końcową odpowiedź, bez wywołań i wyników narzędzi.
     */
    suspend fun ask(text: String, now: LocalDateTime? = null, tools: ToolBox? = null): Answer {
        val utterance = text.trim()
        require(utterance.isNotEmpty()) { "pusta wypowiedź" }
        val remote = engine.remote
        return lock.withLock {
            val specs = if (tools == null || !cfg.tools || !toolsOk || cfg.maxToolRounds == 0) emptyList() else tools.specs(remote)
            val memory = if (tools == null || specs.any { it.name == RECALL_TOOL.name }) emptyList() else tools.context(utterance, remote)
            var messages = history + Message(Role.USER, renderUserTurn(utterance, now, memory))
            var rounds = 0
            var reply: LlmReply
            while (true) {
                val offer = if (rounds < cfg.maxToolRounds) specs else emptyList()
                // Po ostatniej rundzie definicje zostają (API wymaga ich przy blokach narzędzi w historii) z zakazem wywołań.
                val send = if (offer.isNotEmpty()) offer else if (rounds > 0) specs else emptyList()
                reply = if (send.isEmpty()) {
                    engine.complete(SYSTEM_PROMPT, messages, cfg.maxTokens)
                } else {
                    engine.complete(SYSTEM_PROMPT, messages, cfg.maxTokens, send, offer.isNotEmpty())
                }
                if (reply.finish == Finish.TOOL_USE && reply.toolCalls.isNotEmpty() && offer.isNotEmpty() && tools != null) {
                    val names = offer.map { it.name }.toSet()
                    val results = reply.toolCalls.map { call -> run(tools, call, names) }
                    messages = messages + Message(Role.ASSISTANT, reply.text, toolCalls = reply.toolCalls, raw = reply.raw) +
                        Message(Role.USER, "", toolResults = results)
                    rounds++
                    continue
                }
                break
            }
            if (reply.finish == Finish.REFUSAL) throw LlmRefusal("silnik odmówił odpowiedzi")
            val answer = cleanForSpeech(reply.text)
            if (answer.isEmpty()) throw LlmException("silnik zwrócił pustą odpowiedź")
            remember(utterance, answer)
            Answer(answer, reply.finish == Finish.LENGTH, reply.model, reply.usage, rounds)
        }
    }

    /** Wywołanie narzędzia; nieoferowane narzędzie i wyjątek dają wynik z błędem, tura trwa dalej. */
    private suspend fun run(tools: ToolBox, call: ToolCall, offered: Set<String>): ToolResult {
        if (call.name !in offered) return ToolResult(call.id, call.name, "Nieznane narzędzie: ${call.name}.", isError = true)
        return try {
            tools.call(call)
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (_: Exception) {
            ToolResult(call.id, call.name, "Błąd narzędzia.", isError = true)
        }
    }

    // W historii tylko czysta wypowiedź: kontekst dotyczy jednej tury i szybko się starzeje.
    private fun remember(userText: String, answer: String) {
        val keep = cfg.historyTurns * 2
        history = if (keep == 0) emptyList() else (history + Message(Role.USER, userText) + Message(Role.ASSISTANT, answer)).takeLast(keep)
    }
}
