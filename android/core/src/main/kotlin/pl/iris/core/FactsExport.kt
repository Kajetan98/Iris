package pl.iris.core

import java.time.Instant
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** Eksport faktów do pliku JSON (docs/spec.md, sekcja 14, "Eksport faktów"). */

const val EXPORT_FORMAT = "iris-facts"
const val EXPORT_VERSION = 1

private val PRETTY = Json { prettyPrint = true }

/** Fakty w kolejności ekranu pamięci: najpierw podane wprost, potem przypuszczenia od najpewniejszych. */
fun exportFacts(facts: List<Fact>, now: Instant): String {
    val ordered = facts.sortedWith(compareBy<Fact> { it.source != Source.STATED }.thenByDescending { it.confidence })
    val root: JsonObject = buildJsonObject {
        put("format", EXPORT_FORMAT)
        put("version", EXPORT_VERSION)
        put("exportedAt", IsoTime.format(now))
        put(
            "facts",
            buildJsonArray {
                for (f in ordered) {
                    add(
                        buildJsonObject {
                            put("text", f.text)
                            put("source", f.source.wire)
                            put("confidence", f.confidence)
                            put("reinforced", f.reinforced)
                            put("createdAt", IsoTime.format(f.createdAt))
                            put("updatedAt", IsoTime.format(f.updatedAt))
                            put("askedAt", f.askedAt?.let { JsonPrimitive(IsoTime.format(it)) } ?: JsonNull)
                        },
                    )
                }
            },
        )
    }
    return PRETTY.encodeToString(JsonObject.serializer(), root) + "\n"
}
