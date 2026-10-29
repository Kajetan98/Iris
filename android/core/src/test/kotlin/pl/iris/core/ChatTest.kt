package pl.iris.core

import java.time.ZoneOffset
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runTest

class ChatTest {
    private val burek = """[{"fakt": "Masz psa o imieniu Burek.", "pewnosc": 0.9}]"""

    private class Rig(scope: TestScope, replies: List<Any>, remote: Boolean, config: CoreConfig, withEngine: Boolean = true) {
        val clock = ManualClock()
        val store = InMemoryFactStore(clock)
        val engine = ScriptedEngine(replies, remote)
        val chat = Chat(config, clock, store, if (withEngine) Assistant(engine, config.llm) else null, scope, ZoneOffset.UTC)

        fun facts() = store.facts().map { Triple(it.text, it.source, Math.round(it.confidence * 100) / 100.0) }
    }

    private fun TestScope.rig(
        replies: List<Any> = listOf("Dobrze."),
        remote: Boolean = false,
        config: CoreConfig = CoreConfig(),
        withEngine: Boolean = true,
    ) = Rig(this, replies, remote, config, withEngine)

    private suspend fun Rig.turn(text: String): ChatReply = chat.say(text).also { chat.idle() }

    @Test
    fun `memory commands work without a model`() = runTest {
        val r = rig(withEngine = false)
        assertEquals(ChatReply("Zapamiętane: \"mam psa Burka\".", false, null, Usage(0, 0)), r.turn("Zapamiętaj, że mam psa Burka."))
        r.turn("IRIS, zapisz: pracuję w Gdyni")
        assertTrue(r.turn("Co o mnie wiesz?").text.startsWith("Wiem od ciebie: \"mam psa Burka\"; \"pracuję w Gdyni\""))
        assertEquals("Zapomniane: \"mam psa Burka\".", r.turn("Zapomnij, że mam psa").text)
        assertEquals(listOf(Triple("pracuję w Gdyni", Source.STATED, 1.0)), r.facts())
        assertFailsWith<ChatDisabled> { r.turn("Jaka jest pogoda?") }
        assertFailsWith<IllegalArgumentException> { r.turn("   ") }
    }

    @Test
    fun `a guess starts weak and grows with repetition`() = runTest {
        val config = CoreConfig(learning = LearningConfig(infer = InferPolicy.ALWAYS))
        val r = rig(listOf("Miłego spaceru.", burek, "Pozdrów Burka.", burek), config = config)
        assertEquals("Miłego spaceru.", r.turn("Idę z Burkiem na spacer.").text)
        val (system, messages, _) = r.engine.calls[1]
        assertEquals(EXTRACT_PROMPT, system)
        assertEquals(listOf("Idę z Burkiem na spacer."), messages.map { it.content }) // sama wypowiedź, bez kontekstu
        assertEquals(listOf(Triple("Masz psa o imieniu Burek", Source.INFERRED, 0.45)), r.facts())
        r.turn("Burek dziś szczekał.")
        assertEquals(listOf(Triple("Masz psa o imieniu Burek", Source.INFERRED, 0.65)), r.facts())
        assertEquals(1, r.store.facts().single().reinforced)
    }

    @Test
    fun `a remote model only learns when the policy allows it`() = runTest {
        for ((infer, expected) in listOf(InferPolicy.LOCAL_ONLY to 0, InferPolicy.NEVER to 0, InferPolicy.ALWAYS to 1)) {
            val config = CoreConfig(learning = LearningConfig(infer = infer))
            val r = rig(listOf("Ok.", burek), remote = true, config = config)
            r.turn("Idę z Burkiem na spacer.")
            assertEquals(expected, r.store.facts().size, infer.name)
            assertEquals(1 + expected, r.engine.calls.size, infer.name)
        }
        val local = rig(listOf("Ok.", burek), remote = false)
        local.turn("Idę z Burkiem na spacer.")
        assertEquals(1, local.store.facts().size) // lokalny silnik: local_only wystarcza
    }

    @Test
    fun `an unreadable guess list is dropped without breaking the turn`() = runTest {
        val config = CoreConfig(learning = LearningConfig(infer = InferPolicy.ALWAYS))
        val r = rig(listOf("Ok.", "nie wiem", "Ok.", LlmUnavailable("brak")), config = config)
        assertEquals("Ok.", r.turn("Idę z Burkiem na spacer.").text)
        assertEquals("Ok.", r.turn("Jeszcze raz").text)
        assertTrue(r.store.facts().isEmpty())
    }

    @Test
    fun `a guess is used in the context only after reinforcement and is asked about`() = runTest {
        val config = CoreConfig(
            learning = LearningConfig(infer = InferPolicy.ALWAYS, ask = false),
            llm = LlmConfig(sendFacts = SendFacts.ALWAYS),
        )
        val r = rig(listOf("Ok.", burek, "Ok.", burek, "Ok.", "[]"), config = config)
        r.turn("Idę z Burkiem na spacer.")
        r.turn("Burek dziś szczekał.")
        assertFalse("Masz psa o imieniu Burek" in r.engine.chatCalls[1].second.last().content) // 0,45: pominięty
        r.turn("Co słychać?")
        assertTrue("Masz psa o imieniu Burek (przypuszczenie, pewność 0.7)" in r.engine.chatCalls[2].second.last().content)
    }

