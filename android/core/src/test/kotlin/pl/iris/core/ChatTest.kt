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
    private val lampa = """{"domain": "dom", "concepts": ["lampa", "salon"]}"""

    private class Rig(scope: TestScope, replies: List<Any>, remote: Boolean, tools: Boolean, config: CoreConfig, withEngine: Boolean) {
        val clock = ManualClock()
        val store = InMemoryMemoryStore()
        val engine = ScriptedEngine(replies, remote, tools)
        val chat = Chat(config, clock, store, if (withEngine) Assistant(engine, config.llm) else null, scope, ZoneOffset.UTC)

        fun labels() = store.concepts().values.map { it.label }.sorted()

        fun extractions() = engine.calls.filter { it.first == CONCEPTS_PROMPT }
    }

    private fun TestScope.rig(
        replies: List<Any> = listOf("Dobrze."),
        remote: Boolean = false,
        tools: Boolean = false,
        config: CoreConfig = CoreConfig(),
        withEngine: Boolean = true,
    ) = Rig(this, replies, remote, tools, config, withEngine)

    private suspend fun Rig.turn(text: String): ChatReply = chat.say(text).also { chat.idle() }

    @Test
    fun `memory commands work without a model`() = runTest {
        val r = rig(withEngine = false)
        assertEquals(ChatReply("Zapamiętane: lampa, stoi, salonie.", false, null, Usage(0, 0)), r.turn("Zapamiętaj, że lampa stoi w salonie."))
        val recall = r.turn("Co o mnie wiesz?").text
        assertTrue(recall.startsWith("Najmocniej kojarzę: ") && recall.endsWith("Razem pamiętam 3 pojęcia."), recall)
        assertEquals("Zapomniane: lampa.", r.turn("Zapomnij o lampie").text)
        assertEquals(listOf("salonie", "stoi"), r.labels())
        assertEquals("Zapamiętane: cukrzycę, PIN.", r.turn("zapamiętaj, że mam cukrzycę i PIN 1234").text)
        assertFailsWith<ChatDisabled> { r.turn("Jaka jest pogoda?") }
        assertFailsWith<IllegalArgumentException> { r.turn("   ") }
    }

    @Test
    fun `forget all asks first and the window closes the question`() = runTest {
        val r = rig(withEngine = false)
        assertEquals("Nic nie pamiętam, nie ma czego usuwać.", r.turn("zapomnij wszystko").text)
        r.turn("zapamiętaj, że jutro mam spotkanie")
        assertEquals("Na pewno usunąć całą pamięć (2 pojęcia)? Powiedz tak albo nie.", r.turn("zapomnij wszystko").text)
        assertEquals("Dobrze, niczego nie usuwam.", r.turn("nie").text)
        r.turn("zapomnij wszystko")
        r.clock.advance(121.0)
        assertFailsWith<ChatDisabled> { r.turn("tak") } // po oknie to zwykła wypowiedź
        r.turn("zapomnij wszystko")
        assertEquals("Pamięć wyczyszczona.", r.turn("Tak, usuń.").text)
        assertEquals(0, r.store.count())
    }

    @Test
    fun `a local model teaches the graph in the background from the utterance alone`() = runTest {
        val r = rig(listOf("Dobrze.", lampa))
        assertEquals("Dobrze.", r.turn("Zapal lampę w salonie").text)
        val (_, messages, _) = r.extractions().single()
        assertEquals(listOf("Zapal lampę w salonie"), messages.map { it.content })
        assertEquals(listOf("lampa", "salon"), r.labels())
        assertEquals(mapOf("dom" to 1), r.store.concepts(listOf("lamp")).getValue("lamp").domains)
    }

    @Test
    fun `a bad reply or a remote model falls back to the heuristic`() = runTest {
        val bad = rig(listOf("Dobrze.", "nie wiem"))
        bad.turn("Mój pies Burek lubi spacery")
        assertEquals(listOf("burek", "lubi", "pies", "spacery"), bad.labels())
        val remote = rig(listOf("Dobrze."), remote = true)
        remote.turn("Zapal lampę w salonie")
        assertTrue(remote.extractions().isEmpty())
        assertEquals(listOf("lampę", "salonie", "zapal"), remote.labels())
        val always = rig(listOf("Dobrze.", lampa), remote = true, config = CoreConfig(memory = MemoryConfig(infer = InferPolicy.ALWAYS)))
        always.turn("Zapal lampę w salonie")
        assertEquals(1, always.extractions().size)
    }

    @Test
    fun `sensitive utterances do not teach and numbers never stay`() = runTest {
        val r = rig(listOf("Dobrze.", """{"domain": "ty", "concepts": ["tętno", "rower", "Rower 3"]}"""))
        r.turn("Mieszkam na ulicy Polnej w Krakowie")
        assertEquals(0, r.store.count())
        assertTrue(r.extractions().isEmpty())
        r.turn("Jeżdżę na rowerze")
        assertEquals(listOf("rower"), r.labels())
    }

    @Test
    fun `the recall tool answers from the graph and praise reinforces those concepts`() = runTest {
        val r = rig(listOf(toolCall("iris_recall", "query" to "lampa"), "Pamiętam lampę w salonie.", "{}"), tools = true)
        r.turn("zapamiętaj, że lampa stoi w salonie")
        val before = r.store.concepts(listOf("lamp")).getValue("lamp").strength
        assertEquals("Pamiętam lampę w salonie.", r.turn("Co pamiętasz o lampie?").text)
        val result = r.engine.calls[1].second.last().toolResults.single().content
        assertTrue(result.startsWith("lampa (siła ") && "wprost" in result, result)
        val recalled = r.store.concepts(listOf("lamp")).getValue("lamp").strength
        assertTrue(recalled > before)
        assertEquals("Dobrze, wzmacniam pojęcia użyte w tej odpowiedzi.", r.turn("dzięki").text)
        assertTrue(r.store.concepts(listOf("lamp")).getValue("lamp").strength > recalled)
        assertEquals("scripted", r.turn("dzięki").model) // lista zużyta: druga pochwała trafia do modelu
    }

    @Test
    fun `criticism weakens recalled concepts and forgets weak ones`() = runTest {
        val config = CoreConfig(memory = MemoryConfig(feedbackDown = 0.9))
        val r = rig(listOf(toolCall("iris_recall", "query" to "lampa"), "W kuchni.", "{}"), tools = true, config = config)
        r.turn("zapamiętaj, że lampa")
        r.store.save(listOf(r.store.concepts(listOf("lamp")).getValue("lamp").copy(strength = 0.03)))
        r.turn("Gdzie lampa?")
        assertEquals("Dobrze, osłabiam pojęcia użyte w tej odpowiedzi.", r.turn("to nie tak").text)
        assertEquals(0, r.store.count())
    }

    @Test
    fun `memory reaches the model only when the policy allows`() = runTest {
        val remote = rig(listOf("Dobrze."), remote = true, tools = true)
        remote.turn("zapamiętaj, że lampa stoi w salonie")
        remote.turn("Gdzie stoi lampa?")
        assertEquals(listOf("iris_remember"), remote.engine.offered.first())
        assertFalse("Pamięć IRIS" in remote.engine.chatCalls.first().second.last().content)
        val local = rig(listOf("Dobrze."))
        local.turn("zapamiętaj, że lampa stoi w salonie")
        local.turn("Gdzie stoi lampa?")
        assertTrue("- lampa (siła" in local.engine.chatCalls.first().second.last().content)
    }

    @Test
    fun `the remember tool filters sensitive words`() = runTest {
        val r = rig(listOf(toolCall("iris_remember", "text" to "biorę leki na serce i lubię jazz"), "Dobrze.", "{}"), tools = true)
        r.turn("Proszę, pamiętaj o tym")
        assertTrue("jazz" in r.labels() && "leki" !in r.labels())
    }

    @Test
    fun `model errors map to chat errors`() = runTest {
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
    fun `the memory view and deleting from it`() = runTest {
        val r = rig(withEngine = false)
        r.turn("zapamiętaj, że lampa stoi w salonie")
        r.turn("zapamiętaj, że lampa")
        val view = r.chat.memory()
        assertEquals("lampa", view.concepts.first().concept.label)
        assertEquals("dom", view.concepts.first().domain)
        assertEquals(3, view.links.size)
        assertTrue(r.chat.forget("lamp"))
        assertFalse(r.chat.forget("lamp"))
        assertEquals(1, r.chat.memory().links.size)
    }

    @Test
    fun `old concepts fade on the first turn after a long break`() = runTest {
        val r = rig(withEngine = false)
        r.turn("zapamiętaj, że lampa")
        r.clock.advance(60 * 86400.0)
        assertTrue(r.turn("co o mnie wiesz").text.startsWith("Jeszcze nic nie pamiętam."))
        assertEquals(1, r.store.forgotten())
    }
}
