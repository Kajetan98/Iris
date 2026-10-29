package pl.iris.core

import java.io.IOException
import java.net.URI
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

/**
 * Silnik Claude API wywoływany z telefonu (docs/spec.md, sekcje 12 i 14). Port host/brain/claude.py bez SDK:
 * żądanie HTTP składa ta klasa, a wysyła je [HttpTransport] (w aplikacji OkHttp, w testach atrapa).
 * Klucz API i treść rozmowy nie trafiają do komunikatów błędów ani do logów.
 */

data class HttpResponse(val status: Int, val body: String)

/** Błąd sieci lub przekroczony czas: transport nie dostał odpowiedzi HTTP. */
class TransportException(message: String, cause: Throwable? = null) : IOException(message, cause)

interface HttpTransport {
    /** Rzuca [TransportException] przy braku połączenia lub przekroczeniu [timeoutMs]. */
    suspend fun post(url: String, headers: Map<String, String>, body: String, timeoutMs: Long): HttpResponse
}

/** Modele z serwerowym zapasem na odmowę klasyfikatora; dla pozostałych parametr `fallbacks` nie istnieje. */
val FALLBACK_MODELS = setOf("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
const val FALLBACK_BETA = "server-side-fallback-2026-07-01"
const val ANTHROPIC_API_VERSION = "2026-10-21"
const val DEFAULT_BASE_URL = "https://api.anthropic.com"
private const val MAX_ERROR_TEXT = 200
private val LOOPBACK_HOSTS = setOf("localhost", "127.0.0.1", "::1", "[::1]")

class ClaudeEngine(
    private val apiKey: () -> String?,
    private val config: LlmConfig,
    private val transport: HttpTransport,
    private val baseUrl: String = DEFAULT_BASE_URL,
    private val workspaceId: String = "",
) : LlmEngine {
    private val useFallbacks = config.fallbacks && config.model in FALLBACK_MODELS

    override val remote: Boolean
        get() = (runCatching { URI(baseUrl).host }.getOrNull() ?: "") !in LOOPBACK_HOSTS

    override suspend fun complete(system: String, messages: List<Message>, maxTokens: Int): LlmReply {
        val key = apiKey()?.trim().orEmpty()
        if (key.isEmpty()) throw LlmConfigError("brak klucza Claude API (wpisz go w ustawieniach)")
        val url = baseUrl.trimEnd('/') + if (useFallbacks) "/v1/messages?beta=true" else "/v1/messages"
        val headers = buildMap {
            put("x-api-key", key)
            put("anthropic-version", ANTHROPIC_API_VERSION)
            put("content-type", "application/json")
            if (useFallbacks) put("anthropic-beta", FALLBACK_BETA)
            // Klucz niepowiązany z workspace wymaga jego identyfikatora w każdym żądaniu.
            if (workspaceId.isNotBlank()) put("anthropic-workspace-id", workspaceId.trim())
        }
        val body = requestBody(system, messages, maxTokens).toString()
        return withRetries(config.retries) { attempt(url, headers, body) }
    }

    /** Treść żądania. Tokeny myślenia liczą się do max_tokens, więc odpowiedź dostaje zapas ponad swój limit. */
    internal fun requestBody(system: String, messages: List<Message>, maxTokens: Int): JsonObject = buildJsonObject {
        put("model", config.model)
        put("max_tokens", maxTokens + config.thinkingHeadroomTokens)
        put("system", system)
        putJsonArray("messages") {
            for (m in messages) addJsonObject {
                put("role", m.role.wire)
                put("content", m.content)
            }
        }
        if (config.effort != "off") putJsonObject("output_config") { put("effort", config.effort) }
        if (useFallbacks) put("fallbacks", "default")
    }

    private suspend fun attempt(url: String, headers: Map<String, String>, body: String): LlmReply {
        val response = try {
            transport.post(url, headers, body, (config.timeoutS * 1000).toLong())
        } catch (_: TransportException) {
            throw LlmUnavailable("brak połączenia z Claude API lub limit czasu ${formatSeconds(config.timeoutS)} s")
        }
        val status = response.status
        return when {
            status in 200..299 -> parse(response.body)
            status == 401 || status == 403 || status == 404 || (status == 400 && mentionsWorkspace(response.body)) ->
                throw LlmConfigError("Claude API zwróciło $status: ${errorText(response.body)}")
            status == 408 || status == 409 || status == 429 || status >= 500 ->
                throw LlmUnavailable("Claude API zwróciło $status: ${errorText(response.body)}")
            else -> throw LlmException("Claude API zwróciło $status: ${errorText(response.body)}")
        }
    }

    private fun parse(raw: String): LlmReply {
        val root = try {
            Json.parseToJsonElement(raw) as? JsonObject
        } catch (_: IllegalArgumentException) {
            null
        } ?: throw LlmException("niepoprawna odpowiedź Claude API")
        val finish = when (root.str("stop_reason")) {
            "end_turn", "stop_sequence" -> Finish.STOP
            "max_tokens" -> Finish.LENGTH
            "refusal" -> Finish.REFUSAL
            else -> Finish.OTHER
        }
        val text = ((root["content"] as? JsonArray) ?: JsonArray(emptyList()))
            .filterIsInstance<JsonObject>()
            .filter { it.str("type") == "text" }
            .joinToString("") { it.str("text").orEmpty() }
        val usage = root["usage"] as? JsonObject
        val input = listOf("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            .sumOf { (usage?.get(it) as? JsonPrimitive)?.intOrNull ?: 0 }
        val output = (usage?.get("output_tokens") as? JsonPrimitive)?.intOrNull
        return LlmReply(text, finish, root.str("model") ?: config.model, Usage(input, output))
    }

    private fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull

    /** 400 z komunikatem o `anthropic-workspace-id`: klucz bez workspace i brak jego identyfikatora w ustawieniach. */
    private fun mentionsWorkspace(raw: String): Boolean = "anthropic-workspace-id" in errorText(raw)

    private fun errorText(raw: String): String {
        val message = runCatching { ((Json.parseToJsonElement(raw) as JsonObject)["error"] as JsonObject).str("message") }.getOrNull()
        return cpTake(message ?: "brak opisu", MAX_ERROR_TEXT)
    }

    private fun formatSeconds(s: Double): String = if (s == Math.floor(s)) s.toLong().toString() else s.toString()
}
