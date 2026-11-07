package pl.iris.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

class SettingsTest {
    @Test
    fun `memory and learning are off by default`() {
        val s = Settings()
        assertFalse(s.hasKey)
        assertFalse(s.useMemory || s.learn || s.preferOffline)
        val c = s.toCoreConfig()
        assertEquals(SendMemory.NEVER, c.llm.sendMemory)
        assertEquals(InferPolicy.NEVER, c.memory.infer)
        assertEquals(DEFAULT_CLAUDE_MODEL, c.llm.model)
    }

    @Test
    fun `the switches map to the host policies and a blank model falls back to the default`() {
        val c = Settings(apiKey = "k", model = "  ", useMemory = true, learn = true).toCoreConfig()
        assertEquals(SendMemory.ALWAYS, c.llm.sendMemory)
        assertEquals(InferPolicy.ALWAYS, c.memory.infer)
        assertEquals(DEFAULT_CLAUDE_MODEL, c.llm.model)
        assertEquals("claude-sonnet-5-5", Settings(model = " claude-sonnet-5-5 ").toCoreConfig().llm.model)
        assertTrue(Settings(apiKey = " x ").hasKey)
        assertFalse(Settings(apiKey = "   ").hasKey)
    }

    @Test
    fun `without a key memory commands work and the model is disabled`() = runTest {
        val clock = ManualClock()
        val host = ConversationHost(InMemoryMemoryStore(), clock, FakeTransport(emptyList()), this)
        assertEquals("Zapamiętane: psa, burka.", host.say("zapamiętaj, że mam psa Burka").text)
        assertEquals(2, host.memory().concepts.size)
        assertFailsWith<ChatDisabled> { host.say("Jaka jest pogoda?") }
        assertTrue(host.forget("psa"))
    }

    @Test
    fun `updating the settings switches the model on and respects the memory switch`() = runTest {
        val clock = ManualClock()
        val store = InMemoryMemoryStore()
        val transport = FakeTransport(listOf(HttpResponse(200, okBody("Dobrze."))))
        val host = ConversationHost(store, clock, transport, this)
        host.say("zapamiętaj, że mam psa Burka")

        host.update(Settings(apiKey = "sk-ant-test"))
        assertEquals("Dobrze.", host.say("Cześć").text)
        val first = Json.parseToJsonElement(transport.requests.last().body).jsonObject
        assertFalse("burka" in first.toString()) // pamięć wyłączona: pojęcia nie wychodzą do chmury
        assertEquals(listOf("iris_remember"), first.getValue("tools").jsonArray.map { it.jsonObject.getValue("name").jsonPrimitive.content })

        assertFalse("anthropic-workspace-id" in transport.requests.last().headers)
        host.update(Settings(apiKey = "sk-ant-test", workspaceId = "wrkspc_01ABC", useMemory = true))
        host.say("Cześć jeszcze raz")
        val second = Json.parseToJsonElement(transport.requests.last().body).jsonObject
        assertEquals(listOf("iris_recall", "iris_remember"), second.getValue("tools").jsonArray.map { it.jsonObject.getValue("name").jsonPrimitive.content })
        assertEquals("sk-ant-test", transport.requests.last().headers["x-api-key"])
        assertEquals("wrkspc_01ABC", transport.requests.last().headers["anthropic-workspace-id"])
        assertEquals(DEFAULT_CLAUDE_MODEL, second.getValue("model").jsonPrimitive.content)
    }

    @Test
    fun `the remote model extracts concepts only when learning is switched on`() = runTest {
        val clock = ManualClock()
        val store = InMemoryMemoryStore()
        val extract = """{"domain": "ty", "concepts": ["pies", "Burek", "spacer"]}"""
        val transport = FakeTransport(listOf(HttpResponse(200, okBody("Miłego spaceru.")), HttpResponse(200, okBody(extract))))
        val chat = ConversationHost(store, clock, transport, this, initial = Settings(apiKey = "k", learn = true))
        chat.say("Idę z Burkiem na spacer.")
        testScheduler.advanceUntilIdle()
        assertEquals(2, transport.requests.size) // rozmowa i zapytanie o pojęcia
        assertEquals(listOf("Burek", "pies", "spacer"), store.concepts().values.map { it.label }.sorted())

        val offStore = InMemoryMemoryStore()
        val offTransport = FakeTransport(listOf(HttpResponse(200, okBody("Ok."))))
        val off = ConversationHost(offStore, clock, offTransport, this, initial = Settings(apiKey = "k"))
        off.say("Idę z Burkiem na spacer.")
        testScheduler.advanceUntilIdle()
        assertEquals(1, offTransport.requests.size) // bez zapytania o pojęcia: tylko heurystyka na telefonie
        assertEquals(listOf("burkiem", "idę", "spacer"), offStore.concepts().values.map { it.label }.sorted())
    }

    @Test
    fun `the local engine needs a chosen model, not a key, and keeps memory on the phone`() = runTest {
        assertFalse(Settings(engine = EngineKind.LOCAL, apiKey = "sk").ready)
        assertTrue(Settings(engine = EngineKind.LOCAL, localModel = "gemma.litertlm").ready)
        assertTrue(Settings(apiKey = "sk").ready)
        assertEquals("gemma.litertlm", Settings(engine = EngineKind.LOCAL, localModel = " gemma.litertlm ").toCoreConfig().llm.model)

        val clock = ManualClock()
        val store = InMemoryMemoryStore()
        val gen = object : LocalGenerator {
            var last = ""
            override suspend fun generate(system: String, message: String): String {
                last = message
                return "Dobrze."
            }
        }
        val host = ConversationHost(store, clock, FakeTransport(emptyList()), this, local = gen)
        host.say("zapamiętaj, że mam psa Burka")

        host.update(Settings(engine = EngineKind.LOCAL))
        assertFailsWith<ChatDisabled> { host.say("Cześć") } // brak wybranego modelu

        host.update(Settings(engine = EngineKind.LOCAL, localModel = "m.litertlm", useMemory = true))
        val reply = host.say("Co słychać u psa Burka?")
        assertEquals("Dobrze.", reply.text)
        assertEquals("m.litertlm", reply.model)
        assertTrue("- burka (siła" in gen.last, gen.last) // silnik lokalny bez narzędzi: pojęcia w bloku kontekstu
    }
}
