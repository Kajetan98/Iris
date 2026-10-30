package pl.iris.core

import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

class FactsExportTest {
    private val t0 = Instant.parse("2026-10-30T10:00:00.123Z")
    private val now = Instant.parse("2026-10-31T08:30:00Z")

    private fun fact(id: Long, text: String, source: Source, conf: Double, asked: Instant? = null) =
        Fact(id, text, source, conf, t0, t0.plusSeconds(60), reinforced = 2, askedAt = asked)

    @Test
    fun `the file has the format header and facts in the memory screen order without ids`() {
        val out = exportFacts(
            listOf(
                fact(1, "Masz psa o imieniu Burek", Source.INFERRED, 0.55),
                fact(2, "Lubisz kawę", Source.STATED, 1.0),
                fact(3, "Grasz w piątki w kosza", Source.INFERRED, 0.8, asked = t0),
            ),
            now,
        )
        val root = Json.parseToJsonElement(out).jsonObject
        assertEquals("iris-facts", root.getValue("format").jsonPrimitive.content)
        assertEquals(1, root.getValue("version").jsonPrimitive.content.toInt())
        assertEquals("2026-10-31T08:30:00.000+00:00", root.getValue("exportedAt").jsonPrimitive.content)
        val facts = root.getValue("facts").jsonArray.map { it.jsonObject }
        assertEquals(listOf("Lubisz kawę", "Grasz w piątki w kosza", "Masz psa o imieniu Burek"), facts.map { it.getValue("text").jsonPrimitive.content })
        assertTrue(facts.none { "id" in it })
        val first = facts[0]
        assertEquals("stated", first.getValue("source").jsonPrimitive.content)
        assertEquals(1.0, first.getValue("confidence").jsonPrimitive.content.toDouble())
        assertEquals(2, first.getValue("reinforced").jsonPrimitive.content.toInt())
        assertEquals("2026-10-30T10:00:00.123+00:00", first.getValue("createdAt").jsonPrimitive.content)
        assertEquals("2026-10-30T10:01:00.123+00:00", first.getValue("updatedAt").jsonPrimitive.content)
        assertEquals(JsonNull, first.getValue("askedAt"))
        assertEquals("2026-10-30T10:00:00.123+00:00", facts[1].getValue("askedAt").jsonPrimitive.content)
    }

    @Test
    fun `an empty memory exports an empty list and special characters survive`() {
        assertEquals(0, Json.parseToJsonElement(exportFacts(emptyList(), now)).jsonObject.getValue("facts").jsonArray.size)
        val text = "Mówisz \"cześć\" i ścieżka C:\\x\nz nową linią 🙂"
        val out = exportFacts(listOf(fact(1, text, Source.STATED, 1.0)), now)
        val back = Json.parseToJsonElement(out).jsonObject.getValue("facts").jsonArray.single().jsonObject.getValue("text").jsonPrimitive.content
        assertEquals(text, back)
        assertTrue(out.endsWith("}\n"))
        assertTrue("\n  " in out) // z wcięciami
    }
}
