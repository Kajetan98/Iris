package pl.iris.core

/**
 * Wartości początkowe z konfiguracji hosta (host/core/config.py). Test zgodności
 * (VectorsTest) pilnuje, żeby się nie rozjechały z kodem Pythona.
 */

enum class InferPolicy { LOCAL_ONLY, ALWAYS, NEVER }

enum class SendMemory { ALWAYS, LOCAL_ONLY, NEVER }

/** Rdzenie słów danych wrażliwych: z nimi pamięć nie uczy się sama (spec, sekcja 9). */
val SENSITIVE_STEMS: List<String> = listOf(
    "zdrow", "chorob", "chory", "chora", "chorzy", "leki", "lekarstw", "lekarz",
    "leczen", "tętn", "puls", "ciśnien", "cukrzyc", "depres", "terapi", "diagnoz",
    "ciąż", "szpital", "alergi", "nowotw", "onkolog", "psychiat", "adres", "mieszka",
    "ulic", "lokalizacj", "gps", "położeni", "pieniąd", "pieniędz", "zarab", "pensj",
    "wypłat", "konto", "konta", "bank", "zadłuż", "kredyt", "pesel", "hasł",
    "dowód", "wyzna", "religi", "wierząc", "ateist", "katolik", "polityk", "partii",
    "partia", "głosuj", "orientacj", "seksual"
)

/** Domena pamięci: kontekst, w którym pojęcie padło; kolor z logo IRIS. */
data class MemoryDomain(val id: String, val name: String, val info: String, val rgb: List<Int>)

val MEMORY_DOMAINS: List<MemoryDomain> = listOf(
    MemoryDomain("dom", "Dom", "dom i sterowanie urządzeniami", listOf(255, 138, 31)),
    MemoryDomain("czas", "Czas", "przypomnienia, kalendarz, plany", listOf(255, 61, 154)),
    MemoryDomain("ty", "Ty", "ludzie, upodobania, praca, zajęcia", listOf(42, 212, 255)),
    MemoryDomain("rozmowa", "Rozmowa", "wszystko inne", listOf(143, 91, 255)),
)
val MEMORY_DOMAIN_IDS: List<String> = MEMORY_DOMAINS.map { it.id }
const val DEFAULT_MEMORY_DOMAIN = "rozmowa"

/** Rdzenie słów wskazujące domenę (dopasowanie do początku słowa). */
val DOMAIN_WORDS: Map<String, List<String>> = linkedMapOf(
    "dom" to listOf("świat", "swiat", "lamp", "gniazd", "salon", "kuchni", "sypialn", "łazien", "ogrzew", "termostat", "rolet", "zapal", "zgaś", "zgas", "włącz", "wlacz", "wyłącz", "wylacz", "przekaźn", "drzwi", "zamek", "wentyl", "klimaty", "pokój", "pokoj", "przygaś", "jasnoś", "telewiz", "głośnik"),
    "czas" to listOf("przypom", "jutro", "dziś", "dzis", "godzin", "minut", "kalendar", "spotkan", "termin", "notat", "plan", "poniedz", "wtor", "środ", "czwart", "piąt", "sobot", "niedziel", "rano", "wieczor", "tydzie", "tygodn", "urodzin", "rocznic", "budzik"),
    "ty" to listOf("lubi", "uwielbi", "kocha", "prac", "zawod", "zawód", "rodzin", "żon", "mąż", "męż", "dzieci", "dzieck", "córk", "syn", "brat", "siostr", "przyjaci", "koleg", "koleżank", "pies", "psa", "psem", "kot", "hobby", "imię", "imie", "nazyw", "zainteres", "sport", "muzyk", "książ", "film", "studi", "szkoł", "ulubion"),
)

/** Słowa pomocnicze pomijane przez heurystykę wyciągania pojęć. */
val MEMORY_STOP_WORDS: List<String> = listOf(
    "aby", "albo", "ale", "and", "bardzo", "bez", "bo", "był", "była", "było", "będzie", "chciałabym", "chciałbym", "chcę", "ciebie", "czy", "dla", "for", "from", "gdy", "jak", "jako", "jest", "jestem", "jesteś", "już", "która", "które", "który", "lub", "mam", "masz", "mnie", "moja", "moje", "możesz", "można", "mój", "nad", "nie", "ona", "oni", "oraz", "pod", "proszę", "przez", "się", "są", "także", "tego", "tej", "ten", "też", "that", "the", "this", "twoja", "twoje", "twój", "tych", "tylko", "tym", "with", "więc", "żeby",
)

/** Pamięć: graf pojęć (spec, sekcja 9, "Pamięć: graf pojęć"). */
data class MemoryConfig(
    val enabled: Boolean = true,
    val infer: InferPolicy = InferPolicy.LOCAL_ONLY,
    val halfLifeDays: Double = 14.0,
    val initial: Double = 0.35,
    val gainConcept: Double = 0.4,
    val gainLink: Double = 0.3,
    val gainRecall: Double = 0.15,
    val forgetBelow: Double = 0.03,
    val pruneEveryS: Double = 600.0,
    val maxConcepts: Int = 8,
    val recallK: Int = 8,
    val answerWindowS: Double = 120.0,
    /** Pochwała i krytyka ostatniej odpowiedzi zmieniają siłę przypomnianych pojęć. */
    val feedback: Boolean = true,
    val feedbackDown: Double = 0.2,
    val sensitiveWords: List<String> = SENSITIVE_STEMS,
    val domainWords: Map<String, List<String>> = DOMAIN_WORDS,
    val stopWords: List<String> = MEMORY_STOP_WORDS,
) {
    init {
        require(forgetBelow < initial) { "wymagane forgetBelow < initial" }
        require(maxConcepts in 1..32) { "maxConcepts: 1-32" }
    }
}

data class LlmConfig(
    val model: String = DEFAULT_CLAUDE_MODEL,
    val maxTokens: Int = 300,
    val timeoutS: Double = 30.0,
    val retries: Int = 1,
    val historyTurns: Int = 10,
    val sendMemory: SendMemory = SendMemory.LOCAL_ONLY,
    /** Narzędzia pamięci iris_recall i iris_remember (spec, sekcja 12). */
    val tools: Boolean = true,
    val maxToolRounds: Int = 2,
    val effort: String = "low", // "off" pomija parametr
    val thinkingHeadroomTokens: Int = 2000,
    val fallbacks: Boolean = true,
)

const val DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"

/** Całość ustawień rdzenia aplikacji. */
data class CoreConfig(
    val memory: MemoryConfig = MemoryConfig(),
    val llm: LlmConfig = LlmConfig(),
)
