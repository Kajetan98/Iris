package pl.iris.core

import java.time.LocalDateTime
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

data class Answer(val text: String, val truncated: Boolean, val model: String, val usage: Usage)

/** Fasada rozmowy: historia, kontekst, polityka faktów i wywołanie silnika (port host/brain/assistant.py). */
class Assistant(val engine: LlmEngine, private val cfg: LlmConfig) {
    private var history: List<Message> = emptyList()
    private val lock = Mutex()

    fun history(): List<Message> = history

    fun reset() {
        history = emptyList()
    }

    /**
     * Odpowiedź na wypowiedź. Rzuca [LlmException] (w tym [LlmUnavailable], [LlmRefusal]); historia zmienia
     * się tylko po sukcesie.
     */
    suspend fun ask(text: String, facts: List<Fact> = emptyList(), now: LocalDateTime? = null): Answer {
        val utterance = text.trim()
        require(utterance.isNotEmpty()) { "pusta wypowiedź" }
        val chosen = selectFacts(facts, cfg, engine.remote)
        val turn = Message(Role.USER, renderUserTurn(utterance, chosen, now))
        return lock.withLock {
            val reply = engine.complete(SYSTEM_PROMPT, history + turn, cfg.maxTokens)
            if (reply.finish == Finish.REFUSAL) throw LlmRefusal("silnik odmówił odpowiedzi")
            val answer = cleanForSpeech(reply.text)
            if (answer.isEmpty()) throw LlmException("silnik zwrócił pustą odpowiedź")
            remember(utterance, answer)
            Answer(answer, reply.finish == Finish.LENGTH, reply.model, reply.usage)
        }
    }

    // W historii tylko czysta wypowiedź: kontekst dotyczy jednej tury i szybko się starzeje.
    private fun remember(userText: String, answer: String) {
        val keep = cfg.historyTurns * 2
        history = if (keep == 0) emptyList() else (history + Message(Role.USER, userText) + Message(Role.ASSISTANT, answer)).takeLast(keep)
    }
}
