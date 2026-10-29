package pl.iris.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue
import kotlinx.coroutines.test.runTest

class LocalEngineTest {
    private class FakeGenerator(private val replies: MutableList<Any>) : LocalGenerator {
        val calls = mutableListOf<Pair<String, String>>()

        override suspend fun generate(system: String, message: String): String {
            calls += system to message
            val next = replies.removeAt(0)
            if (next is Exception) throw next
            return next as String
        }
    }

    @Test
    fun `a single message goes through unchanged and the engine is local`() = runTest {
        val gen = FakeGenerator(mutableListOf("Cześć."))
        val engine = LocalEngine(gen, "gemma.litertlm")
        assertFalse(engine.remote)
        val reply = engine.complete("SYS", listOf(Message(Role.USER, "Hej")), 300)
        assertEquals(LlmReply("Cześć.", Finish.STOP, "gemma.litertlm"), reply)
        assertEquals(listOf("SYS" to "Hej"), gen.calls)
    }

    @Test
    fun `history becomes a transcript before the new utterance`() = runTest {
        val gen = FakeGenerator(mutableListOf("ok"))
        val messages = listOf(Message(Role.USER, "Mam psa"), Message(Role.ASSISTANT, "Dobrze"), Message(Role.USER, "Jak mam na imię?"))
        LocalEngine(gen, "m").complete("S", messages, 300)
        assertEquals(
            "Dotychczasowa rozmowa:\nUżytkownik: Mam psa\nIRIS: Dobrze\n\nNowa wypowiedź użytkownika:\nJak mam na imię?",
            gen.calls.single().second,
        )
    }

    @Test
    fun `the oldest turns are dropped to fit and the last message always stays`() {
        val messages = listOf(
            Message(Role.USER, "a".repeat(100)), Message(Role.ASSISTANT, "b".repeat(100)),
            Message(Role.USER, "c".repeat(100)), Message(Role.ASSISTANT, "d".repeat(100)),
            Message(Role.USER, "nowe"),
        )
        val t = transcript(messages, 300)
        assertTrue(t.length <= 300, t.length.toString())
        assertTrue("c".repeat(100) in t && "d".repeat(100) in t)
        assertFalse("a".repeat(100) in t)
        val tiny = transcript(messages, 5)
        assertEquals("nowe", tiny)
    }

    @Test
    fun `thinking blocks are removed from the reply`() = runTest {
        val gen = FakeGenerator(mutableListOf("<think>Hmm, użytkownik pyta.\nTak.</think>\nDzień dobry.", "<think>nie skończone"))
        val engine = LocalEngine(gen, "m")
        assertEquals("Dzień dobry.", engine.complete("S", listOf(Message(Role.USER, "x")), 1).text)
        assertEquals("", engine.complete("S", listOf(Message(Role.USER, "x")), 1).text)
    }

    @Test
    fun `generator errors reach the caller as they are`() = runTest {
        val gen = FakeGenerator(mutableListOf(LlmConfigError("nie wczytano modelu")))
        assertFailsWith<LlmConfigError> { LocalEngine(gen, "m").complete("S", listOf(Message(Role.USER, "x")), 1) }
    }
}
