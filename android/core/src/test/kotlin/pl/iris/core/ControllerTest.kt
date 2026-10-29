package pl.iris.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest

class FakeRecognizer : Recognizer {
    var listener: Recognizer.Listener? = null
    var offline: Boolean? = null
    var started = 0
    var stopped = 0
    var cancelled = 0

    override fun start(listener: Recognizer.Listener, preferOffline: Boolean) {
        this.listener = listener
        offline = preferOffline
        started++
    }

    override fun stop() {
        stopped++
    }

    override fun cancel() {
        cancelled++
        listener = null
    }
}

class FakeSpeaker(override val available: Boolean = true) : Speaker {
    val spoken = mutableListOf<String>()
    var stopped = 0
    var done: (() -> Unit)? = null

    override fun speak(text: String, onDone: () -> Unit) {
        spoken += text
        done = onDone
    }

    override fun stop() {
        stopped++
    }
}

class FakeConversation(private val replies: MutableList<Any>) : Conversation {
    val said = mutableListOf<String>()
    val forgotten = mutableListOf<Long>()

    override suspend fun say(text: String): ChatReply {
        said += text
        val item = if (replies.size > 1) replies.removeAt(0) else replies[0]
        if (item is Exception) throw item
        return ChatReply(item.toString(), false, "m", Usage(1, 1))
    }

    override suspend fun facts(): List<Fact> = emptyList()

    override suspend fun forget(id: Long): Boolean = forgotten.add(id)
}

class ControllerTest {
    private class Rig(scope: TestScope, replies: List<Any>, speakerAvailable: Boolean = true, ready: Boolean = true) {
        val recognizer = FakeRecognizer()
        val speaker = FakeSpeaker(speakerAvailable)
        val conversation = FakeConversation(replies.toMutableList())
        val controller = IrisController(conversation, recognizer, speaker, scope, ready)
        val state get() = controller.state.value
    }

    private fun TestScope.rig(vararg replies: Any, speakerAvailable: Boolean = true, ready: Boolean = true) =
        Rig(this, replies.toList(), speakerAvailable, ready)

    @Test
    fun `a spoken turn goes through listening, thinking and speaking`() = runTest {
        val r = rig("Dzień dobry.")
        assertEquals(Phase.IDLE, r.state.phase)
        r.controller.onTalk()
        assertEquals(Phase.LISTENING, r.state.phase)
        r.recognizer.listener!!.onLevel(0.5f)
        assertEquals(0.5f, r.state.level)
        r.controller.onTalk() // drugie dotknięcie kończy nagrywanie
        assertEquals(1, r.recognizer.stopped)
        r.recognizer.listener!!.onResult("  Cześć  ")
        assertEquals(Phase.THINKING, r.state.phase)
        assertEquals(listOf(Turn(1, heard = "Cześć")), r.state.turns)
        advanceUntilIdle()
        assertEquals(Phase.SPEAKING, r.state.phase)
        assertEquals(listOf("Dzień dobry."), r.speaker.spoken)
        assertEquals(Turn(1, heard = "Cześć", reply = "Dzień dobry."), r.state.turns.single())
        r.speaker.done!!()
        assertEquals(Phase.IDLE, r.state.phase)
        assertEquals(0f, r.state.level)
    }

    @Test
    fun `the offline preference reaches the recognizer`() = runTest {
        val r = rig("x")
        r.controller.configure(ready = true, preferOffline = true)
        r.controller.onTalk()
        assertEquals(true, r.recognizer.offline)
    }

    @Test
    fun `recognizer errors become notices and return to idle`() = runTest {
        for (error in RecognizerError.entries) {
            val r = rig("x")
            r.controller.onTalk()
            r.recognizer.listener!!.onError(error)
            assertEquals(Phase.IDLE, r.state.phase, error.name)
            assertEquals(Texts.recognizer(error), r.state.notice, error.name)
        }
        val empty = rig("x")
        empty.controller.onTalk()
        empty.recognizer.listener!!.onResult("   ")
        assertEquals(Texts.NO_MATCH, empty.state.notice)
        assertTrue(empty.conversation.said.isEmpty())
    }

    @Test
    fun `late recognizer callbacks are ignored`() = runTest {
        val r = rig("x")
        r.controller.onTalk()
        val listener = r.recognizer.listener!!
        listener.onResult("pierwsze")
        listener.onResult("drugie") // faza to już THINKING
        listener.onError(RecognizerError.OTHER)
        advanceUntilIdle()
        assertEquals(listOf("pierwsze"), r.conversation.said)
        assertNull(r.state.notice)
    }

    @Test
    fun `typing during listening cancels the recognizer and talks`() = runTest {
        val r = rig("Ok.")
        r.controller.onTalk()
        r.controller.submit("  napisane ")
        assertEquals(1, r.recognizer.cancelled)
        advanceUntilIdle()
        assertEquals(listOf("napisane"), r.conversation.said)
        r.controller.submit("   ")
        assertEquals(1, r.conversation.said.size)
    }

