package pl.iris.core

import java.time.LocalDateTime
import java.time.ZoneOffset
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue
import kotlinx.coroutines.test.runTest

class AssistantTest {
    private val t0 = java.time.Instant.parse("2026-10-28T14:05:00Z")
    private val now = LocalDateTime.ofInstant(t0, ZoneOffset.UTC)

    private fun fact(id: Long, text: String, source: Source = Source.STATED, confidence: Double = 1.0) =
        Fact(id, text, source, confidence, t0, t0)

    @Test
    fun `the turn carries the context block and the history keeps the plain utterance`() = runTest {
        val engine = ScriptedEngine(listOf("Dzień dobry."))
        val assistant = Assistant(engine, LlmConfig(sendFacts = SendFacts.ALWAYS))
        val answer = assistant.ask("  Cześć  ", listOf(fact(1, "Masz psa Burka")), now)
        assertEquals("Dzień dobry.", answer.text)
        val (system, messages, maxTokens) = engine.calls.single()
        assertEquals(SYSTEM_PROMPT, system)
        assertEquals(300, maxTokens)
        val turn = messages.single().content
        assertTrue(turn.startsWith(CONTEXT_HEADER))
        assertTrue("Czas: środa, 28 października 2026, 14:05" in turn)
        assertTrue("- Masz psa Burka" in turn)
        assertTrue(turn.endsWith("$USER_HEADER\nCześć"))
        assertEquals(listOf(Message(Role.USER, "Cześć"), Message(Role.ASSISTANT, "Dzień dobry.")), assistant.history())
    }

    @Test
    fun `facts follow send_facts and the confidence threshold`() = runTest {
        val facts = listOf(fact(1, "Stated"), fact(2, "Weak guess", Source.INFERRED, 0.45), fact(3, "Strong guess", Source.INFERRED, 0.65))
        suspend fun contextFor(cfg: LlmConfig, remote: Boolean): String {
            val engine = ScriptedEngine(listOf("Ok."), remote = remote)
            Assistant(engine, cfg).ask("x", facts, now)
            return engine.calls.single().second.single().content
        }
        val all = contextFor(LlmConfig(sendFacts = SendFacts.ALWAYS), remote = true)
        assertTrue("- Stated" in all && "- Strong guess (przypuszczenie, pewność 0.7)" in all && "Weak guess" !in all)
        assertFalse("Znane fakty" in contextFor(LlmConfig(sendFacts = SendFacts.NEVER), remote = true))
        assertFalse("Znane fakty" in contextFor(LlmConfig(sendFacts = SendFacts.LOCAL_ONLY), remote = true))
        assertTrue("Znane fakty" in contextFor(LlmConfig(sendFacts = SendFacts.LOCAL_ONLY), remote = false))
    }

    @Test
    fun `history is trimmed and an error leaves it unchanged`() = runTest {
        val engine = ScriptedEngine(listOf("a", "b", "c", LlmUnavailable("brak"), "d"))
        val assistant = Assistant(engine, LlmConfig(historyTurns = 2))
        for (u in listOf("1", "2", "3")) assistant.ask(u)
        assertEquals(listOf("2", "b", "3", "c"), assistant.history().map { it.content })
        assertFailsWith<LlmUnavailable> { assistant.ask("4") }
        assertEquals(listOf("2", "b", "3", "c"), assistant.history().map { it.content })
        assistant.ask("5")
        assertEquals(listOf("3", "c", "5", "d"), assistant.history().map { it.content })
        assistant.reset()
        assertTrue(assistant.history().isEmpty())
    }

    @Test
    fun `no history when history_turns is zero`() = runTest {
        val assistant = Assistant(ScriptedEngine(), LlmConfig(historyTurns = 0))
        assistant.ask("x")
        assertTrue(assistant.history().isEmpty())
    }

    @Test
    fun `refusal, empty answers and empty utterances`() = runTest {
        val refusing = Assistant(ScriptedEngine(listOf(LlmReply("", Finish.REFUSAL, "m"))), LlmConfig())
        assertFailsWith<LlmRefusal> { refusing.ask("x") }
        val empty = Assistant(ScriptedEngine(listOf("  **  ")), LlmConfig())
        assertFailsWith<LlmException> { empty.ask("x") }
        assertFailsWith<IllegalArgumentException> { Assistant(ScriptedEngine(), LlmConfig()).ask("   ") }
    }

    @Test
    fun `a cut answer is flagged and cleaned for speech`() = runTest {
        val engine = ScriptedEngine(listOf(LlmReply("# Tytuł\n- **jeden**\n- dwa", Finish.LENGTH, "m", Usage(3, 4))))
        val answer = Assistant(engine, LlmConfig()).ask("x")
        assertEquals(Answer("Tytuł jeden dwa", true, "m", Usage(3, 4)), answer)
    }

    @Test
    fun `the system prompt describes a program without feelings`() {
        assertTrue("Jesteś programem" in SYSTEM_PROMPT)
        assertTrue("Nie twierdzisz, że coś czujesz" in SYSTEM_PROMPT)
        assertTrue("zapamiętaj, że ..." in SYSTEM_PROMPT)
        for (word in listOf("bateri", "łącz", "ciał", "czujnik")) assertFalse(word in SYSTEM_PROMPT, word)
    }
}
