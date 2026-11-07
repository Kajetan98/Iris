package pl.iris.core

import java.time.ZoneId
import kotlinx.coroutines.CoroutineScope

/**
 * Ustawienia aplikacji (docs/spec.md, sekcja 14). Pamięć w rozmowie i uczenie się z modelu są domyślnie
 * wyłączone: przy silniku Claude ich włączenie oznacza, że pojęcia i wypowiedzi trafiają do Anthropic.
 */
enum class EngineKind { CLAUDE, LOCAL }

data class Settings(
    /** Claude API (zdalny) albo model na telefonie (lokalny, bez klucza i opłat). */
    val engine: EngineKind = EngineKind.CLAUDE,
    /** Nazwa pliku `.litertlm` skopiowanego do pamięci aplikacji; puste, gdy nie wybrano modelu. */
    val localModel: String = "",
    val apiKey: String = "",
    /** Identyfikator workspace (`wrkspc_...`); potrzebny tylko dla klucza niepowiązanego z workspace. */
    val workspaceId: String = "",
    val model: String = DEFAULT_CLAUDE_MODEL,
    /** Pamięć trafia do modelu (`send_memory = always`), inaczej `never`. */
    val useMemory: Boolean = false,
    /** Pojęcia z rozmów wyciąga model (`infer = always`), inaczej tylko heurystyka (`never`). */
    val learn: Boolean = false,
    val preferOffline: Boolean = false,
    /** Sprawdzanie aktualizacji aplikacji przy starcie (żądanie do GitHuba). */
    val checkUpdates: Boolean = true,
    /** Hasło wybudzające "Hej Iris" / "Irysku" przy otwartej aplikacji (wymaga modelu Vosk). */
    val wakeWord: Boolean = false,
    /** Nasłuch hasła także w tle i przy wyłączonym ekranie (usługa pierwszoplanowa ze stałym powiadomieniem). */
    val wakeBackground: Boolean = false,
) {
    val hasKey: Boolean get() = apiKey.isNotBlank()

    /** Jest czym rozmawiać: klucz dla Claude albo wybrany plik modelu dla silnika lokalnego. */
    val ready: Boolean get() = if (engine == EngineKind.LOCAL) localModel.isNotBlank() else hasKey
}

fun Settings.toCoreConfig(): CoreConfig = CoreConfig(
    memory = MemoryConfig(infer = if (learn) InferPolicy.ALWAYS else InferPolicy.NEVER),
    llm = LlmConfig(
        model = if (engine == EngineKind.LOCAL) localModel.trim() else model.trim().ifEmpty { DEFAULT_CLAUDE_MODEL },
        sendMemory = if (useMemory) SendMemory.ALWAYS else SendMemory.NEVER,
    ),
)

/** Przechowywanie ustawień (na telefonie: SharedPreferences, klucz API zaszyfrowany kluczem z Keystore). */
interface SettingsStore {
    fun load(): Settings

    fun save(settings: Settings)
}

/**
 * Rozmowa zależna od ustawień: po każdej zmianie buduje [Chat] od nowa (z nową konfiguracją i kluczem).
 * Bez klucza (albo bez wybranego modelu lokalnego) działają polecenia pamięci, a rozmowa kończy się [ChatDisabled].
 */
class ConversationHost(
    private val store: MemoryStore,
    private val clock: Clock,
    private val transport: HttpTransport,
    private val scope: CoroutineScope,
    private val zone: ZoneId = ZoneId.systemDefault(),
    initial: Settings = Settings(),
    private val local: LocalGenerator? = null,
) : Conversation {
    @Volatile
    private var chat: Chat = build(initial)

    fun update(settings: Settings) {
        chat = build(settings)
    }

    private fun build(settings: Settings): Chat {
        val config = settings.toCoreConfig()
        val engine: LlmEngine? = when {
            settings.engine == EngineKind.LOCAL -> if (settings.ready && local != null) LocalEngine(local, settings.localModel.trim()) else null
            settings.hasKey -> ClaudeEngine({ settings.apiKey }, config.llm, transport, workspaceId = settings.workspaceId)
            else -> null
        }
        val assistant = engine?.let { Assistant(it, config.llm) }
        return Chat(config, clock, store, assistant, scope, zone)
    }

    override suspend fun say(text: String): ChatReply = chat.say(text)

    override suspend fun memory(): MemoryView = chat.memory()

    override suspend fun forget(key: String): Boolean = chat.forget(key)
}
