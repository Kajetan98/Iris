package pl.iris.core

/**
 * Wartości początkowe z konfiguracji hosta (host/core/config.py). Test zgodności
 * (VectorsTest) pilnuje, żeby się nie rozjechały z kodem Pythona.
 */

enum class InferPolicy { LOCAL_ONLY, ALWAYS, NEVER }

enum class SendFacts { ALWAYS, LOCAL_ONLY, NEVER }

/** Rdzenie słów danych wrażliwych: wniosków z nimi IRIS nie zapisuje sama (spec, sekcja 9). */
val SENSITIVE_STEMS: List<String> = listOf(
    "zdrow", "chorob", "chory", "chora", "chorzy", "leki", "lekarstw", "lekarz",
    "leczen", "tętn", "puls", "ciśnien", "cukrzyc", "depres", "terapi", "diagnoz",
    "ciąż", "szpital", "alergi", "nowotw", "onkolog", "psychiat", "adres", "mieszka",
    "ulic", "lokalizacj", "gps", "położeni", "pieniąd", "pieniędz", "zarab", "pensj",
    "wypłat", "konto", "konta", "bank", "zadłuż", "kredyt", "pesel", "hasł",
    "dowód", "wyzna", "religi", "wierząc", "ateist", "katolik", "polityk", "partii",
    "partia", "głosuj", "orientacj", "seksual"
)

data class LearningConfig(
    val enabled: Boolean = true,
    val infer: InferPolicy = InferPolicy.LOCAL_ONLY,
    val maxPerTurn: Int = 3,
    val similar: Double = 0.6,
    val forgetMatch: Double = 0.5,
    val startMax: Double = 0.45,
    val reinforceStep: Double = 0.2,
    val maxInferred: Double = 0.9,
    val ask: Boolean = true,
    val askMin: Double = 0.4,
    val askIntervalS: Double = 900.0,
    val askAgainDays: Double = 7.0,
    val answerWindowS: Double = 120.0,
    val decayAfterDays: Double = 30.0,
    val decayStep: Double = 0.1,
    val forgetBelow: Double = 0.2,
    /** Pochwała i krytyka ostatniej odpowiedzi zmieniają pewność użytych przypuszczeń (spec, sekcja 9). */
    val feedback: Boolean = true,
    val feedbackUp: Double = 0.1,
    val feedbackDown: Double = 0.2,
    val sensitiveWords: List<String> = SENSITIVE_STEMS,
) {
    init {
        require(maxPerTurn in 0..10) { "maxPerTurn: 0-10" }
        require(forgetBelow < startMax && startMax <= maxInferred) { "wymagane forgetBelow < startMax <= maxInferred" }
    }
}

data class LlmConfig(
    val model: String = DEFAULT_CLAUDE_MODEL,
    val maxTokens: Int = 300,
    val timeoutS: Double = 30.0,
    val retries: Int = 1,
    val historyTurns: Int = 10,
    val sendFacts: SendFacts = SendFacts.LOCAL_ONLY,
    val maxFacts: Int = 30,
    val minInferredConfidence: Double = 0.5,
    val effort: String = "low", // "off" pomija parametr
    val thinkingHeadroomTokens: Int = 2000,
    val fallbacks: Boolean = true,
)

const val DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"

/** Całość ustawień rdzenia. `quiet` odpowiada `initiative.quiet` hosta: bez pytań o przypuszczenia. */
data class CoreConfig(
    val learning: LearningConfig = LearningConfig(),
    val llm: LlmConfig = LlmConfig(),
    val quiet: Boolean = false,
)