    @Test
    fun `the question is appended to the answer and the next turn answers it`() = runTest {
        val r = rig(listOf("Jutro będzie słonecznie."))
        r.store.addFact("Masz psa o imieniu Burek", Source.INFERRED, 0.65)
        val reply = r.turn("Jaka będzie pogoda?")
        assertEquals("Jutro będzie słonecznie. Czy dobrze rozumiem: masz psa o imieniu Burek?", reply.text)
        assertEquals(1, r.engine.chatCalls.size)
        val yes = r.turn("Tak, zgadza się")
        assertEquals(ChatReply("Dobrze, zapamiętane.", false, null, Usage(0, 0)), yes)
        assertEquals(1, r.engine.chatCalls.size) // odpowiedź na pytanie nie trafia do modelu
        assertEquals(listOf(Triple("Masz psa o imieniu Burek", Source.STATED, 1.0)), r.facts())
    }

    @Test
    fun `no questions in quiet mode`() = runTest {
        val r = rig(listOf("Ok."), config = CoreConfig(quiet = true))
        r.store.addFact("Masz psa o imieniu Burek", Source.INFERRED, 0.65)
        assertEquals("Ok.", r.turn("Hej").text)
    }

    @Test
    fun `model errors map to chat errors and leave the history alone`() = runTest {
        val cases = listOf(
            LlmRefusal("odmowa") to ChatErrorKind.REFUSED,
            LlmUnavailable("brak") to ChatErrorKind.UNAVAILABLE,
            LlmConfigError("klucz") to ChatErrorKind.CONFIG,
            LlmException("inne") to ChatErrorKind.FAILED,
        )
        for ((error, kind) in cases) {
            val r = rig(listOf(error))
            val e = assertFailsWith<ChatError> { r.turn("Hej") }
            assertEquals(kind, e.kind)
            assertEquals(error.message, e.message)
        }
    }

    @Test
    fun `the facts list and deleting from it`() = runTest {
        val r = rig(withEngine = false)
        r.turn("zapamiętaj, że mam psa Burka")
        val id = r.chat.facts().single().id
        assertTrue(r.chat.forget(id))
        assertFalse(r.chat.forget(id))
        assertTrue(r.chat.facts().isEmpty())
    }

    @Test
    fun `old guesses fade on the first turn after a long break`() = runTest {
        val r = rig(withEngine = false)
        r.store.addFact("Lubisz kawę", Source.INFERRED, 0.45)
        r.clock.advance(31 * 86400.0)
        r.turn("zapamiętaj, że gram w szachy")
        assertEquals(listOf(Triple("Lubisz kawę", Source.INFERRED, 0.35), Triple("gram w szachy", Source.STATED, 1.0)), r.facts())
    }

    @Test
    fun `praise and criticism act on the guesses that went to the model`() = runTest {
        val config = CoreConfig(learning = LearningConfig(ask = false), llm = LlmConfig(sendFacts = SendFacts.ALWAYS))
        val r = rig(listOf("Dobrze."), config = config)
        r.store.addFact("Masz psa o imieniu Burek", Source.INFERRED, 0.65)
        r.turn("Co słychać?")
        val praise = r.turn("Świetnie!")
        assertEquals(ChatReply("Dobrze, zapisuję, że to było trafne.", false, null, Usage(0, 0)), praise)
        assertEquals(0.75, r.store.facts().single().confidence, 1e-9)
        val calls = r.engine.chatCalls.size
        r.turn("dzięki") // lista zużyta: zwykła rozmowa z modelem
        assertEquals(calls + 1, r.engine.chatCalls.size)
        r.turn("Co słychać?")
        assertEquals("Dobrze, osłabiam przypuszczenia użyte w tej odpowiedzi.", r.turn("to nie tak").text)
        assertEquals(0.55, r.store.facts().single().confidence, 1e-9)
    }

    @Test
    fun `reactions need guesses in the context and the switch on`() = runTest {
        val off = CoreConfig(learning = LearningConfig(ask = false, feedback = false), llm = LlmConfig(sendFacts = SendFacts.ALWAYS))
        val a = rig(listOf("Dobrze."), config = off)
        a.store.addFact("Masz psa o imieniu Burek", Source.INFERRED, 0.65)
        a.turn("Co słychać?")
        assertEquals("Dobrze.", a.turn("dzięki").text)
        assertEquals(0.65, a.store.facts().single().confidence, 1e-9)

        // pamięć w rozmowie wyłączona: przypuszczenie nie poszło do modelu, więc nie ma czego oceniać
        val noMemory = CoreConfig(learning = LearningConfig(ask = false), llm = LlmConfig(sendFacts = SendFacts.NEVER))
        val b = rig(listOf("Dobrze."), config = noMemory)
        b.store.addFact("Masz psa o imieniu Burek", Source.INFERRED, 0.65)
        b.turn("Co słychać?")
        assertEquals("Dobrze.", b.turn("dzięki").text)
        assertEquals(0.65, b.store.facts().single().confidence, 1e-9)
    }
}
