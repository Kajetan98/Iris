package pl.iris.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue
import kotlinx.coroutines.test.currentTime
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

class ClaudeEngineTest {
    private val hello = listOf(Message(Role.USER, "Cześć"))
    private fun engine(transport: FakeTransport, config: LlmConfig = LlmConfig(), key: String? = "sk-ant-test-key", base: String = DEFAULT_BASE_URL) =
        ClaudeEngine({ key }, config, transport, base)

    @Test
    fun `request for a model with the server side fallback`() = runTest {
        val transport = FakeTransport(listOf(HttpResponse(200, okBody())))
        val reply = engine(transport).complete("SYS", hello, 300)
        val r = transport.requests.single()
        assertEquals("https://api.anthropic.com/v1/messages?beta=true", r.url)
        assertEquals("sk-ant-test-key", r.headers["x-api-key"])
        assertEquals("2026-10-21", r.headers["anthropic-version"])
        assertEquals("application/json", r.headers["content-type"])
        assertEquals("server-side-fallback-2026-07-01", r.headers["anthropic-beta"])
        assertEquals(30_000, r.timeoutMs)
        val body = Json.parseToJsonElement(r.body).jsonObject
        assertEquals("claude-opus-5-5", body.getValue("model").jsonPrimitive.content)
        assertEquals(2300, body.getValue("max_tokens").jsonPrimitive.content.toInt()) // 300 + zapas na myślenie
        assertEquals("SYS", body.getValue("system").jsonPrimitive.content)
        assertEquals("default", body.getValue("fallbacks").jsonPrimitive.content)
        assertEquals("low", body.getValue("output_config").jsonObject.getValue("effort").jsonPrimitive.content)
        val msg = body.getValue("messages").jsonArray.single().jsonObject
        assertEquals("user", msg.getValue("role").jsonPrimitive.content)
        assertEquals("Cześć", msg.getValue("content").jsonPrimitive.content)
        for (banned in listOf("temperature", "top_p", "top_k")) assertFalse(banned in body, banned)
        assertEquals("Cześć.", reply.text)
        assertEquals(Finish.STOP, reply.finish)
        assertEquals(Usage(20, 7), reply.usage) // 12 + 3 + 5 tokenów wejścia
    }

    @Test
    fun `request for a model without the fallback and with effort off`() = runTest {
        val transport = FakeTransport(listOf(HttpResponse(200, okBody(model = "claude-haiku-5-5"))))
        val reply = engine(transport, LlmConfig(model = "claude-haiku-5-5", effort = "off")).complete("S", hello, 100)
        val r = transport.requests.single()
        assertEquals("https://api.anthropic.com/v1/messages", r.url)
        assertFalse("anthropic-beta" in r.headers)
        val body = Json.parseToJsonElement(r.body).jsonObject
        assertFalse("fallbacks" in body)
        assertFalse("output_config" in body)
        assertEquals("claude-haiku-5-5", reply.model)
    }

    @Test
    fun `fallbacks can be switched off for any model`() = runTest {
        val transport = FakeTransport(listOf(HttpResponse(200, okBody())))
        engine(transport, LlmConfig(fallbacks = false)).complete("S", hello, 10)
        val r = transport.requests.single()
        assertEquals("https://api.anthropic.com/v1/messages", r.url)
        assertFalse("anthropic-beta" in r.headers)
        assertFalse("fallbacks" in Json.parseToJsonElement(r.body).jsonObject)
    }

    @Test
    fun `stop reasons`() = runTest {
        assertEquals(Finish.LENGTH, engine(FakeTransport(listOf(HttpResponse(200, okBody(stop = "max_tokens"))))).complete("S", hello, 10).finish)
        assertEquals(Finish.REFUSAL, engine(FakeTransport(listOf(HttpResponse(200, okBody(stop = "refusal"))))).complete("S", hello, 10).finish)
        assertEquals(Finish.OTHER, engine(FakeTransport(listOf(HttpResponse(200, okBody(stop = "tool_use"))))).complete("S", hello, 10).finish)
    }

    @Test
    fun `a missing key is a configuration error and nothing is sent`() = runTest {
        val transport = FakeTransport(listOf(HttpResponse(200, okBody())))
        assertFailsWith<LlmConfigError> { engine(transport, key = null).complete("S", hello, 10) }
        assertFailsWith<LlmConfigError> { engine(transport, key = "  ").complete("S", hello, 10) }
        assertTrue(transport.requests.isEmpty())
    }

