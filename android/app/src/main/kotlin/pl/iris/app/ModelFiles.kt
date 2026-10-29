package pl.iris.app

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import java.io.File
import java.io.IOException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext

/** Zły plik modelu albo brak miejsca; komunikat jest po polsku i bez danych użytkownika. */
class ModelImportException(message: String) : Exception(message)

/**
 * Plik modelu lokalnego w pamięci prywatnej aplikacji (docs/spec.md, sekcja 14): jeden naraz, wybrany przez
 * użytkownika systemowym wyborem plików i skopiowany, bo środowisko wymaga zwykłej ścieżki.
 */
class ModelFiles(private val context: Context) {
    private val dir get() = File(context.filesDir, "model")

    private val _progress = MutableStateFlow<String?>(null)

    /** Tekst stanu kopiowania ("Kopiuję model... 35%") albo null, gdy nic się nie dzieje. */
    val progress: StateFlow<String?> = _progress

    fun file(name: String): File = File(dir, name)

    fun clear() {
        dir.listFiles()?.forEach { it.delete() }
    }

    /** Kopiuje wskazany plik i zwraca jego nazwę w katalogu modelu; poprzedni model jest usuwany po sukcesie. */
    suspend fun import(uri: Uri): String = withContext(Dispatchers.IO) {
        val resolver = context.contentResolver
        var shown: String? = null
        var total = -1L
        resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                shown = c.getString(0)
                total = if (c.isNull(1)) -1L else c.getLong(1)
            }
        }
        val name = safeName(shown) ?: throw ModelImportException("Wybierz plik z rozszerzeniem .litertlm.")
        dir.mkdirs()
        val part = File(dir, "$name.part")
        try {
            _progress.value = "Kopiuję model..."
            val input = resolver.openInputStream(uri) ?: throw ModelImportException("Nie można otworzyć wybranego pliku.")
            input.use { src ->
                part.outputStream().use { out ->
                    val buffer = ByteArray(1 shl 20)
                    var done = 0L
                    var lastPercent = -1
                    while (true) {
                        val n = src.read(buffer)
                        if (n < 0) break
                        out.write(buffer, 0, n)
                        done += n
                        if (total > 0) {
                            val percent = (done * 100 / total).toInt()
                            if (percent != lastPercent) {
                                lastPercent = percent
                                _progress.value = "Kopiuję model... $percent%"
                            }
                        }
                    }
                }
            }
            dir.listFiles()?.filter { it.name != part.name && it.name != name }?.forEach { it.delete() }
            val target = File(dir, name)
            target.delete()
            if (!part.renameTo(target)) throw ModelImportException("Nie udało się zapisać modelu w pamięci aplikacji.")
            name
        } catch (e: ModelImportException) {
            part.delete()
            throw e
        } catch (e: IOException) {
            part.delete()
            throw ModelImportException("Kopiowanie nie powiodło się (zwykle brak miejsca na telefonie). Potrzeba tyle wolnego miejsca, ile zajmuje plik.")
        } finally {
            _progress.value = null
        }
    }

    private fun safeName(shown: String?): String? {
        val base = shown?.substringAfterLast('/')?.trim().orEmpty()
        val clean = base.replace(Regex("[^A-Za-z0-9._-]"), "_")
        return clean.takeIf { it.length > ".litertlm".length && it.endsWith(".litertlm", ignoreCase = true) }
    }
}
