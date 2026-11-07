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

    /** Narzędzia tury: zapisuje wywołania, `iris_recall` zwraca stały tekst. */
    private class FakeTools(
        val specs: List<ToolSpec> = listOf(RECALL_TOOL, REMEMBER_TOOL),
        val lines: List<String> = listOf("lampa (siła 0,40, wprost)"),
        val boom: Boolean = false,
    ) : ToolBox {
        val calls = mutableListOf<ToolCall>()
        var contextAsked = 0

        override fun specs(remote: Boolean) = specs

        override suspend fun call(call: ToolCall): ToolResult {
            calls += call
            if (boom) error("baza padła")
            return ToolResult(call.id, call.name, "lampa (siła 0,62, wprost)")
        }

        override suspend fun context(text: String, remote: Boolean): List<String> {
            contextAsked++
            return lines
        }
    }

    @Test
    fun `the turn carries the context block and the history keeps the plain utterance`() = runTest {
        val engine = ScriptedEngine(listOf("Dzień dobry."))
        val assistant = Assistant(engine, LlmConfig())
        val tools = FakeTools()
        val answer = assistant.ask("  Cześć  ", now, tools)
        assertEquals("Dzień dobry.", answer.text)
        val (system, messages, maxTokens) = engine.calls.single()
        assertEquals(SYSTEM_PROMPT, system)
        assertEquals(300, maxTokens)
        val turn = messages.single().content
        assertTrue(turn.startsWith(CONTEXT_HEADER))
        assertTrue("Czas: środa, 28 października 2026, 14:05" in turn)
        // Silnik bez narzędzi: pamięć w bloku kontekstu.
        assertTrue("Pamięć IRIS, pojęcia przypomniane do tej wypowiedzi:\n- lampa (siła 0,40, wprost)" in turn)
        assertTrue(turn.endsWith("$USER_HEADER\nCześć"))
        assertEquals(1, tools.contextAsked)
        assertEquals(listOf(Message(Role.USER, "Cześć"), Message(Role.ASSISTANT, "Dzień dobry.")), assistant.history())
    }

    @Test
    fun `a tool round sends the results and keeps only text in the history`() = runTest {
        val engine = ScriptedEngine(listOf(toolCall("iris_recall", "query" to "lampa"), "Pamiętam lampę."), supportsTools = true)
        val assistant = Assistant(engine, LlmConfig())
        val tools = FakeTools()
        val answer = assistant.ask("Co pamiętasz o lampie?", now, tools)
        assertEquals("Pamiętam lampę.", answer.text)
        assertEquals(1, answer.toolRounds)
        assertEquals(listOf(listOf("iris_recall", "iris_remember"), listOf("iris_recall", "iris_remember")), engine.offered)
        assertEquals(0, tools.contextAsked) // model dostał iris_recall, więc kontekst bez pojęć
        assertEquals("lampa", tools.calls.single().arguments.stringArg("query"))
        val second = engine.calls[1].second
        assertEquals(Role.ASSISTANT, second[1].role)
        assertEquals("lampa (siła 0,62, wprost)", second[2].toolResults.single().content)
        assertEquals(listOf("Co pamiętasz o lampie?", "Pamiętam lampę."), assistant.history().map { it.content })
        assertTrue(assistant.history().all { it.toolCalls.isEmpty() && it.toolResults.isEmpty() })
    }

    @Test
    fun `after the last round tools stay but calls are not allowed`() = runTest {
        val call = toolCall("iris_recall", "query" to "x")
        val engine = ScriptedEngine(listOf(call, call, "Koniec."), supportsTools = true)
        val answer = Assistant(engine, LlmConfig(maxToolRounds = 2)).ask("x", now, FakeTools())
        assertEquals("Koniec.", answer.text)
        assertEquals(2, answer.toolRounds)
        assertEquals(listOf("iris_recall", "iris_remember"), engine.offered[1])
        assertTrue(engine.offered[2].isEmpty())
    }

    @Test
    fun `unknown tools and tool errors become error results`() = runTest {
        val bad = LlmReply("", Finish.TOOL_USE, "m", toolCalls = listOf(
            ToolCall("a", "rm_rf", kotlinx.serialization.json.JsonObject(emptyMap())),
            ToolCall("b", "iris_recall", kotlinx.serialization.json.JsonObject(emptyMap())),
        ))
        val engine = ScriptedEngine(listOf(bad, "Nie mogę."), supportsTools = true)
        Assistant(engine, LlmConfig()).ask("x", now, FakeTools(boom = true))
        val results = engine.calls[1].second.last().toolResults
        assertEquals(listOf("Nieznane narzędzie: rm_rf." to true, "Błąd narzędzia." to true), results.map { it.content to it.isError })
    }

    @Test
    fun `without recall in the specs memory goes to the context, without a toolbox there is none`() = runTest {
        val engine = ScriptedEngine(listOf("Ok."), supportsTools = true)
        val tools = FakeTools(specs = listOf(REMEMBER_TOOL), lines = emptyList())
        Assistant(engine, LlmConfig()).ask("x", now, tools)
        assertEquals(listOf("iris_remember"), engine.offered.single())
        assertEquals(1, tools.contextAsked)
        val plain = ScriptedEngine(listOf("Ok."))
        Assistant(plain, LlmConfig()).ask("x", now)
        assertFalse("Pamięć IRIS" in plain.calls.single().second.single().content)
        val off = ScriptedEngine(listOf("Ok."), supportsTools = true)
        Assistant(off, LlmConfig(tools = false)).ask("x", now, FakeTools())
        assertTrue(off.offered.single().isEmpty())
        assertTrue("- lampa (siła 0,40, wprost)" in off.calls.single().second.single().content)
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
        assertTrue("iris_recall" in SYSTEM_PROMPT && "Pamięć nie przechowuje zdań, liczb ani danych wrażliwych" in SYSTEM_PROMPT)
        for (word in listOf("bateri", "łącza", "ciał", "czujnik")) assertFalse(word in SYSTEM_PROMPT, word)
    }
}