    @Test
    fun `errors are classified like on the host`() = runTest {
        val err = { status: Int -> HttpResponse(status, """{"type":"error","error":{"type":"x","message":"komunikat $status"}}""") }
        for (status in listOf(401, 403, 404)) {
            val e = assertFailsWith<LlmConfigError> { engine(FakeTransport(listOf(err(status))), LlmConfig(retries = 0)).complete("S", hello, 10) }
            assertEquals("Claude API zwróciło $status: komunikat $status", e.message)
        }
        for (status in listOf(408, 409, 429, 500, 503, 529)) {
            assertFailsWith<LlmUnavailable>("$status") { engine(FakeTransport(listOf(err(status))), LlmConfig(retries = 0)).complete("S", hello, 10) }
        }
        for (status in listOf(400, 413, 422)) {
            val e = assertFailsWith<LlmException>("$status") { engine(FakeTransport(listOf(err(status)))).complete("S", hello, 10) }
            assertFalse(e is LlmUnavailable || e is LlmConfigError)
        }
        assertFailsWith<LlmUnavailable> {
            engine(FakeTransport(listOf(TransportException("timeout"))), LlmConfig(retries = 0)).complete("S", hello, 10)
        }
        assertFailsWith<LlmException> { engine(FakeTransport(listOf(HttpResponse(200, "to nie JSON")))).complete("S", hello, 10) }
        assertFailsWith<LlmException> { engine(FakeTransport(listOf(HttpResponse(200, "[1]")))).complete("S", hello, 10) }
    }

    @Test
    fun `an unreadable error body falls back to a generic description`() = runTest {
        val e = assertFailsWith<LlmException> { engine(FakeTransport(listOf(HttpResponse(400, "<html>")))).complete("S", hello, 10) }
        assertEquals("Claude API zwróciło 400: brak opisu", e.message)
        val long = HttpResponse(400, """{"error":{"message":"${"x".repeat(500)}"}}""")
        val e2 = assertFailsWith<LlmException> { engine(FakeTransport(listOf(long))).complete("S", hello, 10) }
        assertEquals("Claude API zwróciło 400: ${"x".repeat(200)}", e2.message)
    }

    @Test
    fun `unavailable is retried with a growing delay and then succeeds`() = runTest {
        val transport = FakeTransport(listOf(HttpResponse(529, "{}"), TransportException("x"), HttpResponse(200, okBody())))
        val reply = engine(transport, LlmConfig(retries = 2)).complete("S", hello, 10)
        assertEquals("Cześć.", reply.text)
        assertEquals(3, transport.requests.size)
        assertEquals(500L + 1000L, currentTime) // 0,5 s i 1 s w czasie wirtualnym
    }

    @Test
    fun `retries run out`() = runTest {
        val transport = FakeTransport(listOf(HttpResponse(500, "{}")))
        assertFailsWith<LlmUnavailable> { engine(transport, LlmConfig(retries = 1)).complete("S", hello, 10) }
        assertEquals(2, transport.requests.size)
    }

    @Test
    fun `configuration errors are not retried`() = runTest {
        val transport = FakeTransport(listOf(HttpResponse(401, "{}")))
        assertFailsWith<LlmConfigError> { engine(transport, LlmConfig(retries = 3)).complete("S", hello, 10) }
        assertEquals(1, transport.requests.size)
    }

    @Test
    fun `the key and the conversation stay out of error messages`() = runTest {
        val secret = "sk-ant-sekret-12345"
        val conversation = listOf(Message(Role.USER, "bardzo prywatna wypowiedź"))
        val failures = listOf(HttpResponse(401, "{}"), HttpResponse(500, "{}"), HttpResponse(400, "{}"), TransportException("x"))
        for (f in failures) {
            val e = assertFailsWith<LlmException> { engine(FakeTransport(listOf(f)), LlmConfig(retries = 0), key = secret).complete("S", conversation, 10) }
            assertFalse(secret in e.message.orEmpty())
            assertFalse("prywatna" in e.message.orEmpty())
        }
    }

    @Test
    fun `an engine counts as remote unless it points at the loopback`() {
        val t = FakeTransport(emptyList())
        assertTrue(engine(t).remote)
        assertTrue(engine(t, base = "http://192.168.1.5:8080").remote)
        assertFalse(engine(t, base = "http://localhost:11434").remote)
        assertFalse(engine(t, base = "http://127.0.0.1:8080").remote)
    }
}