    @Test
    fun `a new turn while thinking is ignored`() = runTest {
        val r = rig("Ok.")
        r.controller.submit("pierwsze")
        r.controller.submit("drugie")
        r.controller.onTalk()
        advanceUntilIdle()
        assertEquals(listOf("pierwsze"), r.conversation.said)
    }

    @Test
    fun `tapping while speaking interrupts the speech`() = runTest {
        val r = rig("Długa odpowiedź.")
        r.controller.submit("x")
        advanceUntilIdle()
        assertEquals(Phase.SPEAKING, r.state.phase)
        r.controller.onTalk()
        assertEquals(1, r.speaker.stopped)
        assertEquals(Phase.IDLE, r.state.phase)
        r.speaker.done!!() // spóźniony koniec starej mowy nic nie zmienia
        assertEquals(Phase.IDLE, r.state.phase)
    }

    @Test
    fun `without a voice the answer stays in the captions and a notice explains why`() = runTest {
        val r = rig("Odpowiedź.", speakerAvailable = false)
        r.controller.submit("x")
        advanceUntilIdle()
        assertEquals(Phase.IDLE, r.state.phase)
        assertEquals(Texts.NO_VOICE, r.state.notice)
        assertEquals("Odpowiedź.", r.state.turns.single().reply)
        assertTrue(r.speaker.spoken.isEmpty())
    }

    @Test
    fun `chat errors become notes on the turn`() = runTest {
        val cases = listOf(
            ChatDisabled() to Pair(Texts.NO_MODEL, false),
            ChatError(ChatErrorKind.REFUSED, "x") to Pair(Texts.REFUSED, true),
            ChatError(ChatErrorKind.UNAVAILABLE, "x") to Pair(Texts.UNAVAILABLE, true),
            ChatError(ChatErrorKind.CONFIG, "Claude API zwróciło 401: invalid x-api-key") to Pair("${Texts.CONFIG}\nClaude API zwróciło 401: invalid x-api-key", true),
            ChatError(ChatErrorKind.CONFIG, "") to Pair(Texts.CONFIG, true),
            ChatError(ChatErrorKind.FAILED, "x") to Pair(Texts.FAILED, true),
            IllegalStateException("treść: bardzo prywatne") to Pair(Texts.FAILED, true),
        )
        for ((error, expected) in cases) {
            val r = rig(error)
            r.controller.submit("x")
            advanceUntilIdle()
            val turn = r.state.turns.single()
            assertEquals(expected.first, turn.note, error.toString())
            assertEquals(expected.second, turn.warn, error.toString())
            assertNull(turn.reply)
            assertEquals(Phase.IDLE, r.state.phase)
            assertTrue("prywatne" !in turn.note.orEmpty())
        }
    }

    @Test
    fun `only the last turns are kept`() = runTest {
        val r = rig("Ok.", speakerAvailable = false)
        repeat(MAX_TURNS + 5) {
            r.controller.submit("n$it")
            advanceUntilIdle()
        }
        assertEquals(MAX_TURNS, r.state.turns.size)
        assertEquals("n${MAX_TURNS + 4}", r.state.turns.last().heard)
        assertEquals("n5", r.state.turns.first().heard)
    }

    @Test
    fun `permission denial, notice clearing, key flag and shutdown`() = runTest {
        val r = rig("x", ready = false)
        assertEquals(Texts.STATE_IDLE_NO_MODEL, Texts.state(r.state.phase, r.state.ready))
        r.controller.configure(ready = true, preferOffline = false)
        assertEquals(Texts.STATE_IDLE, Texts.state(r.state.phase, r.state.ready))
        r.controller.onTalk()
        r.controller.onPermissionDenied()
        assertEquals(Phase.IDLE, r.state.phase)
        assertEquals(Texts.NO_PERMISSION, r.state.notice)
        r.controller.clearNotice()
        assertNull(r.state.notice)
        r.controller.shutdown()
        assertEquals(1, r.speaker.stopped)
        assertEquals(1, r.recognizer.cancelled)
    }

    @Test
    fun `state texts are fixed and do not suggest feelings`() {
        assertEquals("Słucham", Texts.state(Phase.LISTENING, true))
        assertEquals("Przygotowuję odpowiedź", Texts.state(Phase.THINKING, true))
        assertEquals("Mówię", Texts.state(Phase.SPEAKING, true))
        val all = listOf(Texts.NO_MODEL, Texts.NO_MATCH, Texts.NO_PERMISSION, Texts.RECOGNIZER_UNAVAILABLE, Texts.REFUSED, Texts.UNAVAILABLE, Texts.CONFIG, Texts.FAILED, Texts.NO_VOICE)
        for (t in all) for (word in listOf("czuj", "smutn", "przykro mi", "cieszę")) assertTrue(word !in t.lowercase(), "$word w: $t")
    }
}
