package pl.iris.core

import java.time.Instant

/**
 * Zapis grafu pojęć (docs/spec.md, sekcje 9 i 14). Semantyka jak tabele pamięci hosta
 * (host/store/store.py); na telefonie SQLite, w testach [InMemoryMemoryStore].
 */
interface MemoryStore {
    /** Pojęcia o podanych kluczach (wszystkie, gdy [keys] to null). */
    fun concepts(keys: Collection<String>? = null): Map<String, Concept>

    /** Połączenia, których jednym końcem jest któryś z kluczy. */
    fun linksTouching(keys: Collection<String>): List<Link>

    fun linksBetween(keys: Collection<String>): Map<Pair<String, String>, Link>

    fun allLinks(): List<Link>

    /**
     * Zmiany grafu w jednej transakcji: wiersze pojęć (etykieta i czas utworzenia zostają z pierwszego zapisu)
     * i połączeń, usunięcia (z połączeniami pojęcia), licznik zapomnianych.
     */
    fun save(
        concepts: Collection<Concept> = emptyList(),
        links: Collection<Link> = emptyList(),
        delete: Collection<String> = emptyList(),
        deleteLinks: Collection<Pair<String, String>> = emptyList(),
        forgotten: Int = 0,
    )

    /** Usuwa pojęcia z połączeniami; zwraca usunięte. */
    fun deleteConcepts(keys: Collection<String>): List<Concept>

    /** Usuwa całą pamięć: pojęcia, połączenia, licznik zapomnianych i dawne fakty (`facts_v3`). */
    fun clear(): Int

    fun count(): Int

    fun forgotten(): Int
}

/** Graf w pamięci procesu (testy). */
class InMemoryMemoryStore : MemoryStore {
    private val concepts = LinkedHashMap<String, Concept>()
    private val links = LinkedHashMap<Pair<String, String>, Link>()
    private var forgotten = 0

    override fun concepts(keys: Collection<String>?): Map<String, Concept> =
        if (keys == null) LinkedHashMap(concepts) else keys.mapNotNull { k -> concepts[k]?.let { k to it } }.toMap()

    override fun linksTouching(keys: Collection<String>): List<Link> {
        val set = keys.toSet()
        return links.values.filter { it.a in set || it.b in set }
    }

    override fun linksBetween(keys: Collection<String>): Map<Pair<String, String>, Link> {
        val set = keys.toSet()
        return links.filterKeys { it.first in set && it.second in set }
    }

    override fun allLinks(): List<Link> = links.values.toList()

    override fun save(
        concepts: Collection<Concept>,
        links: Collection<Link>,
        delete: Collection<String>,
        deleteLinks: Collection<Pair<String, String>>,
        forgotten: Int,
    ) {
        for (c in concepts) {
            val old = this.concepts[c.key]
            val strength = c.strength.coerceIn(0.0, 1.0)
            this.concepts[c.key] = if (old == null) c.copy(strength = strength) else c.copy(label = old.label, createdAt = old.createdAt, strength = strength)
        }
        for (l in links) {
            require(l.a < l.b) { "połączenie wymaga a < b" }
            require(l.a in this.concepts && l.b in this.concepts) { "połączenie z nieznanym pojęciem" }
            this.links[l.a to l.b] = l.copy(strength = l.strength.coerceIn(0.0, 1.0))
        }
        for (p in deleteLinks) this.links.remove(p)
        deleteConcepts(delete)
        this.forgotten += forgotten
    }

    override fun deleteConcepts(keys: Collection<String>): List<Concept> {
        val removed = keys.distinct().mapNotNull { concepts.remove(it) }
        val gone = removed.map { it.key }.toSet()
        links.keys.removeAll { it.first in gone || it.second in gone }
        return removed
    }

    override fun clear(): Int = concepts.size.also {
        concepts.clear()
        links.clear()
        forgotten = 0
    }

    override fun count(): Int = concepts.size

    override fun forgotten(): Int = forgotten
}

/** Fakt z bazy w wersji z faktami (tabela `facts`), do migracji. */
data class LegacyFact(val text: String, val updatedAt: Instant)

/**
 * Migracja faktów do grafu (spec, sekcja 9): każdy fakt uczy graf heurystyką, bez filtra danych wrażliwych
 * (fakty trafiły tam za zgodą użytkownika), z filtrem liczb, w kolejności i w chwili `updatedAt`.
 */
fun migrateFacts(facts: List<LegacyFact>, store: MemoryStore, cfg: MemoryConfig = MemoryConfig()) {
    for (fact in facts.sortedBy { it.updatedAt }) {
        val ext = keep(heuristic(fact.text, cfg), cfg, allowSensitive = true)
        val keys = ext.concepts.map { it.first }
        if (keys.isEmpty()) continue
        val plan = planLearn(ext, store.concepts(keys), store.linksBetween(keys), fact.updatedAt.toMillisPrecision(), cfg)
        store.save(plan.concepts, plan.links)
    }
}
