package pl.iris.core

import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

class MemoryExportTest {
    private val t0 = Instant.parse("2026-11-01T12:00:00Z")

    @Test
    fun `the export has the format, concepts from the strongest, links and the forgotten count`() {
        val store = InMemoryMemoryStore()
        val clock = ManualClock(t0)
        val keeper = MemoryKeeper(MemoryConfig(), LlmConfig(), clock, store)
        keeper.teach("Lampa w salonie", "dom")
        keeper.teach("lampa")
        store.save(forgotten = 2)
        val json = exportMemory(keeper.view(), t0.plusSeconds(60))
        assertTrue(json.endsWith("}\n") && "\n    \"format\"" in json) // czytelne wcięcia
        val root = Json.parseToJsonElement(json).jsonObject
        assertEquals("iris-memory", root.getValue("format").jsonPrimitive.content)
        assertEquals(1, root.getValue("version").jsonPrimitive.content.toInt())
        assertEquals("2026-11-01T12:01:00.000+00:00", root.getValue("exportedAt").jsonPrimitive.content)
        val concepts = root.getValue("concepts").jsonArray.map { it.jsonObject }
        assertEquals(listOf("lampa", "salonie"), concepts.map { it.getValue("label").jsonPrimitive.content })
        val lamp = concepts.first()
        assertEquals(setOf("key", "label", "strength", "effective", "hits", "lastUsed", "createdAt", "contexts"), lamp.keys)
        assertEquals(2, lamp.getValue("hits").jsonPrimitive.content.toInt())
        assertEquals("2", lamp.getValue("contexts").jsonObject.getValue("dom").jsonPrimitive.content) // "lampa" to słowo domeny dom
        assertEquals(1, root.getValue("links").jsonArray.size)
        assertEquals(2, root.getValue("forgotten").jsonPrimitive.content.toInt())
    }
}
