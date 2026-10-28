package pl.iris.core

/** Silnik z gotowymi odpowiedziami; zapisuje wywołania. */
class ScriptedEngine(replies: List<Any> = listOf("Dobrze."), override val remote: Boolean = false) : LlmEngine {
    private val replies = replies.toMutableList()
    val calls = mutableListOf<Triple<String, List<Message>, Int>>()

    override suspend fun complete(system: String, messages: List<Message>, maxTokens: Int): LlmReply {
        calls += Triple(system, messages.toList(), maxTokens)
        val item = if (replies.size > 1) replies.removeAt(0) else replies[0]
        return when (item) {
            is Exception -> throw item
            is LlmReply -> item
            else -> LlmReply(item.toString(), Finish.STOP, "scripted", Usage(10, 5))
        }
    }

    /** Tylko zapytania rozmowy, bez zapytań o wnioski z rozmowy. */
    val chatCalls get() = calls.filter { it.first != EXTRACT_PROMPT }
}

/** Transport z gotowymi odpowiedziami HTTP; zapisuje żądania. */
class FakeTransport(private val responses: List<Any>) : HttpTransport {
    class Request(val url: String, val headers: Map<String, String>, val body: String, val timeoutMs: Long)

    val requests = mutableListOf<Request>()
    private var next = 0

    override suspend fun post(url: String, headers: Map<String, String>, body: String, timeoutMs: Long): HttpResponse {
        requests += Request(url, headers, body, timeoutMs)
        val item = responses[minOf(next++, responses.size - 1)]
        if (item is Exception) throw item
        return item as HttpResponse
    }
}

fun okBody(text: String = "Cześć.", stop: String = "end_turn", model: String = "claude-opus-5-5"): String =
    """{"id":"msg_1","type":"message","role":"assistant","model":"$model","stop_reason":"$stop",
       "content":[{"type":"thinking","thinking":"..."},{"type":"text","text":${kotlinx.serialization.json.JsonPrimitive(text)}}],
       "usage":{"input_tokens":12,"cache_creation_input_tokens":3,"cache_read_input_tokens":5,"output_tokens":7}}"""
