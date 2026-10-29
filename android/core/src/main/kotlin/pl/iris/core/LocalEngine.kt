package pl.iris.core

/**
 * Silnik lokalny: model językowy uruchamiany na telefonie (docs/spec.md, sekcja 14). Rdzeń nie zna środowiska
 * uruchomieniowego; w aplikacji [LocalGenerator] opiera się na LiteRT-LM, w testach jest atrapą.
 */
interface LocalGenerator {
    /**
     * Nowa rozmowa z instrukcją [system] i jedną wiadomością [message]; zwraca tekst odpowiedzi.
     * Rzuca [LlmConfigError], gdy modelu nie da się wczytać, a inne błędy jako [LlmException].
     */
    suspend fun generate(system: String, message: String): String
}

const val LOCAL_PROMPT_CHARS = 6000

private val THINKING = Regex("(?s)<think>.*?(</think>|$)")

class LocalEngine(
    private val generator: LocalGenerator,
    private val modelName: String,
    private val maxChars: Int = LOCAL_PROMPT_CHARS,
) : LlmEngine {
    override val remote: Boolean = false

    override suspend fun complete(system: String, messages: List<Message>, maxTokens: Int): LlmReply {
        require(messages.isNotEmpty()) { "brak wiadomości" }
        val text = generator.generate(system, transcript(messages, maxChars))
        return LlmReply(THINKING.replace(text, "").trim(), Finish.STOP, modelName)
    }
}

/**
 * Każde wywołanie tworzy nową rozmowę, więc historia idzie w jednej wiadomości jako zapis rozmowy.
 * Przycinanie od najstarszych tur; ostatnia wiadomość zostaje zawsze.
 */
internal fun transcript(messages: List<Message>, maxChars: Int): String {
    val last = messages.last().content
    var earlier = messages.dropLast(1).map { (if (it.role == Role.USER) "Użytkownik: " else "IRIS: ") + it.content }
    fun render() = if (earlier.isEmpty()) last else "Dotychczasowa rozmowa:\n" + earlier.joinToString("\n") + "\n\nNowa wypowiedź użytkownika:\n" + last
    while (earlier.isNotEmpty() && render().length > maxChars) earlier = earlier.drop(1)
    return render()
}
