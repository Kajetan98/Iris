package pl.iris.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

class SettingsTest {
    @Test
    fun `memory and learning are off by default`() {
        val s = Settings()
        assertFalse(s.hasKey)
        assertFalse(s.useMemory || s.learn || s.preferOffline)
        val c = s.toCoreConfig()
        assertEquals(SendFacts.NEVER, c.llm.sendFacts)
        assertEquals(InferPolicy.NEVER, c.learning.infer)
        assertEquals(DEFAULT_CLAUDE_MODEL, c.llm.model)
    }

    @Test
    fun `the switches map to the host policies and a blank model falls back to the default`() {
        val c = Settings(apiKey = "k", model = "  ", useMemory = true, learn = true).toCoreConfig()
        assertEquals(SendFacts.ALWAYS, c.llm.sendFacts)
        assertEquals(InferPolicy.ALWAYS, c.learning.infer)
        assertEquals(DEFAULT_CLAUDE_MODEL, c.llm.model)
        assertEquals("claude-sonnet-5-5", Settings(model = " claude-sonnet-5-5 ").toCoreConfig().llm.model)
        assertTrue(Settings(apiKey = " x ").hasKey)
        assertFalse(Settings(apiKey = "   ").hasKey)
    }

    @Test
    fun `without a key memory commands work and the model is disabled`() = runTest {
        val clock = ManualClock()
        val host = ConversationHost(InMemoryFactStore(clock), clock, FakeTransport(emptyList()), this)
        assertEquals("Zapamiętane: \"mam psa Burka\".", host.say("zapamiętaj, że mam psa Burka").text)
        assertEquals(1, host.facts().size)
        assertFailsWith<ChatDisabled> { host.say("Jaka jest pogoda?") }
        assertTrue(host.forget(host.facts().single().id))
    }

    @Test
    fun `updating the settings switches the model on and respects the memory switch`() = runTest {
        val clock = ManualClock()
        val store = InMemoryFactStore(clock)
        val transport = FakeTransport(listOf(HttpResponse(200, okBody("Dobrze."))))
        val host = ConversationHost(store, clock, transport, this)
        host.say("zapamiętaj, że mam psa Burka")

        host.update(Settings(apiKey = "sk-ant-test"))
        assertEquals("Dobrze.", host.say("Cześć").text)
        val first = Json.parseToJsonElement(transport.requests.last().body).jsonObject
        assertFalse("Burka" in first.toString()) // pamięć wyłączona: fakty nie wychodzą do chmury

        assertFalse("anthropic-workspace-id" in transport.requests.last().headers)
        host.update(Settings(apiKey = "sk-ant-test", workspaceId = "wrkspc_01ABC", useMemory = true))
        host.say("Cześć jeszcze raz")
        val second = Json.parseToJsonElement(transport.requests.last().body).jsonObject
        assertTrue("mam psa Burka" in second.getValue("messages").toString())
        assertEquals("sk-ant-test", transport.requests.last().headers["x-api-key"])
        assertEquals("wrkspc_01ABC", transport.requests.last().headers["anthropic-workspace-id"])
        assertEquals(DEFAULT_CLAUDE_MODEL, second.getValue("model").jsonPrimitive.content)
    }

    @Test
    fun `learning from conversations is off until switched on and then uses the remote model`() = runTest {
        val clock = ManualClock()
        val store = InMemoryFactStore(clock)
        val extract = """[{"fakt": "Masz psa o imieniu Burek.", "pewnosc": 0.9}]"""
        val transport = FakeTransport(listOf(HttpResponse(200, okBody("Miłego spaceru.")), HttpResponse(200, okBody(extract))))
        val chat = ConversationHost(store, clock, transport, this, initial = Settings(apiKey = "k", learn = true))
        chat.say("Idę z Burkiem na spacer.")
        testScheduler.advanceUntilIdle()
        assertEquals(2, transport.requests.size) // rozmowa i zapytanie o wnioski
        assertEquals(listOf("Masz psa o imieniu Burek"), store.facts().map { it.text })

        val off = ConversationHost(InMemoryFactStore(clock), clock, FakeTransport(listOf(HttpResponse(200, okBody("Ok.")))), this, initial = Settings(apiKey = "k"))
        off.say("Idę z Burkiem na spacer.")
        testScheduler.advanceUntilIdle()
        assertTrue(off.facts().isEmpty())
    }
}
