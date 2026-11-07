package pl.iris.core

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

/** Narzędzia pamięci dla modelu (docs/spec.md, sekcja 12, "Narzędzia pamięci"); port host/brain/tools.py. */

val RECALL_TOOL = ToolSpec(
    name = "iris_recall",
    description = "Przypomnij sobie, co pamięć IRIS kojarzy z tematem: pasujące pojęcia z wcześniejszych rozmów i pojęcia " +
        "z nimi powiązane, od najsilniejszych. Użyj, gdy rozmowa dotyczy czegoś, o czym użytkownik mógł już " +
        "wcześniej mówić, albo gdy pyta, co pamiętasz na dany temat. Wynik to pojęcia, nie zdania.",
    parameters = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("query") {
                put("type", "string")
                put("description", "Temat albo zdanie, np. 'pies' lub 'lampa w salonie'.")
            }
        }
        putJsonArray("required") { add("query") }
        put("additionalProperties", false)
    },
)

val REMEMBER_TOOL = ToolSpec(
    name = "iris_remember",
    description = "Zapisz w pamięci IRIS pojęcia z jednego zdania, tylko gdy użytkownik wprost prosi o zapamiętanie " +
        "czegoś. Rozmowy uczą pamięć same, więc nie używaj tego dla zwykłych wypowiedzi. Liczby i dane wrażliwe " +
        "nie są zapisywane.",
    parameters = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("text") {
                put("type", "string")
                put("description", "Co zapamiętać, jednym zdaniem.")
            }
            putJsonObject("domain") {
                put("type", "string")
                putJsonArray("enum") { MEMORY_DOMAIN_IDS.forEach { add(it) } }
                put("description", "Opcjonalnie: dom, czas, ty albo rozmowa.")
            }
        }
        putJsonArray("required") { add("text") }
        put("additionalProperties", false)
    },
)

/** Narzędzia jednej tury rozmowy. */
interface ToolBox {
    /** Narzędzia, które wolno dać temu silnikowi (polityka `sendMemory`). */
    fun specs(remote: Boolean): List<ToolSpec>

    /** Wykonanie narzędzia; błąd zwraca jako wynik z `isError`. */
    suspend fun call(call: ToolCall): ToolResult

    /** Pojęcia do bloku kontekstu, gdy model nie dostaje `iris_recall`. */
    suspend fun context(text: String, remote: Boolean): List<String>
}

internal fun JsonObject.stringArg(key: String): String? =
    (this[key] as? kotlinx.serialization.json.JsonPrimitive)?.takeIf { it.isString }?.content
