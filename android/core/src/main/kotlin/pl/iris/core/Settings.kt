package pl.iris.core

import java.time.ZoneId
import kotlinx.coroutines.CoroutineScope

/**
 * Ustawienia aplikacji (docs/spec.md, sekcja 14). Pamięć w rozmowie i uczenie się są domyślnie wyłączone:
 * jedyny silnik jest zdalny, więc ich włączenie oznacza, że fakty i wypowiedzi trafiają do Anthropic.
 */
data class Settings(
    val apiKey: String = "",
    val model: String = DEFAULT_CLAUDE_MODEL,
    /** Fakty trafiają do kontekstu modelu (`send_facts = always`), inaczej `never`. */
    val useMemory: Boolean = false,
    /** Wnioski z rozmów (`infer = always`), inaczej `never`. */
    val learn: Boolean = false,
    val preferOffline: Boolean = false,
) {
    val hasKey: Boolean get() = apiKey.isNotBlank()
}

fun Settings.toCoreConfig(): CoreConfig = CoreConfig(
    learning = LearningConfig(infer = if (learn) InferPolicy.ALWAYS else InferPolicy.NEVER),
    llm = LlmConfig(
        model = model.trim().ifEmpty { DEFAULT_CLAUDE_MODEL },
        sendFacts = if (useMemory) SendFacts.ALWAYS else SendFacts.NEVER,
    ),
)

/** Przechowywanie ustawień (na telefonie: SharedPreferences, klucz API zaszyfrowany kluczem z Keystore). */
interface SettingsStore {
    fun load(): Settings

    fun save(settings: Settings)
}

/**
 * Rozmowa zależna od ustawień: po każdej zmianie buduje [Chat] od nowa (z nową konfiguracją i kluczem).
 * Bez klucza działają polecenia pamięci, a rozmowa z modelem kończy się [ChatDisabled].
 */
class ConversationHost(
    private val store: FactStore,
    private val clock: Clock,
    private val transport: HttpTransport,
    private val scope: CoroutineScope,
    private val zone: ZoneId = ZoneId.systemDefault(),
    initial: Settings = Settings(),
) : Conversation {
    @Volatile
    private var chat: Chat = build(initial)

    fun update(settings: Settings) {
        chat = build(settings)
    }

    private fun build(settings: Settings): Chat {
        val config = settings.toCoreConfig()
        val assistant = if (settings.hasKey) {
            Assistant(ClaudeEngine({ settings.apiKey }, config.llm, transport), config.llm)
        } else {
            null
        }
        return Chat(config, clock, store, assistant, scope, zone)
    }

    override suspend fun say(text: String): ChatReply = chat.say(text)

    override suspend fun facts(): List<Fact> = chat.facts()

    override suspend fun forget(id: Long): Boolean = chat.forget(id)
}
