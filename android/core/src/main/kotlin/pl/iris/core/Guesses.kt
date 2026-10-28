package pl.iris.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull

/**
 * Wnioski o użytkowniku z jego wypowiedzi (docs/spec.md, sekcja 9, "Uczenie się"). LLM tylko proponuje:
 * osobne zapytanie z samą wypowiedzią, bez historii, stanu i znanych faktów, odpowiedź jako lista JSON.
 * Port host/brain/learn.py.
 */

val EXTRACT_PROMPT: String = listOf(
    "Z wypowiedzi użytkownika wypisujesz trwałe fakty o nim samym, które przydadzą się asystentowi głosowemu w kolejnych rozmowach: zainteresowania, zwyczaje, plany, praca, bliskie osoby i zwierzęta, upodobania, jak chce, żeby się do niego zwracać.",
    "",
    "Zasady:",
    "- Tylko to, co wynika z wypowiedzi. Nie dopowiadasz niczego ponad nią.",
    "- Pomijasz pytania, polecenia i rzeczy chwilowe (pogoda, samopoczucie w tej chwili).",
    "- Pomijasz zdrowie, leki, położenie i adres, pieniądze, wyznanie, poglądy polityczne i orientację.",
    "- Każdy fakt to jedno krótkie zdanie w drugiej osobie liczby pojedynczej, np. \"Masz psa o imieniu Burek.\"",
    "- Pewność od 0 do 1 mówi, jak wyraźnie wypowiedź potwierdza fakt.",
    "- Odpowiadasz wyłącznie listą JSON, bez innego tekstu: [{\"fakt\": \"...\", \"pewnosc\": 0.7}]. Gdy nie ma faktów: [].",
).joinToString("\n") + "\n"

const val EXTRACT_MAX_TOKENS = 300

/** Silnik nie podał poprawnej listy wniosków. Tura rozmowy się nie zmienia. */
class LearnError(message: String) : Exception(message)

data class Guess(val text: String, val confidence: Double)

/** Lista wniosków z odpowiedzi silnika (także w bloku ```json). [LearnError], gdy to nie lista. */
fun parseGuesses(raw: String, limit: Int): List<Guess> {
    val start = raw.indexOf('[')
    val end = raw.lastIndexOf(']')
    if (start < 0 || end < start) throw LearnError("odpowiedź bez listy JSON")
    val data: JsonElement = try {
        Json.parseToJsonElement(raw.substring(start, end + 1))
    } catch (_: IllegalArgumentException) { // SerializationException jest jego podklasą
        throw LearnError("niepoprawny JSON")
    }
    if (data !is JsonArray) throw LearnError("odpowiedź nie jest listą")
    val guesses = mutableListOf<Guess>()
    for (item in data) {
        if (guesses.size >= limit) break
        if (item !is JsonObject) continue
        val text = pick(item, "fakt", "text")
        val conf = pick(item, "pewnosc", "pewność", "confidence")
        if (text !is JsonPrimitive || !text.isString) continue
        if (conf !is JsonPrimitive || conf.isString || conf.booleanOrNull != null) continue
        val value = conf.doubleOrNull ?: continue
        val clean = factText(text.content)
        if (cpLength(clean) in MIN_FACT_CHARS..MAX_FACT_CHARS) {
            guesses += Guess(clean, minOf(1.0, maxOf(0.0, value)))
        }
    }
    return guesses
}

/** `dict.get(a, dict.get(b, ...))` Pythona: pierwszy obecny klucz, nawet z wartością null. */
private fun pick(item: JsonObject, vararg keys: String): JsonElement? = keys.firstOrNull { it in item }?.let { item[it] }

/** Propozycje faktów z jednej wypowiedzi. Rzuca [LlmException] albo [LearnError]. */
suspend fun inferFacts(engine: LlmEngine, utterance: String, limit: Int): List<Guess> {
    val reply = engine.complete(EXTRACT_PROMPT, listOf(Message(Role.USER, utterance)), EXTRACT_MAX_TOKENS)
    if (reply.finish == Finish.REFUSAL) throw LearnError("silnik odmówił")
    return parseGuesses(reply.text, limit)
}
