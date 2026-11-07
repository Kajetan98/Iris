package pl.iris.core

import java.time.Instant
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/** Eksport pamięci do pliku JSON (docs/spec.md, sekcja 14, "Eksport pamięci"). */

const val EXPORT_FORMAT = "iris-memory"
const val EXPORT_VERSION = 1

private val PRETTY = Json { prettyPrint = true }

/** Cała pamięć: pojęcia od najsilniejszych, połączenia, licznik zapomnianych. */
fun exportMemory(view: MemoryView, now: Instant): String {
    val root: JsonObject = buildJsonObject {
        put("format", EXPORT_FORMAT)
        put("version", EXPORT_VERSION)
        put("exportedAt", IsoTime.format(now))
        put(
            "concepts",
            buildJsonArray {
                for (v in view.concepts) {
                    val c = v.concept
                    add(
                        buildJsonObject {
                            put("key", c.key)
                            put("label", c.label)
                            put("strength", c.strength)
                            put("effective", v.effective)
                            put("hits", c.hits)
                            put("lastUsed", IsoTime.format(c.lastUsed))
                            put("createdAt", IsoTime.format(c.createdAt))
                            putJsonObject("contexts") { for ((d, n) in c.domains) put(d, n) }
                        },
                    )
                }
            },
        )
        put(
            "links",
            buildJsonArray {
                for (l in view.links) {
                    add(
                        buildJsonObject {
                            put("a", l.a)
                            put("b", l.b)
                            put("strength", l.strength)
                            put("lastUsed", IsoTime.format(l.lastUsed))
                        },
                    )
                }
            },
        )
        put("forgotten", view.forgotten)
    }
    return PRETTY.encodeToString(JsonObject.serializer(), root) + "\n"
}
