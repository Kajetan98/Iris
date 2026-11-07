package pl.iris.app

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import pl.iris.core.Concept
import pl.iris.core.IsoTime
import pl.iris.core.LegacyFact
import pl.iris.core.Link
import pl.iris.core.MemoryConfig
import pl.iris.core.MemoryStore
import pl.iris.core.migrateFacts

/**
 * Graf pojęć w SQLite w pamięci prywatnej aplikacji (spec, sekcje 9 i 14): tabele `concepts`, `concept_domains`,
 * `concept_links` i `memory_meta` jak w schemacie 4 hosta. Wersja 1 bazy miała tabelę `facts`; przejście na
 * wersję 2 uczy graf z faktów (migrateFacts, jak migracja hosta) w transakcji aktualizacji i zostawia tabelę
 * jako `facts_v3`. Reguły są w module core; tu tylko zapis wierszy. Treść pamięci nie trafia do logów.
 */
class SqliteMemoryStore(context: Context, private val cfg: MemoryConfig = MemoryConfig()) : MemoryStore {
    private val helper = object : SQLiteOpenHelper(context.applicationContext, "iris.db", null, VERSION) {
        override fun onConfigure(db: SQLiteDatabase) = db.setForeignKeyConstraintsEnabled(true)

        override fun onCreate(db: SQLiteDatabase) = createGraph(db)

        // SQLiteOpenHelper wywołuje onUpgrade w transakcji: błąd zostawia bazę w wersji 1 z faktami.
        override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
            if (oldVersion < 2) {
                createGraph(db)
                db.execSQL("ALTER TABLE facts RENAME TO facts_v3")
                val facts = db.rawQuery("SELECT text, updated_at FROM facts_v3 ORDER BY updated_at, id", null).use { c ->
                    buildList { while (c.moveToNext()) add(LegacyFact(c.getString(0), IsoTime.parse(c.getString(1)))) }
                }
                migrateFacts(facts, Rows(db), cfg)
            }
        }
    }

    private val rows: Rows get() = Rows(helper.writableDatabase)

    override fun concepts(keys: Collection<String>?): Map<String, Concept> = rows.concepts(keys)

    override fun linksTouching(keys: Collection<String>): List<Link> = rows.linksTouching(keys)

    override fun linksBetween(keys: Collection<String>): Map<Pair<String, String>, Link> = rows.linksBetween(keys)

    override fun allLinks(): List<Link> = rows.allLinks()

    override fun save(
        concepts: Collection<Concept>,
        links: Collection<Link>,
        delete: Collection<String>,
        deleteLinks: Collection<Pair<String, String>>,
        forgotten: Int,
    ): Unit = rows.save(concepts, links, delete, deleteLinks, forgotten)

    override fun deleteConcepts(keys: Collection<String>): List<Concept> = rows.deleteConcepts(keys)

    override fun clear(): Int = rows.clear()

    override fun count(): Int = rows.count()

    override fun forgotten(): Int = rows.forgotten()

    /** Operacje na jednym połączeniu: zwykłym albo tym z transakcji aktualizacji (migracja). */
    private class Rows(private val db: SQLiteDatabase) : MemoryStore {
        override fun concepts(keys: Collection<String>?): Map<String, Concept> {
            if (keys == null) return load(null)
            val out = LinkedHashMap<String, Concept>()
            for (part in keys.distinct().chunked(CHUNK)) out += load(part)
            return out
        }

        private fun load(keys: List<String>?): Map<String, Concept> {
            if (keys != null && keys.isEmpty()) return emptyMap()
            val where = if (keys == null) "" else " WHERE key IN (${marks(keys.size)})"
            val args = keys?.toTypedArray()
            val domains = HashMap<String, MutableMap<String, Int>>()
            db.rawQuery("SELECT key, domain, count FROM concept_domains$where ORDER BY rowid", args).use { c ->
                while (c.moveToNext()) domains.getOrPut(c.getString(0)) { LinkedHashMap() }[c.getString(1)] = c.getInt(2)
            }
            return db.rawQuery("SELECT key, label, strength, last_used, hits, created_at FROM concepts$where ORDER BY rowid", args).use { c ->
                buildMap<String, Concept> {
                    while (c.moveToNext()) {
                        val key = c.getString(0)
                        put(
                            key,
                            Concept(
                                key, c.getString(1), c.getDouble(2), IsoTime.parse(c.getString(3)), c.getInt(4),
                                IsoTime.parse(c.getString(5)), domains[key].orEmpty(),
                            ),
                        )
                    }
                }
            }
        }

        override fun linksTouching(keys: Collection<String>): List<Link> {
            val seen = LinkedHashMap<Pair<String, String>, Link>()
            for (part in keys.distinct().chunked(CHUNK / 2)) {
                val m = marks(part.size)
                val args = (part + part).toTypedArray()
                readLinks("SELECT a, b, strength, last_used FROM concept_links WHERE a IN ($m) OR b IN ($m)", args)
                    .forEach { seen[it.a to it.b] = it }
            }
            return seen.values.toList()
        }

        override fun linksBetween(keys: Collection<String>): Map<Pair<String, String>, Link> {
            val set = keys.toSet()
            if (set.size < 2) return emptyMap()
            return linksTouching(set).filter { it.a in set && it.b in set }.associateBy { it.a to it.b }
        }

        override fun allLinks(): List<Link> = readLinks("SELECT a, b, strength, last_used FROM concept_links ORDER BY rowid", null)

        private fun readLinks(sql: String, args: Array<String>?): List<Link> = db.rawQuery(sql, args).use { c ->
            buildList { while (c.moveToNext()) add(Link(c.getString(0), c.getString(1), c.getDouble(2), IsoTime.parse(c.getString(3)))) }
        }

        override fun save(
            concepts: Collection<Concept>,
            links: Collection<Link>,
            delete: Collection<String>,
            deleteLinks: Collection<Pair<String, String>>,
            forgotten: Int,
        ): Unit = transaction {
            // Bez UPSERT (ON CONFLICT DO UPDATE wymaga SQLite 3.24, czyli Androida 11; minSdk to 26):
            // INSERT OR IGNORE i UPDATE. Etykieta i czas utworzenia zostają z pierwszego zapisu, jak na hoście.
            for (c in concepts) {
                db.execSQL(
                    "INSERT OR IGNORE INTO concepts(key, label, strength, last_used, hits, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    arrayOf<Any>(c.key, c.label, c.strength.coerceIn(0.0, 1.0), IsoTime.format(c.lastUsed), c.hits, IsoTime.format(c.createdAt)),
                )
                db.update(
                    "concepts",
                    ContentValues().apply {
                        put("strength", c.strength.coerceIn(0.0, 1.0))
                        put("last_used", IsoTime.format(c.lastUsed))
                        put("hits", c.hits)
                    },
                    "key = ?",
                    arrayOf(c.key),
                )
                for ((domain, count) in c.domains) {
                    db.execSQL("INSERT OR IGNORE INTO concept_domains(key, domain, count) VALUES (?, ?, ?)", arrayOf<Any>(c.key, domain, count))
                    db.update("concept_domains", ContentValues().apply { put("count", count) }, "key = ? AND domain = ?", arrayOf(c.key, domain))
                }
            }
            for (l in links) {
                db.execSQL(
                    "INSERT OR IGNORE INTO concept_links(a, b, strength, last_used) VALUES (?, ?, ?, ?)",
                    arrayOf<Any>(l.a, l.b, l.strength.coerceIn(0.0, 1.0), IsoTime.format(l.lastUsed)),
                )
                db.update(
                    "concept_links",
                    ContentValues().apply {
                        put("strength", l.strength.coerceIn(0.0, 1.0))
                        put("last_used", IsoTime.format(l.lastUsed))
                    },
                    "a = ? AND b = ?",
                    arrayOf(l.a, l.b),
                )
            }
            for ((a, b) in deleteLinks) db.delete("concept_links", "a = ? AND b = ?", arrayOf(a, b))
            for (k in delete) db.delete("concepts", "key = ?", arrayOf(k)) // domeny i połączenia przez ON DELETE CASCADE
            if (forgotten != 0) {
                db.execSQL("INSERT OR IGNORE INTO memory_meta(k, v) VALUES ('forgotten', '0')")
                db.execSQL("UPDATE memory_meta SET v = CAST(CAST(v AS INTEGER) + ? AS TEXT) WHERE k = 'forgotten'", arrayOf<Any>(forgotten))
            }
        }

        override fun deleteConcepts(keys: Collection<String>): List<Concept> = transaction {
            val found = concepts(keys)
            for (k in found.keys) db.delete("concepts", "key = ?", arrayOf(k))
            keys.distinct().mapNotNull { found[it] }
        }

        override fun clear(): Int = transaction {
            val n = db.delete("concepts", null, null)
            db.delete("concept_links", null, null)
            db.delete("memory_meta", null, null)
            db.execSQL("DROP TABLE IF EXISTS facts_v3")
            n
        }

        override fun count(): Int = db.rawQuery("SELECT COUNT(*) FROM concepts", null).use { c -> if (c.moveToFirst()) c.getInt(0) else 0 }

        override fun forgotten(): Int =
            db.rawQuery("SELECT v FROM memory_meta WHERE k = 'forgotten'", null).use { c ->
                if (c.moveToFirst()) c.getString(0).toIntOrNull() ?: 0 else 0
            }

        // W transakcji aktualizacji bazy beginTransaction zagnieżdża się w niej.
        private inline fun <T> transaction(block: () -> T): T {
            db.beginTransaction()
            try {
                return block().also { db.setTransactionSuccessful() }
            } finally {
                db.endTransaction()
            }
        }
    }

    private companion object {
        const val VERSION = 2

        // Starsze wersje SQLite na Androidzie przyjmują do 999 parametrów w zapytaniu.
        const val CHUNK = 500

        fun marks(n: Int) = List(n) { "?" }.joinToString(",")

        fun createGraph(db: SQLiteDatabase) {
            db.execSQL(
                """
                CREATE TABLE concepts (
                    key        TEXT PRIMARY KEY CHECK (length(key) > 0),
                    label      TEXT NOT NULL,
                    strength   REAL NOT NULL CHECK (strength BETWEEN 0 AND 1),
                    last_used  TEXT NOT NULL,
                    hits       INTEGER NOT NULL CHECK (hits >= 1),
                    created_at TEXT NOT NULL
                )
                """.trimIndent(),
            )
            db.execSQL(
                """
                CREATE TABLE concept_domains (
                    key    TEXT NOT NULL REFERENCES concepts(key) ON DELETE CASCADE,
                    domain TEXT NOT NULL,
                    count  INTEGER NOT NULL CHECK (count >= 1),
                    PRIMARY KEY (key, domain)
                )
                """.trimIndent(),
            )
            db.execSQL(
                """
                CREATE TABLE concept_links (
                    a         TEXT NOT NULL REFERENCES concepts(key) ON DELETE CASCADE,
                    b         TEXT NOT NULL REFERENCES concepts(key) ON DELETE CASCADE,
                    strength  REAL NOT NULL CHECK (strength BETWEEN 0 AND 1),
                    last_used TEXT NOT NULL,
                    PRIMARY KEY (a, b),
                    CHECK (a < b)
                )
                """.trimIndent(),
            )
            db.execSQL("CREATE INDEX concept_links_b ON concept_links(b)")
            db.execSQL("CREATE TABLE memory_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        }
    }
}
