package pl.iris.app

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import java.time.Instant
import pl.iris.core.Fact
import pl.iris.core.FactRows
import pl.iris.core.IsoTime
import pl.iris.core.Source

/**
 * Tabela `facts` w SQLite w pamięci prywatnej aplikacji (spec, sekcja 14): wiersze jak w sekcji 9 bez `session_id`.
 * Reguły (zmiana tekstu, wzmacnianie, zanikanie) są w RowFactStore z modułu core; tu tylko zapis wierszy.
 * Treść faktów nie trafia do logów.
 */
class SqliteFactRows(context: Context) : FactRows {
    private val helper = object : SQLiteOpenHelper(context.applicationContext, "iris.db", null, 1) {
        override fun onCreate(db: SQLiteDatabase) {
            db.execSQL(
                """
                CREATE TABLE facts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    text       TEXT NOT NULL CHECK (length(trim(text)) > 0),
                    source     TEXT NOT NULL CHECK (source IN ('stated', 'inferred')),
                    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reinforced INTEGER NOT NULL DEFAULT 0,
                    asked_at   TEXT
                )
                """.trimIndent(),
            )
        }

        override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit
    }

    override fun all(): List<Fact> = helper.readableDatabase.query("facts", null, null, null, null, null, "id").use { c ->
        buildList { while (c.moveToNext()) add(read(c)) }
    }

    override fun get(id: Long): Fact? =
        helper.readableDatabase.query("facts", null, "id = ?", arrayOf(id.toString()), null, null, null).use { c ->
            if (c.moveToFirst()) read(c) else null
        }

    override fun insert(text: String, source: Source, confidence: Double, at: Instant): Fact {
        val values = ContentValues().apply {
            put("text", text)
            put("source", source.wire)
            put("confidence", confidence)
            put("created_at", IsoTime.format(at))
            put("updated_at", IsoTime.format(at))
            put("reinforced", 0)
        }
        val id = helper.writableDatabase.insertOrThrow("facts", null, values)
        return Fact(id, text, source, confidence, at, at)
    }

    override fun save(fact: Fact) {
        val values = ContentValues().apply {
            put("text", fact.text)
            put("source", fact.source.wire)
            put("confidence", fact.confidence)
            put("updated_at", IsoTime.format(fact.updatedAt))
            put("reinforced", fact.reinforced)
            if (fact.askedAt == null) putNull("asked_at") else put("asked_at", IsoTime.format(fact.askedAt!!))
        }
        val rows = helper.writableDatabase.update("facts", values, "id = ?", arrayOf(fact.id.toString()))
        require(rows == 1) { "brak wiersza ${fact.id}" }
    }

    override fun delete(id: Long): Boolean = helper.writableDatabase.delete("facts", "id = ?", arrayOf(id.toString())) > 0

    // Bez WHERE delete zwraca liczbę usuniętych wierszy; sqlite_sequence zostaje, więc id nie wracają.
    override fun deleteAll(): Int = helper.writableDatabase.delete("facts", null, null)

    override fun count(): Int =
        helper.readableDatabase.rawQuery("SELECT COUNT(*) FROM facts", null).use { c -> if (c.moveToFirst()) c.getInt(0) else 0 }

    private fun read(c: Cursor): Fact {
        fun col(name: String) = c.getColumnIndexOrThrow(name)
        val asked = col("asked_at").let { if (c.isNull(it)) null else IsoTime.parse(c.getString(it)) }
        return Fact(
            id = c.getLong(col("id")),
            text = c.getString(col("text")),
            source = Source.entries.first { it.wire == c.getString(col("source")) },
            confidence = c.getDouble(col("confidence")),
            createdAt = IsoTime.parse(c.getString(col("created_at"))),
            updatedAt = IsoTime.parse(c.getString(col("updated_at"))),
            reinforced = c.getInt(col("reinforced")),
            askedAt = asked,
        )
    }
}
